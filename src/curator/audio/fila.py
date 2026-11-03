"""Fila de download/análise retomável e serial (ADR-08.2).

Estados: pendente → baixando → analisando → ok | erro (com backoff, volta a ser tentada) | sem-audio.
"""

import json
import random
import sqlite3
from datetime import UTC, datetime, timedelta

from curator.banco.conexao import agora as agora_iso
from curator.banco.consultas import BIBLIOTECA


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def enfileirar_biblioteca(con: sqlite3.Connection, versao: str, hash_modelos: str) -> int:
    """Põe na fila as faixas da biblioteca sem análise válida para esta versão e conjunto de modelos.

    `sem-audio` não volta sozinho (nenhum provedor tinha a faixa); use `repetir_sem_audio`.
    """
    momento = agora_iso()
    antes = con.execute("SELECT count(*) FROM fila_download").fetchone()[0]
    # análises antigas (outra versão/modelos) reabrem itens já concluídos
    con.execute(
        """UPDATE fila_download SET status = 'pendente', tentativas = 0, proxima_tentativa_em = NULL,
             provedores_tentados = '[]', atualizada_em = :m
           WHERE status = 'ok' AND video_id IN (
             SELECT video_id FROM analises WHERE status = 'ok' AND (versao_analisador != :v OR hash_modelos != :h))""",
        {"m": momento, "v": versao, "h": hash_modelos},
    )
    con.execute(
        f"""INSERT INTO fila_download (video_id, status, criada_em, atualizada_em)
            SELECT b.video_id, 'pendente', :m, :m FROM ({BIBLIOTECA}) b
            LEFT JOIN analises a USING (video_id)
            WHERE NOT EXISTS (SELECT 1 FROM fila_download f WHERE f.video_id = b.video_id)
              AND NOT (coalesce(a.status, '') = 'ok' AND a.versao_analisador = :v AND a.hash_modelos = :h)
              AND coalesce(a.status, '') != 'sem-audio'""",
        {"m": momento, "v": versao, "h": hash_modelos},
    )
    return con.execute("SELECT count(*) FROM fila_download").fetchone()[0] - antes


def repetir_sem_audio(con: sqlite3.Connection) -> int:
    cur = con.execute(
        "UPDATE fila_download SET status = 'pendente', tentativas = 0, provedores_tentados = '[]', "
        "proxima_tentativa_em = NULL, atualizada_em = ? WHERE status = 'sem-audio'",
        (agora_iso(),),
    )
    return cur.rowcount


def recuperar(con: sqlite3.Connection) -> int:
    """Itens que ficaram no meio (processo morto) voltam para pendente."""
    cur = con.execute(
        "UPDATE fila_download SET status = 'pendente', atualizada_em = ? WHERE status IN ('baixando', 'analisando')",
        (agora_iso(),),
    )
    return cur.rowcount


def proximo(con: sqlite3.Connection, agora: datetime, max_tentativas: int) -> sqlite3.Row | None:
    """Próximo item elegível: curtidas mais recentes primeiro, depois o resto da biblioteca."""
    return con.execute(
        """SELECT q.* FROM fila_download q JOIN faixas f USING (video_id)
           WHERE q.status IN ('pendente', 'erro') AND q.tentativas < ?
             AND (q.proxima_tentativa_em IS NULL OR q.proxima_tentativa_em <= ?)
           ORDER BY f.posicao_curtida IS NULL, f.posicao_curtida, q.video_id LIMIT 1""",
        (max_tentativas, _iso(agora)),
    ).fetchone()


def reservar(con: sqlite3.Connection, agora: datetime, max_tentativas: int) -> sqlite3.Row | None:
    """Como `proximo`, mas já marca o item como "baixando" no mesmo comando: dois workers em paralelo
    nunca pegam a mesma faixa (um UPDATE é atômico no SQLite)."""
    return con.execute(
        """UPDATE fila_download SET status = 'baixando', atualizada_em = :m WHERE video_id = (
             SELECT q.video_id FROM fila_download q JOIN faixas f USING (video_id)
             WHERE q.status IN ('pendente', 'erro') AND q.tentativas < :max
               AND (q.proxima_tentativa_em IS NULL OR q.proxima_tentativa_em <= :agora)
             ORDER BY f.posicao_curtida IS NULL, f.posicao_curtida, q.video_id LIMIT 1)
           RETURNING *""",
        {"m": agora_iso(), "max": max_tentativas, "agora": _iso(agora)},
    ).fetchone()


def marcar(con: sqlite3.Connection, video_id: str, status: str, **campos) -> None:
    if "provedores_tentados" in campos:
        campos["provedores_tentados"] = json.dumps(campos["provedores_tentados"])
    sets = ", ".join(f"{k} = :{k}" for k in ["status", *campos])
    con.execute(
        f"UPDATE fila_download SET {sets}, atualizada_em = :_m WHERE video_id = :_v",
        {"status": status, **campos, "_m": agora_iso(), "_v": video_id},
    )


def backoff(tentativa: int, base_s: float, agora: datetime, rng: random.Random) -> datetime:
    """base × 2^(tentativa-1) com jitter de ±20%, no máximo 24 h."""
    atraso = min(base_s * 2 ** max(tentativa - 1, 0), 24 * 3600) * rng.uniform(0.8, 1.2)
    return agora + timedelta(seconds=atraso)


def falhar(
    con: sqlite3.Connection,
    video_id: str,
    tipo: str,
    erro: str,
    cfg: dict,
    agora: datetime,
    rng: random.Random,
) -> None:
    tentativas = (
        con.execute("SELECT tentativas FROM fila_download WHERE video_id = ?", (video_id,)).fetchone()[0] + 1
    )
    marcar(
        con, video_id, "erro", tentativas=tentativas, tipo_erro=tipo, ultimo_erro=erro[:1000],
        proxima_tentativa_em=_iso(backoff(tentativas, cfg["fila"]["backoff_base_s"], agora, rng)),
    )  # fmt: skip


def resumo(con: sqlite3.Connection, max_tentativas: int) -> dict:
    por_status = dict(con.execute("SELECT status, count(*) FROM fila_download GROUP BY status").fetchall())
    esgotados = con.execute(
        "SELECT count(*) FROM fila_download WHERE status = 'erro' AND tentativas >= ?", (max_tentativas,)
    ).fetchone()[0]
    erros = con.execute(
        "SELECT tipo_erro, count(*) FROM fila_download WHERE status = 'erro' GROUP BY tipo_erro"
    ).fetchall()
    return {"por_status": por_status, "esgotados": esgotados, "erros_por_tipo": dict(erros)}
