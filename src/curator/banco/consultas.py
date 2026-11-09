"""Consultas de biblioteca usadas pelo resto do sistema (ADR-03)."""

import sqlite3

# Playlists que o motor enxerga: próprias, nem de descobertas (criadas a partir de recomendações, ADR-13: o
# que só está nelas ainda não foi curtido) nem ignoradas (#77). Use com o alias `p`.
PLAYLISTS_VALIDAS = "p.propria = 1 AND p.descobertas = 0 AND p.ignorada = 0"

# Lista negra para tudo, mesmo curtidos:
# - as faixas ignoradas uma a uma (#100), estejam onde estiverem;
# - os vídeos de playlists ignoradas (#77) que não estão em nenhuma playlist válida. Quem também está numa playlist
#   válida continua valendo por ela.
IGNORADAS = f"""
    SELECT video_id FROM faixas_ignoradas
    UNION
    SELECT video_id FROM (
        SELECT pf.video_id FROM playlist_faixas pf JOIN playlists p USING (playlist_id) WHERE p.ignorada = 1
        EXCEPT
        SELECT pf.video_id FROM playlist_faixas pf JOIN playlists p USING (playlist_id) WHERE {PLAYLISTS_VALIDAS}
    )
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
    """Dislikes ∪ lista negra (faixas ignoradas e vídeos de playlists ignoradas): nunca entram em nada que o motor
    gera ou sugere."""
    return com_dislike(con) | ignoradas(con)


def hash_bloqueadas(con: sqlite3.Connection) -> str:
    """Muda quando entra ou sai um dislike ou uma faixa da lista negra (parte das chaves de cache do motor)."""
    import hashlib

    return hashlib.sha256("\n".join(sorted(bloqueadas(con))).encode()).hexdigest()[:16]


# ------------------------------------------------------------------ faixas ignoradas uma a uma (#100)


def ignorar_faixas(
    con: sqlite3.Connection, ids: list[str], motivo: str | None = None
) -> dict[str, list[str]]:
    """Põe as faixas na lista negra. Devolve {"ignoradas", "ja_estavam", "desconhecidas"}; o motivo de uma que já
    estava só muda se vier um novo."""
    from curator.banco.conexao import agora

    out: dict[str, list[str]] = {"ignoradas": [], "ja_estavam": [], "desconhecidas": []}
    for vid in dict.fromkeys(ids):
        if con.execute("SELECT 1 FROM faixas WHERE video_id = ?", (vid,)).fetchone() is None:
            out["desconhecidas"].append(vid)
        elif con.execute("SELECT 1 FROM faixas_ignoradas WHERE video_id = ?", (vid,)).fetchone():
            if motivo:
                con.execute("UPDATE faixas_ignoradas SET motivo = ? WHERE video_id = ?", (motivo, vid))
            out["ja_estavam"].append(vid)
        else:
            con.execute(
                "INSERT INTO faixas_ignoradas (video_id, motivo, criado_em) VALUES (?, ?, ?)",
                (vid, motivo, agora()),
            )
            out["ignoradas"].append(vid)
    return out


def considerar_faixas(con: sqlite3.Connection, ids: list[str]) -> dict[str, list[str]]:
    """Desfaz o `ignorar_faixas`. Devolve {"consideradas", "nao_estavam"}. Uma faixa que também está só em playlists
    ignoradas (#77) continua na lista negra por elas."""
    out: dict[str, list[str]] = {"consideradas": [], "nao_estavam": []}
    for vid in dict.fromkeys(ids):
        cur = con.execute("DELETE FROM faixas_ignoradas WHERE video_id = ?", (vid,))
        out["consideradas" if cur.rowcount else "nao_estavam"].append(vid)
    return out


def faixas_ignoradas(con: sqlite3.Connection) -> list[dict]:
    """As faixas ignoradas uma a uma, da mais recente para a mais antiga."""
    return [
        dict(r)
        for r in con.execute(
            """SELECT i.video_id, f.titulo, f.artistas, i.motivo, i.criado_em, f.curtida
               FROM faixas_ignoradas i JOIN faixas f USING (video_id) ORDER BY i.criado_em DESC, i.video_id"""
        )
    ]


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
        "faixas_ignoradas": um("SELECT count(*) FROM faixas_ignoradas"),
        "videos_ignorados": len(ignoradas(con)),
        "duracao_total_h": round(
            (um(f"SELECT coalesce(sum(duracao_s), 0) FROM faixas WHERE video_id IN {bib}") or 0) / 3600, 1
        ),
        "analise": analise,
        "playlists": playlists,
    }
