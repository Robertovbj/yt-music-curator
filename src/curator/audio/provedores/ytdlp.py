"""Provedores baseados no yt-dlp: YouTube Music (pelo video_id), busca no YouTube e SoundCloud.

ADR-08.1: menor bitrate aceitável, sem conversão para WAV, sem cookies da conta principal,
uma instância de YoutubeDL por operação com a `proxy` da saída escolhida.
"""

import logging
import re
from pathlib import Path

from curator.audio import casamento
from curator.audio.provedores.base import (
    Arquivo,
    Candidato,
    ErroBloqueio,
    ErroIndisponivel,
    ErroTransitorio,
    FaixaAlvo,
    Saida,
)

log = logging.getLogger(__name__)

_BLOQUEIO = re.compile(
    r"not a bot|confirm you.re not|HTTP Error 429|Too Many Requests|rate.?limit", re.IGNORECASE
)
_INDISPONIVEL = re.compile(
    r"video unavailable|not available|available in your country|blocked it in your country|geo.?restrict|private video|has been removed|account .* terminated|"
    r"confirm your age|age.restricted|members.only|copyright|no video formats|requested format is not available|"
    r"HTTP Error 404|does not exist",
    re.IGNORECASE,
)


def classificar(e: Exception) -> Exception:
    """Traduz a mensagem do yt-dlp para o tipo de erro que a fila entende."""
    texto = str(e)
    if _BLOQUEIO.search(texto):
        return ErroBloqueio(texto)
    if _INDISPONIVEL.search(texto):
        return ErroIndisponivel(texto)
    return ErroTransitorio(texto)


class _Logger:
    """Manda as mensagens do yt-dlp para o logging, sem poluir o stdout."""

    def debug(self, msg):
        log.debug(msg)

    def info(self, msg):
        log.debug(msg)

    def warning(self, msg):
        log.info(msg)

    def error(self, msg):
        log.info(msg)


class BaseYtDlp:
    usa_rede = True

    def __init__(
        self, formato: str, sleep_requests_s: float = 1.0, cookies: str = "", tolerancia_s: float = 3
    ):
        self.formato = formato
        self.sleep_requests_s = sleep_requests_s
        self.cookies = cookies
        self.tolerancia_s = tolerancia_s

    def _opcoes(self, saida: Saida | None, **extra) -> dict:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "cachedir": False,
            "retries": 3,
            "fragment_retries": 3,
            "sleep_interval_requests": self.sleep_requests_s,
            "proxy": (saida.url if saida and saida.url else ""),  # "" = direta, ignorando proxy do ambiente
            "logger": _Logger(),
        }
        if self.cookies:
            opts["cookiefile"] = self.cookies
        opts.update(extra)
        return opts

    def _extrair(self, url: str, saida: Saida | None, download: bool, **extra) -> dict:
        from yt_dlp import YoutubeDL

        try:
            with YoutubeDL(self._opcoes(saida, **extra)) as ydl:
                info = ydl.extract_info(url, download=download)
                if download:
                    info["_arquivo"] = ydl.prepare_filename(info)
                return info
        except Exception as e:  # DownloadError, ExtractorError, URLError...
            raise classificar(e) from e

    def baixar(self, candidato: Candidato, destino: Path, saida: Saida | None) -> Arquivo:
        info = self._extrair(
            candidato.ref, saida, download=True, format=self.formato, outtmpl=str(destino / "audio.%(ext)s")
        )
        caminho = Path(info["_arquivo"])
        if not caminho.exists():  # algumas extrações trocam a extensão no fim
            achados = sorted(destino.glob("audio.*"))
            if not achados:
                raise ErroTransitorio(f"yt-dlp não gerou arquivo para {candidato.ref}")
            caminho = achados[0]
        return Arquivo(caminho, caminho.stat().st_size)

    def _buscar(self, consulta: str, faixa: FaixaAlvo, saida: Saida | None) -> list[Candidato]:
        info = self._extrair(consulta, saida, download=False, extract_flat="in_playlist")
        candidatos = []
        for e in info.get("entries") or []:
            url = e.get("url") or e.get("webpage_url")
            if not url or e.get("id") == faixa.video_id:
                continue
            artista = e.get("artist") or e.get("uploader") or e.get("channel")
            s = casamento.score(faixa, e.get("title") or "", artista, e.get("duration"), self.tolerancia_s)
            candidatos.append(Candidato(self.nome, url, e.get("title") or "", artista, e.get("duration"), s))
        return sorted(candidatos, key=lambda c: (-c.score, c.ref))


class YouTubeMusic(BaseYtDlp):
    """O próprio video_id da curtida: o mesmo áudio que você ouve."""

    nome = "youtube_music"

    def buscar(self, faixa: FaixaAlvo, saida: Saida | None) -> list[Candidato]:
        url = f"https://music.youtube.com/watch?v={faixa.video_id}"
        return [Candidato(self.nome, url, faixa.titulo, None, faixa.duracao_s, 1.0, exato=True)]


class YouTubeBusca(BaseYtDlp):
    """Outro vídeo com a mesma música (canal "- Topic", lyric video), casado por título, artista e duração."""

    nome = "youtube_busca"

    def buscar(self, faixa: FaixaAlvo, saida: Saida | None) -> list[Candidato]:
        consulta = f"{' '.join(faixa.artistas[:2])} {faixa.titulo}".strip()
        return self._buscar(f"ytsearch5:{consulta}", faixa, saida)


class SoundCloud(BaseYtDlp):
    nome = "soundcloud"

    def buscar(self, faixa: FaixaAlvo, saida: Saida | None) -> list[Candidato]:
        consulta = f"{' '.join(faixa.artistas[:1])} {faixa.titulo}".strip()
        return self._buscar(f"scsearch5:{consulta}", faixa, saida)
