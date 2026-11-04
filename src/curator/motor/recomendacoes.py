"""Coletas e histórico das recomendações (#63, ADR-13). Nada aqui chama a rede.

Status de um item recomendado, recalculado a cada leitura (depois de `curator sincronizar`):
- `curtida`: o usuário curtiu (a faixa passou a fazer parte da biblioteca);
- `dislike`: rejeitada para sempre;
- `em_playlist`: o usuário pôs numa playlist própria comum (não nas de descobertas);
- `sem_reacao`: nada disso. Pode voltar numa recomendação futura, com penalidade (#62).
"""

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from curator.banco.conexao import agora, transacao

FONTES = ("yt_radio", "yt_related", "deezer")


# ------------------------------------------------------------------ coletas


def salvar_coleta(con: sqlite3.Connection, fonte: str, semente: str, itens: list[str]) -> int:
    cur = con.execute(
        "INSERT INTO coletas (fonte, semente, itens, coletada_em) VALUES (?, ?, ?, ?)",
        (fonte, semente, json.dumps(list(dict.fromkeys(itens))), agora()),
    )
    return cur.lastrowid


def coleta_recente(con: sqlite3.Connection, fonte: str, semente: str, cache_h: float) -> int | None:
    """A coleta mais nova da mesma semente e fonte, se feita há menos de `cache_h` horas."""
    limite = (datetime.now(UTC) - timedelta(hours=cache_h)).strftime("%Y-%m-%dT%H:%M:%SZ")
    linha = con.execute(
        "SELECT coleta_id FROM coletas WHERE fonte = ? AND semente = ? AND coletada_em >= ? "
        "ORDER BY coletada_em DESC, coleta_id DESC LIMIT 1",
        (fonte, semente, limite),
    ).fetchone()
    return linha[0] if linha else None


def ler_coletas(con: sqlite3.Connection, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    linhas = con.execute(
        f"SELECT * FROM coletas WHERE coleta_id IN ({','.join('?' * len(ids))}) ORDER BY coleta_id", ids
    )
    return [{**dict(r), "itens": json.loads(r["itens"])} for r in linhas]


# ------------------------------------------------------------------ recomendações


@dataclass
class ItemRecomendado:
    video_id: str
    score: float
    fontes: list[str]
    votos: int
    vezes_antes: int = 0
    conhecida: bool = False
    componentes: dict[str, float] = field(default_factory=dict)


def hash_spec(spec: dict) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def salvar(
    con: sqlite3.Connection,
    *,
    modo: str,
    escopo: str,
    spec: dict,
    sementes: list[str],
    coletas: list[int],
    itens: list[ItemRecomendado],
    pedido: str | None = None,
    status: str = "pronta",
) -> int:
    with transacao(con):
        cur = con.execute(
            """INSERT INTO recomendacoes (pedido, modo, escopo, status, sementes, spec, coletas, hash, criada_em,
                 concluida_em) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pedido, modo, escopo, status, json.dumps(sementes), json.dumps(spec, ensure_ascii=False),
             json.dumps(coletas), hash_spec(spec), agora(), agora() if status == "pronta" else None),
        )  # fmt: skip
        rid = cur.lastrowid
        _gravar_itens(con, rid, itens)
    return rid


def _gravar_itens(con: sqlite3.Connection, rid: int, itens: list[ItemRecomendado]) -> None:
    con.execute("DELETE FROM recomendacao_itens WHERE recomendacao_id = ?", (rid,))
    con.executemany(
        """INSERT INTO recomendacao_itens (recomendacao_id, posicao, video_id, score, fontes, votos, vezes_antes,
             conhecida) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [(rid, n, i.video_id, i.score, json.dumps(sorted(i.fontes)), i.votos, i.vezes_antes, int(i.conhecida))
         for n, i in enumerate(itens, start=1)],
    )  # fmt: skip


def concluir(con: sqlite3.Connection, rid: int, itens: list[ItemRecomendado]) -> None:
    """Grava o resultado final de uma recomendação que esperava análises (modo lento)."""
    with transacao(con):
        _gravar_itens(con, rid, itens)
        con.execute(
            "UPDATE recomendacoes SET status = 'pronta', concluida_em = ? WHERE recomendacao_id = ?",
            (agora(), rid),
        )


def obter(con: sqlite3.Connection, rid: int) -> dict | None:
    linha = con.execute("SELECT * FROM recomendacoes WHERE recomendacao_id = ?", (rid,)).fetchone()
    if linha is None:
        return None
    r = dict(linha)
    for k in ("sementes", "spec", "coletas"):
        r[k] = json.loads(r[k])
    return r


# ------------------------------------------------------------------ status e histórico


STATUS_SQL = """
    CASE
        WHEN f.avaliacao = 'dislike' THEN 'dislike'
        WHEN f.curtida = 1 THEN 'curtida'
        WHEN EXISTS (SELECT 1 FROM playlist_faixas pf JOIN playlists p USING (playlist_id)
                     WHERE pf.video_id = f.video_id AND p.propria = 1 AND p.descobertas = 0) THEN 'em_playlist'
        ELSE 'sem_reacao'
    END
"""


def itens(con: sqlite3.Connection, rid: int) -> list[dict]:
    """Itens de uma recomendação, na ordem, com o status atual."""
    linhas = con.execute(
        f"""SELECT i.*, f.titulo, f.artistas, {STATUS_SQL} AS status FROM recomendacao_itens i
            JOIN faixas f USING (video_id) WHERE i.recomendacao_id = ? ORDER BY i.posicao""",
        (rid,),
    )
    out = []
    for r in linhas:
        d = dict(r)
        d["fontes"] = json.loads(d["fontes"])
        d["artistas"] = [a["nome"] for a in json.loads(d["artistas"])]
        d["conhecida"] = bool(d["conhecida"])
        out.append(d)
    return out


@dataclass
class Historico:
    vezes: int  # recomendações anteriores (prontas) em que apareceu como nova
    ultima: str | None  # criada_em da mais recente


def historico(con: sqlite3.Connection, antes_de: str | None = None) -> dict[str, Historico]:
    """Por faixa: quantas vezes já foi recomendada como nova antes de `antes_de` (padrão: agora)."""
    antes_de = antes_de or "9999"
    out = {}
    for vid, n, ultima in con.execute(
        """SELECT i.video_id, count(*), max(r.criada_em) FROM recomendacao_itens i
           JOIN recomendacoes r USING (recomendacao_id)
           WHERE r.status = 'pronta' AND i.conhecida = 0 AND r.criada_em < ?
           GROUP BY i.video_id""",
        (antes_de,),
    ):
        out[vid] = Historico(n, ultima)
    return out


def listar(con: sqlite3.Connection, limite: int = 30) -> list[dict]:
    """Recomendações mais recentes primeiro, com quantos itens estão em cada status."""
    out = []
    for r in con.execute(
        "SELECT * FROM recomendacoes ORDER BY recomendacao_id DESC LIMIT ?", (limite,)
    ).fetchall():
        d = dict(r)
        d["sementes"] = json.loads(d["sementes"])
        d["spec"] = json.loads(d["spec"])
        d["coletas"] = json.loads(d["coletas"])
        contagem = {"curtida": 0, "dislike": 0, "em_playlist": 0, "sem_reacao": 0}
        for i in itens(con, d["recomendacao_id"]):
            if not i["conhecida"]:
                contagem[i["status"]] += 1
        d["status_itens"] = contagem
        out.append(d)
    return out
