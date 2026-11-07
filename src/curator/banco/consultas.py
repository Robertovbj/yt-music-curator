"""Consultas de biblioteca usadas pelo resto do sistema (ADR-03)."""

import sqlite3

# Playlists que o motor enxerga: próprias, nem de descobertas (criadas a partir de recomendações, ADR-13: o
# que só está nelas ainda não foi curtido) nem ignoradas (#77). Use com o alias `p`.
PLAYLISTS_VALIDAS = "p.propria = 1 AND p.descobertas = 0 AND p.ignorada = 0"

# Vídeos de playlists ignoradas (#77) que não estão em nenhuma playlist válida: lista negra para tudo,
# mesmo curtidos. Quem também está numa playlist válida continua valendo por ela.
IGNORADAS = f"""
    SELECT pf.video_id FROM playlist_faixas pf JOIN playlists p USING (playlist_id) WHERE p.ignorada = 1
    EXCEPT
    SELECT pf.video_id FROM playlist_faixas pf JOIN playlists p USING (playlist_id) WHERE {PLAYLISTS_VALIDAS}
"""

BIBLIOTECA = f"""
    SELECT video_id FROM (
        SELECT video_id FROM faixas WHERE curtida = 1
        UNION
        SELECT pf.video_id FROM playlist_faixas pf JOIN playlists p USING (playlist_id)
        WHERE {PLAYLISTS_VALIDAS}
    ) WHERE video_id NOT IN ({IGNORADAS})
"""


# Faixas com dislike (#61): continuam no banco com a análise (#59), mas nunca entram em playlists geradas,
# classificações, clusters nem recomendações.
DISLIKES = "SELECT video_id FROM faixas WHERE avaliacao = 'dislike'"


def _ids(con: sqlite3.Connection, sql: str, params=()) -> list[str]:
    return [r[0] for r in con.execute(sql, params)]


def biblioteca(con: sqlite3.Connection) -> list[str]:
    """Curtidas ∪ faixas de playlists próprias."""
    return _ids(con, f"SELECT video_id FROM ({BIBLIOTECA}) ORDER BY video_id")


def com_dislike(con: sqlite3.Connection) -> set[str]:
    return set(_ids(con, DISLIKES))


def ignoradas(con: sqlite3.Connection) -> set[str]:
    return set(_ids(con, IGNORADAS))


def bloqueadas(con: sqlite3.Connection) -> set[str]:
    """Dislikes ∪ vídeos de playlists ignoradas: nunca entram em nada que o motor gera ou sugere."""
    return com_dislike(con) | ignoradas(con)


def utilizaveis(con: sqlite3.Connection) -> list[str]:
    """A biblioteca sem as faixas com dislike: o que o motor pode usar."""
    return _ids(
        con, f"SELECT video_id FROM ({BIBLIOTECA}) WHERE video_id NOT IN ({DISLIKES}) ORDER BY video_id"
    )


def dislikes_em_playlists(con: sqlite3.Connection) -> dict[str, list[str]]:
    """playlist_id → faixas com dislike que ainda estão nela (para sugerir a remoção num plano)."""
    out: dict[str, list[str]] = {}
    for pid, vid in con.execute(
        f"""SELECT DISTINCT pf.playlist_id, pf.video_id FROM playlist_faixas pf JOIN playlists p USING (playlist_id)
            WHERE p.propria = 1 AND p.ignorada = 0 AND pf.video_id IN ({DISLIKES})
            ORDER BY pf.playlist_id, pf.video_id"""
    ):
        out.setdefault(pid, []).append(vid)
    return out


def nao_alocadas(con: sqlite3.Connection) -> list[str]:
    """Curtidas que não estão em nenhuma playlist própria, da curtida mais recente para a mais antiga."""
    return _ids(
        con,
        f"""SELECT f.video_id FROM faixas f WHERE f.curtida = 1 AND f.avaliacao != 'dislike'
            AND f.video_id NOT IN ({IGNORADAS}) AND NOT EXISTS (
               SELECT 1 FROM playlist_faixas pf JOIN playlists p USING (playlist_id)
               WHERE {PLAYLISTS_VALIDAS} AND pf.video_id = f.video_id)
           ORDER BY f.posicao_curtida""",
    )


def fora_das_gerais(con: sqlite3.Connection) -> list[str]:
    """Curtidas (sem dislike nem lista negra) que não estão em nenhuma playlist geral (#80), da mais recente para
    a mais antiga. As outras playlists não contam: as gerais ignoram o que já existe."""
    return _ids(
        con,
        f"""SELECT f.video_id FROM faixas f WHERE f.curtida = 1 AND f.avaliacao != 'dislike'
            AND f.video_id NOT IN ({IGNORADAS}) AND NOT EXISTS (
               SELECT 1 FROM playlist_faixas pf JOIN playlists p USING (playlist_id)
               WHERE p.geral = 1 AND p.ignorada = 0 AND pf.video_id = f.video_id)
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
               WHERE p.propria = 1 AND p.ignorada = 0 GROUP BY p.playlist_id ORDER BY p.titulo"""
        )
    ]
    return {
        "biblioteca": um(f"SELECT count(*) FROM {bib}"),
        "curtidas": um("SELECT count(*) FROM faixas WHERE curtida = 1"),
        "nao_alocadas": len(nao_alocadas(con)),
        "dislikes": um("SELECT count(*) FROM faixas WHERE avaliacao = 'dislike'"),
        "dislikes_em_playlists": sum(len(v) for v in dislikes_em_playlists(con).values()),
        "com_estatisticas": um(
            f"SELECT count(*) FROM faixas WHERE estatisticas_em IS NOT NULL AND video_id IN {bib}"
        ),
        "analisadas_fora_da_biblioteca": um(
            f"SELECT count(*) FROM analises WHERE status = 'ok' AND video_id NOT IN {bib}"
        ),
        # análises que vieram de outra pessoa (#96); fora da biblioteca, elas são músicas NOVAS para você
        "analises_importadas": um("SELECT count(*) FROM analises WHERE importacao_id IS NOT NULL"),
        "importadas_na_biblioteca": um(
            f"SELECT count(*) FROM analises WHERE importacao_id IS NOT NULL AND video_id IN {bib}"
        ),
        "playlists_proprias": len(playlists),
        "playlists_ignoradas": um("SELECT count(*) FROM playlists WHERE ignorada = 1"),
        "videos_ignorados": len(ignoradas(con)),
        "duracao_total_h": round(
            (um(f"SELECT coalesce(sum(duracao_s), 0) FROM faixas WHERE video_id IN {bib}") or 0) / 3600, 1
        ),
        "analise": analise,
        "playlists": playlists,
    }
