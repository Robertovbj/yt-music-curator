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
    ErroPremium,
    ErroTransitorio,
    FaixaAlvo,
    Saida,
)

log = logging.getLogger(__name__)

_BLOQUEIO = re.compile(
    r"not a bot|confirm you.re not|HTTP Error 429|Too Many Requests|rate.?limit", re.IGNORECASE
)
_PREMIUM = re.compile(r"Premium members|requires (a )?(YouTube|Music) Premium", re.IGNORECASE)
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
    if _PREMIUM.search(texto):
        return ErroPremium(texto)
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
        from curator.audio.analisador import DURACAO_MAXIMA_S

        if candidato.duracao_s and candidato.duracao_s > DURACAO_MAXIMA_S:
            return self._baixar_comeco(candidato, destino, saida, DURACAO_MAXIMA_S)
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

    def _baixar_comeco(
        self, candidato: Candidato, destino: Path, saida: Saida | None, segundos: float
    ) -> Arquivo:
        """Só os primeiros `segundos` de uma faixa longa, que é o que a análise usa (um medley de 1h48 tem ~50 MB).

        Um pedido HTTP com Range pelo próprio yt-dlp, e não o `download_ranges` dele: aquele passa pelo ffmpeg,
        que não usa o proxy SOCKS da saída (e a URL do googlevideo só vale para o IP que a pediu). O arquivo
        truncado decodifica até onde vai.
        """
        from yt_dlp import YoutubeDL
        from yt_dlp.networking import Request

        try:
            with YoutubeDL(self._opcoes(saida, format=self.formato)) as ydl:
                info = ydl.extract_info(candidato.ref, download=False)
                duracao = info.get("duration") or candidato.duracao_s
                total = info.get("filesize") or info.get("filesize_approx")
                kbps = info.get("abr") or info.get("tbr") or 160
                limite = (
                    int(total * segundos / duracao * 1.05)
                    if total and duracao
                    else int(kbps * 125 * segundos * 1.1)
                )
                caminho = destino / f"audio.{info.get('ext') or 'webm'}"
                pedido = Request(
                    info["url"], headers={**(info.get("http_headers") or {}), "Range": f"bytes=0-{limite}"}
                )
                with ydl.urlopen(pedido) as resp, caminho.open("wb") as f:
                    while bloco := resp.read(1 << 16):
                        f.write(bloco)
        except Exception as e:  # DownloadError, ExtractorError, HTTPError...
            raise classificar(e) from e
        if not caminho.stat().st_size:
            raise ErroTransitorio(f"download parcial vazio para {candidato.ref}")
        return Arquivo(caminho, caminho.stat().st_size, parcial=True)

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
