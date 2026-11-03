"""Bandcamp: busca pela página pública de pesquisa; o download fica com o yt-dlp (extrator `Bandcamp`).

A página de busca não traz a duração, então o score fica só com título e artista (com a penalidade de
"sem duração") e o worker confere a duração do áudio baixado antes de aceitar a análise.
"""

import html
import re
import urllib.parse
import urllib.request

from curator.audio import casamento
from curator.audio.provedores.base import Candidato, ErroTransitorio, FaixaAlvo, Saida
from curator.audio.provedores.ytdlp import BaseYtDlp

_ITEM = re.compile(r'<li class="searchresult[^"]*"(.*?)</li>', re.DOTALL)
_LINK = re.compile(r'<div class="heading">\s*<a href="([^"]+)"[^>]*>\s*(.*?)\s*</a>', re.DOTALL)
_POR = re.compile(r'<div class="subhead">.*?by\s+(.*?)\s*</div>', re.DOTALL)
_TIPO = re.compile(r'<div class="itemtype">\s*(\w+)\s*</div>')


def ler_resultados(pagina: str) -> list[tuple[str, str, str | None]]:
    """[(url, título, artista)] das faixas (itemtype TRACK) de uma página de busca."""
    out = []
    for bloco in _ITEM.findall(pagina):
        tipo = _TIPO.search(bloco)
        link = _LINK.search(bloco)
        if not link or (tipo and tipo[1].upper() != "TRACK"):
            continue
        por = _POR.search(bloco)
        url = html.unescape(link[1]).split("?", 1)[0]
        titulo = html.unescape(re.sub(r"<[^>]+>", "", link[2])).strip()
        artista = html.unescape(re.sub(r"<[^>]+>", "", por[1])).strip() if por else None
        out.append((url, titulo, artista))
    return out


class Bandcamp(BaseYtDlp):
    nome = "bandcamp"

    def __init__(self, *a, abrir_url=urllib.request.urlopen, **kw):
        super().__init__(*a, **kw)
        self._abrir = abrir_url

    def buscar(self, faixa: FaixaAlvo, saida: Saida | None) -> list[Candidato]:
        consulta = f"{' '.join(faixa.artistas[:1])} {faixa.titulo}".strip()
        url = "https://bandcamp.com/search?" + urllib.parse.urlencode({"q": consulta, "item_type": "t"})
        pedido = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})
        try:
            with self._abrir(pedido, timeout=30) as r:
                pagina = r.read().decode("utf-8", "replace")
        except Exception as e:
            raise ErroTransitorio(f"busca no Bandcamp falhou: {e}") from e
        candidatos = [
            Candidato(self.nome, u, t, a, None, casamento.score(faixa, t, a, None, self.tolerancia_s))
            for u, t, a in ler_resultados(pagina)[:5]
        ]
        return sorted(candidatos, key=lambda c: (-c.score, c.ref))
