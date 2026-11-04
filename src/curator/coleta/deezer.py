"""O Deezer como fonte extra de candidatas (#65, ADR-13): artistas relacionados aos das sementes.

API pública e sem chave (https://api.deezer.com). O Deezer só diz *quais* músicas; cada uma é casada com
um video_id do YouTube Music pela busca do ytmusicapi, com as regras do ADR-08 (título, artista, duração
±3 s, sem variantes como cover/remix). Sem casamento confiável, a faixa é descartada. Os casamentos ficam
em `deezer_casamentos`, inclusive os que falharam.
"""

import json
import logging
import sqlite3
import time
import urllib.parse
import urllib.request

from curator.audio.casamento import score as score_casamento
from curator.audio.provedores.base import FaixaAlvo
from curator.banco.conexao import agora
from curator.coleta.cliente import ClienteYT
from curator.coleta.sincronizar import gravar_faixa
from curator.motor import recomendacoes as rc
from curator.texto import normalizar

log = logging.getLogger(__name__)

API = "https://api.deezer.com"


class ErroDeezer(RuntimeError):
    pass


class ClienteDeezer:
    """GET com pausa entre chamadas (o limite do Deezer é ~50 por 5 s) e erro claro quando a API recusa."""

    def __init__(self, intervalo_s: float = 0.25, dormir=time.sleep, abrir=None):
        self.intervalo_s = intervalo_s
        self._dormir = dormir
        self._abrir = abrir or urllib.request.urlopen
        self._ultima = 0.0

    def get(self, caminho: str, **params) -> dict:
        espera = self.intervalo_s - (time.monotonic() - self._ultima)
        if espera > 0:
            self._dormir(espera)
        url = f"{API}{caminho}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        req = urllib.request.Request(
            url, headers={"User-Agent": "yt-music-curator", "Accept": "application/json"}
        )
        try:
            with self._abrir(req, timeout=15) as r:
                dados = json.load(r)
        finally:
            self._ultima = time.monotonic()
        if isinstance(dados, dict) and "error" in dados:
            raise ErroDeezer(f"{caminho}: {dados['error']}")
        return dados


def _artista(con: sqlite3.Connection, video_id: str) -> str | None:
    linha = con.execute("SELECT artistas FROM faixas WHERE video_id = ?", (video_id,)).fetchone()
    artistas = json.loads(linha[0]) if linha else []
    return artistas[0]["nome"] if artistas else None


def _id_artista(dz: ClienteDeezer, nome: str) -> int | None:
    """Só aceita nome igual (sem acento/caixa): um artista errado traria recomendações de outro mundo."""
    alvo = normalizar(nome)
    for a in dz.get("/search/artist", q=nome, limit=5).get("data") or []:
        if normalizar(a.get("name") or "") == alvo:
            return a["id"]
    return None


def casar(
    con: sqlite3.Connection, cli: ClienteYT, faixa: dict, tolerancia_s: float, limiar: float
) -> str | None:
    """video_id do YT Music para uma faixa do Deezer, ou None. Usa e alimenta o cache de casamentos."""
    linha = con.execute(
        "SELECT video_id FROM deezer_casamentos WHERE deezer_id = ?", (faixa["id"],)
    ).fetchone()
    if linha:
        return linha[0]
    titulo, artista = faixa.get("title") or "", (faixa.get("artist") or {}).get("name") or ""
    alvo = FaixaAlvo("", titulo, [artista], faixa.get("duration"))
    melhor, melhor_s = None, 0.0
    for r in cli.chamar("search", f"{artista} {titulo}", filter="songs", limit=5) or []:
        if not r.get("videoId"):
            continue
        cand_artista = ((r.get("artists") or [{}])[0] or {}).get("name")
        s = score_casamento(alvo, r.get("title") or "", cand_artista, r.get("duration_seconds"), tolerancia_s)
        if s > melhor_s:
            melhor, melhor_s = r, s
    vid = gravar_faixa(con, melhor, avaliacao_confiavel=False) if melhor and melhor_s >= limiar else None
    con.execute(
        "INSERT OR REPLACE INTO deezer_casamentos (deezer_id, video_id, score, titulo, artista, casado_em) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (faixa["id"], vid, round(melhor_s, 4), titulo, artista, agora()),
    )
    return vid


def coletar_semente(
    con: sqlite3.Connection,
    cli: ClienteYT,
    dz: ClienteDeezer,
    semente: str,
    cache_h: float,
    n_artistas: int = 3,
    n_faixas: int = 3,
    tolerancia_s: float = 3,
    limiar: float = 0.82,
) -> int:
    """Coleta (do cache ou nova): as faixas mais tocadas dos artistas relacionados ao artista da semente."""
    if cid := rc.coleta_recente(con, "deezer", semente, cache_h):
        return cid
    itens: list[str] = []
    nome = _artista(con, semente)
    aid = _id_artista(dz, nome) if nome else None
    if aid is None:
        log.info("deezer: artista da semente %s (%s) não encontrado", semente, nome)
    else:
        for rel in (dz.get(f"/artist/{aid}/related", limit=n_artistas).get("data") or [])[:n_artistas]:
            for f in (dz.get(f"/artist/{rel['id']}/top", limit=n_faixas).get("data") or [])[:n_faixas]:
                if vid := casar(con, cli, f, tolerancia_s, limiar):
                    itens.append(vid)
    return rc.salvar_coleta(con, "deezer", semente, itens)


def coletar(
    con: sqlite3.Connection, cli: ClienteYT, dz: ClienteDeezer, sementes: list[str], cfg: dict
) -> list[int]:
    r, p = cfg["recomendacoes"], cfg["provedores"]
    out = []
    for s in sementes:
        try:
            out.append(coletar_semente(
                con, cli, dz, s, r["cache_h"], r["deezer_artistas"], r["deezer_faixas_por_artista"],
                p["tolerancia_duracao_s"], p["limiar_casamento"],
            ))  # fmt: skip
        except Exception as e:  # o Deezer fora do ar não derruba a recomendação: o YT continua
            log.warning("deezer: semente %s falhou: %s", s, e)
    return out
