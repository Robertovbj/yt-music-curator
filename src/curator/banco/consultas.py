"""Consultas de biblioteca usadas pelo resto do sistema (ADR-03)."""

import sqlite3

BIBLIOTECA = """
    SELECT video_id FROM faixas WHERE curtida = 1
    UNION
    SELECT pf.video_id FROM playlist_faixas pf JOIN playlists p USING (playlist_id) WHERE p.propria = 1
"""


def _ids(con: sqlite3.Connection, sql: str, params=()) -> list[str]:
    return [r[0] for r in con.execute(sql, params)]


def biblioteca(con: sqlite3.Connection) -> list[str]:
    """Curtidas ∪ faixas de playlists próprias."""
    return _ids(con, f"SELECT video_id FROM ({BIBLIOTECA}) ORDER BY video_id")


def nao_alocadas(con: sqlite3.Connection) -> list[str]:
    """Curtidas que não estão em nenhuma playlist própria, da curtida mais recente para a mais antiga."""
    return _ids(
        con,
        """SELECT f.video_id FROM faixas f WHERE f.curtida = 1 AND NOT EXISTS (
               SELECT 1 FROM playlist_faixas pf JOIN playlists p USING (playlist_id)
               WHERE p.propria = 1 AND pf.video_id = f.video_id)
           ORDER BY f.posicao_curtida""",
    )


def exclusivas_da_mae(con: sqlite3.Connection, mae: str) -> list[str]:
    """Na mãe e em nenhuma das filhas diretas (o conceito do yt-music-helper)."""
    return _ids(
        con,
        """SELECT DISTINCT pf.video_id FROM playlist_faixas pf WHERE pf.playlist_id = :mae
           AND pf.video_id NOT IN (
               SELECT f.video_id FROM playlist_faixas f JOIN playlists p USING (playlist_id) WHERE p.mae_id = :mae)
           ORDER BY pf.video_id""",
        {"mae": mae},
    )


def estatisticas(con: sqlite3.Connection) -> dict:
    um = lambda sql, p=(): con.execute(sql, p).fetchone()[0]  # noqa: E731
    bib = f"({BIBLIOTECA})"
    analise = dict(
        con.execute(
            f"""SELECT coalesce(a.status, 'sem análise'), count(*) FROM {bib} b
                LEFT JOIN analises a USING (video_id) GROUP BY 1"""
        ).fetchall()
    )
    playlists = [
        dict(r)
        for r in con.execute(
            """SELECT p.titulo, p.tipo, m.titulo AS mae, count(pf.video_id) AS faixas
               FROM playlists p LEFT JOIN playlists m ON m.playlist_id = p.mae_id
               LEFT JOIN playlist_faixas pf ON pf.playlist_id = p.playlist_id
               WHERE p.propria = 1 GROUP BY p.playlist_id ORDER BY p.titulo"""
        )
    ]
    return {
        "biblioteca": um(f"SELECT count(*) FROM {bib}"),
        "curtidas": um("SELECT count(*) FROM faixas WHERE curtida = 1"),
        "nao_alocadas": len(nao_alocadas(con)),
        "dislikes": um("SELECT count(*) FROM faixas WHERE avaliacao = 'dislike'"),
        "analisadas_fora_da_biblioteca": um(
            f"SELECT count(*) FROM analises WHERE status = 'ok' AND video_id NOT IN {bib}"
        ),
        "playlists_proprias": len(playlists),
        "duracao_total_h": round(
            (um(f"SELECT coalesce(sum(duracao_s), 0) FROM faixas WHERE video_id IN {bib}") or 0) / 3600, 1
        ),
        "analise": analise,
        "playlists": playlists,
    }
