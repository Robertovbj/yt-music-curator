"""Candidatas de recomendação do YouTube Music (#64, ADR-13): rádio e "relacionadas" de várias sementes.

Só leitura no YT (ytmusicapi, a mesma conta da sincronização), com a pausa do `ClienteYT`. Cada resposta
vira uma coleta salva (snapshot) e cada faixa é gravada em `faixas`, fora da biblioteca. O que é da
biblioteca ou tem dislike é filtrado depois, no ranking (`motor/recomendar.py`), para o mesmo snapshot
servir aos escopos "novas" e "misturar".
"""

import logging
import re
import sqlite3

import numpy as np

from curator.banco import consultas
from curator.banco.conexao import transacao
from curator.coleta.cliente import ClienteYT
from curator.coleta.sincronizar import gravar_faixa
from curator.motor import espacos
from curator.motor import recomendacoes as rc
from curator.motor.receita import resolver_playlist
from curator.texto import normalizar

log = logging.getLogger(__name__)

TAMANHO_RADIO = 50


class SementeInvalida(ValueError):
    pass


# ------------------------------------------------------------------ sementes


def _mais_centrais(con: sqlite3.Connection, ids: list[str], k: int) -> list[str]:
    """As k faixas mais próximas do centroide (embedding); sem análise, as primeiras na ordem dada."""
    emb = espacos.embeddings(con)
    com = [v for v in ids if v in emb.indice]
    if not com:
        return ids[:k]
    E = np.stack([emb.linha(v) for v in com])
    c = E.mean(axis=0)
    c /= np.linalg.norm(c) or 1
    sims = E @ c
    ordem = sorted(range(len(com)), key=lambda i: (-float(sims[i]), com[i]))
    return [com[i] for i in ordem[:k]]


def sementes_de_playlist(con: sqlite3.Connection, ref: str, k: int) -> list[str]:
    pid = resolver_playlist(con, ref)
    if pid is None:
        raise SementeInvalida(f"playlist '{ref}' não encontrada (use o id ou o título exato)")
    dislikes = consultas.bloqueadas(con)
    ids = [
        r[0]
        for r in con.execute(
            "SELECT video_id FROM playlist_faixas WHERE playlist_id = ? ORDER BY posicao", (pid,)
        )
        if r[0] not in dislikes
    ]
    return _mais_centrais(con, list(dict.fromkeys(ids)), k)


def sementes_de_cluster(con: sqlite3.Connection, cluster_id: int, k: int) -> list[str]:
    from curator.motor import clusters

    aid = clusters.ultimo_geral(con)
    if aid is None:
        raise SementeInvalida("nenhum agrupamento ainda; rode `curator reorganizar`")
    dislikes = consultas.bloqueadas(con)
    ids = [
        r[0]
        for r in con.execute(
            "SELECT video_id FROM cluster_membros WHERE agrupamento_id = ? AND cluster_id = ? "
            "ORDER BY nucleo DESC, similaridade DESC, video_id",
            (aid, cluster_id),
        )
        if r[0] not in dislikes
    ]
    if not ids:
        raise SementeInvalida(f"cluster {cluster_id} não existe no agrupamento #{aid}")
    return ids[:k]


def sementes_de_artista(con: sqlite3.Connection, nome: str, k: int) -> list[str]:
    """As curtidas mais recentes do artista (nome sem acento/caixa), depois o resto da biblioteca."""
    alvo = normalizar(nome)
    dentro = set(consultas.utilizaveis(con))
    ids = [
        r[0]
        for r in con.execute(
            """SELECT f.video_id, a.nome FROM faixas f JOIN faixa_artistas fa USING (video_id)
               JOIN artistas a USING (artista_id) ORDER BY coalesce(f.posicao_curtida, 1e9), f.video_id"""
        )
        if r[0] in dentro and normalizar(r[1]) == alvo
    ]
    if not ids:
        raise SementeInvalida(f"artista '{nome}' não tem faixas na biblioteca")
    return list(dict.fromkeys(ids))[:k]


def resolver_sementes(
    con: sqlite3.Connection,
    faixas: list[str] | None = None,
    playlist: str | None = None,
    cluster: int | None = None,
    artista: str | None = None,
    k: int = 5,
) -> list[str]:
    """Junta as sementes dadas (faixas primeiro), sem repetir e sem dislikes. Determinístico."""
    dislikes = consultas.bloqueadas(con)
    out = [v for v in faixas or [] if v not in dislikes]
    if rejeitadas := [v for v in faixas or [] if v in dislikes]:
        log.info("sementes com dislike ou na lista negra descartadas: %s", rejeitadas)
    if playlist:
        out += sementes_de_playlist(con, playlist, k)
    if cluster is not None:
        out += sementes_de_cluster(con, cluster, k)
    if artista:
        out += sementes_de_artista(con, artista, k)
    out = list(dict.fromkeys(out))
    if not out:
        raise SementeInvalida("nenhuma semente: dê faixas, uma playlist, um cluster ou um artista")
    return out


# ------------------------------------------------------------------ coleta no YT


def _segundos(texto: str | None) -> int | None:
    if not texto or not re.fullmatch(r"\d+(:\d{1,2}){1,2}", texto):
        return None
    s = 0
    for parte in texto.split(":"):
        s = s * 60 + int(parte)
    return s


def _gravar(con: sqlite3.Connection, tracks: list[dict], excluir: str) -> list[str]:
    ids = []
    with transacao(con):
        for t in tracks:
            if not t.get("videoId") or t["videoId"] == excluir:
                continue
            if t.get("duration_seconds") is None and t.get("length"):
                t = {**t, "duration_seconds": _segundos(t["length"])}
            # rádio: só o LIKE é confiável (ver sincronizar.AVALIACAO)
            if vid := gravar_faixa(con, t, avaliacao_confiavel=False):
                ids.append(vid)
    return ids


def _relacionadas(cli: ClienteYT, browse_id: str) -> list[dict]:
    """A primeira seção de músicas ("You might also like"; o título muda com o idioma da conta)."""
    for secao in cli.chamar("get_song_related", browse_id) or []:
        musicas = [c for c in secao.get("contents") or [] if isinstance(c, dict) and c.get("videoId")]
        if musicas:
            return musicas
    return []


def coletar_semente(
    con: sqlite3.Connection, cli: ClienteYT, semente: str, fontes: list[str], cache_h: float
) -> list[int]:
    """Coletas (do cache ou novas) de uma semente nas fontes do YT pedidas. Uma chamada de rádio serve às duas."""
    quer = [f for f in ("yt_radio", "yt_related") if f in fontes]
    out, faltam = [], []
    for f in quer:
        cid = rc.coleta_recente(con, f, semente, cache_h)
        (out.append(cid) if cid else faltam.append(f))
    if not faltam:
        return out
    w = cli.chamar("get_watch_playlist", videoId=semente, radio=True, limit=TAMANHO_RADIO)
    if "yt_radio" in faltam:
        out.append(rc.salvar_coleta(con, "yt_radio", semente, _gravar(con, w.get("tracks") or [], semente)))
    if "yt_related" in faltam:
        tracks = _relacionadas(cli, w["related"]) if w.get("related") else []
        out.append(rc.salvar_coleta(con, "yt_related", semente, _gravar(con, tracks, semente)))
    return sorted(out)


def coletar(
    con: sqlite3.Connection, cli: ClienteYT, sementes: list[str], fontes: list[str], cache_h: float
) -> list[int]:
    out = []
    for s in sementes:
        try:
            out += coletar_semente(con, cli, s, fontes, cache_h)
        except Exception as e:  # uma semente ruim (vídeo indisponível) não derruba o pedido
            log.warning("coleta da semente %s falhou: %s", s, e)
    return sorted(set(out))
