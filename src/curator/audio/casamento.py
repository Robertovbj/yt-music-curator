"""Score de casamento entre a faixa da biblioteca e um candidato de outro provedor (ADR-08.4).

Pesos: título 0,5 · artista 0,3 · duração 0,2. Duração fora de 3× a tolerância zera o score:
analisar a música errada é pior que não analisar.
"""

import re
from difflib import SequenceMatcher

from curator.audio.provedores.base import FaixaAlvo
from curator.texto import normalizar, normalizar_titulo

_TOPIC = re.compile(r"\s*-\s*topic$", re.IGNORECASE)


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def score(
    faixa: FaixaAlvo, titulo: str, artista: str | None, duracao_s: float | None, tolerancia_s: float = 3
) -> float:
    artistas = [normalizar(a) for a in faixa.artistas if a]
    cand_artista = normalizar(_TOPIC.sub("", artista or ""))
    cand_titulo = normalizar_titulo(titulo)

    # Títulos de upload costumam vir como "Artista - Música": tira o artista antes de comparar.
    for a in artistas:
        if a and cand_titulo.startswith(a + " "):
            cand_titulo = cand_titulo[len(a) + 1 :]
            cand_artista = cand_artista or a
            break

    s_titulo = _sim(normalizar_titulo(faixa.titulo), cand_titulo)
    s_artista = max((_sim(a, cand_artista) for a in artistas), default=0.0)
    if any(a and a in normalizar(titulo) for a in artistas):
        s_artista = max(s_artista, 1.0)

    if faixa.duracao_s and duracao_s:
        diff = abs(faixa.duracao_s - duracao_s)
        if diff > 3 * tolerancia_s:
            return 0.0
        s_dur = 1.0 if diff <= tolerancia_s else 1 - (diff - tolerancia_s) / (2 * tolerancia_s)
        return round(0.5 * s_titulo + 0.3 * s_artista + 0.2 * s_dur, 4)
    # Sem duração dos dois lados, a confiança é menor: penaliza.
    return round((0.6 * s_titulo + 0.4 * s_artista) * 0.9, 4)
