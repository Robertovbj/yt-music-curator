"""Pool de saídas de rede para o yt-dlp (ADR-08.3): cota diária, estatísticas e quarentena por saída.

A conexão direta ("direta") é sempre a primeira saída. O ytmusicapi (sua conta) nunca passa por aqui.
"""

import re
import sqlite3
from datetime import UTC, datetime, timedelta

from curator.audio.provedores.base import Saida

DIRETA = "direta"
FALHAS_PARA_QUARENTENA = 5


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hoje(agora: datetime) -> str:
    """Dia da cota no fuso LOCAL: em UTC o dia viraria às 21h (-03) e daria duas cotas no mesmo dia."""
    return agora.astimezone().date().isoformat()


def mascarar(url: str | None) -> str:
    """socks5://usuario:senha@host:1080 → socks5://***@host:1080 (nunca logar credenciais)."""
    return re.sub(r"//[^/@]+@", "//***@", url) if url else "(direta)"


def sincronizar_config(con: sqlite3.Connection, cfg: dict) -> None:
    """Espelha `[[proxy]]` do config na tabela; proxies removidos do config ficam inativos."""
    cota = cfg["fila"]["cota_diaria"]
    desejadas = {DIRETA: (None, cota)}
    for p in cfg.get("proxy", []):
        if p.get("nome") == DIRETA or not p.get("url"):
            raise ValueError("cada [[proxy]] precisa de `nome` (diferente de 'direta') e `url`")
        desejadas[p["nome"]] = (p["url"], p.get("cota_diaria", cota))
    for nome, (url, c) in desejadas.items():
        con.execute(
            "INSERT INTO proxies (nome, url, cota_diaria, ativo) VALUES (?, ?, ?, 1) "
            "ON CONFLICT (nome) DO UPDATE SET url = excluded.url, cota_diaria = excluded.cota_diaria, ativo = 1",
            (nome, url, c),
        )
    con.execute(
        f"UPDATE proxies SET ativo = 0 WHERE nome NOT IN ({','.join('?' * len(desejadas))})", tuple(desejadas)
    )


_USO = "CASE WHEN dia_uso = :hoje THEN uso_hoje ELSE 0 END"


def escolher(con: sqlite3.Connection, agora: datetime, nome: str | None = None) -> Saida | None:
    """A saída ativa, fora de quarentena e com cota, menos usada hoje (empate: a usada há mais tempo).

    Com `nome`, só aquela saída (no modo paralelo cada worker tem a sua)."""
    linha = con.execute(
        f"""SELECT nome, url FROM proxies
            WHERE ativo = 1 AND (quarentena_ate IS NULL OR quarentena_ate <= :agora) AND {_USO} < cota_diaria
              AND (:nome IS NULL OR nome = :nome)
            ORDER BY {_USO}, coalesce(ultimo_uso_em, ''), nome LIMIT 1""",
        {"agora": _iso(agora), "hoje": _hoje(agora), "nome": nome},
    ).fetchone()
    return Saida(linha["nome"], linha["url"]) if linha else None


def registrar_uso(con: sqlite3.Connection, nome: str, agora: datetime) -> None:
    hoje = _hoje(agora)
    con.execute(
        f"UPDATE proxies SET uso_hoje = {_USO} + 1, dia_uso = :hoje, ultimo_uso_em = :agora WHERE nome = :nome",
        {"hoje": hoje, "agora": _iso(agora), "nome": nome},
    )


def registrar_sucesso(con: sqlite3.Connection, nome: str) -> None:
    con.execute(
        "UPDATE proxies SET sucessos = sucessos + 1, falhas_seguidas = 0, quarentenas = 0 WHERE nome = ?",
        (nome,),
    )


def registrar_falha(con: sqlite3.Connection, nome: str, cfg: dict, agora: datetime) -> datetime | None:
    """Conta a falha; depois de várias seguidas a saída vai para quarentena (rebaixamento automático)."""
    con.execute(
        "UPDATE proxies SET falhas = falhas + 1, falhas_seguidas = falhas_seguidas + 1 WHERE nome = ?",
        (nome,),
    )
    seguidas = con.execute("SELECT falhas_seguidas FROM proxies WHERE nome = ?", (nome,)).fetchone()[0]
    return quarentenar(con, nome, cfg, agora) if seguidas >= FALHAS_PARA_QUARENTENA else None


def quarentenar(con: sqlite3.Connection, nome: str, cfg: dict, agora: datetime) -> datetime:
    """Quarentena crescente: inicial × 2^(quarentenas seguidas), até o máximo. Devolve o fim."""
    d = cfg["disjuntor"]
    n = con.execute("SELECT quarentenas FROM proxies WHERE nome = ?", (nome,)).fetchone()[0]
    fim = agora + timedelta(hours=min(d["quarentena_inicial_h"] * 2**n, d["quarentena_max_h"]))
    con.execute(
        "UPDATE proxies SET quarentena_ate = ?, quarentenas = quarentenas + 1, falhas_seguidas = 0 WHERE nome = ?",
        (_iso(fim), nome),
    )
    return fim


def estado(con: sqlite3.Connection, agora: datetime) -> list[dict]:
    hoje = _hoje(agora)
    return [
        {**dict(r), "url": mascarar(r["url"]), "uso_hoje": r["uso_efetivo"]}
        for r in con.execute(
            f"SELECT *, {_USO} AS uso_efetivo FROM proxies WHERE ativo = 1 ORDER BY nome != 'direta', nome",
            {"hoje": hoje},
        )
    ]


def proxima_liberacao(con: sqlite3.Connection, agora: datetime, nome: str | None = None) -> datetime | None:
    """Quando alguma saída (ou a saída `nome`) volta a ficar disponível: fim da quarentena ou virada do dia."""
    fins = [
        datetime.fromisoformat(r[0])
        for r in con.execute(
            "SELECT quarentena_ate FROM proxies WHERE ativo = 1 AND quarentena_ate > ? AND (? IS NULL OR nome = ?)",
            (_iso(agora), nome, nome),
        )
    ]
    local = agora.astimezone()
    amanha = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), local.tzinfo)
    return min([*fins, amanha])
