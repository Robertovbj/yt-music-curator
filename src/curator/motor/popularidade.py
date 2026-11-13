"""Popularidade, novidade e tipo de vídeo (#87), a partir do que o `sincronizar` guarda (#85).

- **Popularidade:** percentil do log das visualizações (de todo o YouTube, não as suas) na biblioteca utilizável.
  Vai de 0 (a menos vista da biblioteca) a 1 (a mais vista), com empates no meio (posto médio). Uma faixa de
  fora, como uma candidata de recomendação, é medida na régua da biblioteca. A escala log faz 1 mil → 10 mil
  pesar o mesmo que 1 milhão → 10 milhões.
- **Sem informação:** o percentil fica `None` (NaN nas matrizes). No perfil-alvo, isso custa um desvio, como
  qualquer feature ausente. Nos filtros `min`/`max`, a faixa sai. No filtro de tipo de vídeo, ela fica.
- **Tipo do vídeo:** o `videoType` do YouTube Music sem o prefixo, com nomes legíveis na receita.

Mesmos números no banco → mesmos percentis: nada aqui depende de ordem de leitura ou de relógio.
"""

import sqlite3

import numpy as np

from curator.banco import consultas
from curator.banco.consultas import EM_LISTA, lista

# nome na receita → `faixas.tipo_video` (videoType do YouTube Music sem MUSIC_VIDEO_TYPE_)
TIPOS_VIDEO = {
    "faixa": "ATV",  # faixa de áudio (capa do álbum), a "versão de estúdio"
    "clipe": "OMV",  # clipe oficial
    "oficial": "OFFICIAL_SOURCE_MUSIC",  # vídeo de um canal oficial que não é clipe (lyric video, ao vivo…)
    "usuario": "UGC",  # vídeo enviado por usuário (cover, montagem)
    "enviada": "PRIVATELY_OWNED_TRACK",  # música que o próprio usuário enviou ao YT Music
    "podcast": "PODCAST_EPISODE",
}
NOMES_TIPOS = {v: k for k, v in TIPOS_VIDEO.items()}


def _log_visualizacoes(con: sqlite3.Connection, ids: list[str]) -> dict[str, float]:
    out = {}
    for vid, n in con.execute(
        f"SELECT video_id, visualizacoes FROM faixas WHERE visualizacoes IS NOT NULL "
        f"AND video_id IN {EM_LISTA}",
        (lista(ids),),
    ):
        out[vid] = float(np.log10(max(int(n), 0) + 1))
    return out


def regua(con: sqlite3.Connection) -> np.ndarray:
    """log10(visualizações + 1) das faixas da biblioteca utilizável que têm o dado, em ordem crescente."""
    return np.sort(np.array(list(_log_visualizacoes(con, consultas.utilizaveis(con)).values()), np.float64))


def percentis(con: sqlite3.Connection, ids: list[str], base: np.ndarray | None = None) -> np.ndarray:
    """Percentil (0..1) de cada faixa na régua da biblioteca; NaN sem visualizações ou sem régua."""
    base = regua(con) if base is None else base
    logs = _log_visualizacoes(con, ids)
    out = np.full(len(ids), np.nan)
    if not len(base):
        return out
    for i, v in enumerate(ids):
        if v in logs:
            x = logs[v]
            abaixo = np.searchsorted(base, x, "left")
            ate = np.searchsorted(base, x, "right")
            out[i] = (abaixo + 0.5 * (ate - abaixo)) / len(base)
    return out


# desvio do percentil na própria biblioteca (o de uma uniforme, ≈ 0,29): a escala da popularidade no perfil-alvo
DESVIO = float(1 / np.sqrt(12))


def tipos(con: sqlite3.Connection, ids: list[str]) -> dict[str, str | None]:
    """video_id → nome legível do tipo do vídeo (None sem informação; o código cru se for um tipo novo)."""
    out = dict.fromkeys(ids)
    for vid, t in con.execute(
        f"SELECT video_id, tipo_video FROM faixas WHERE video_id IN {EM_LISTA}", (lista(ids),)
    ):
        out[vid] = NOMES_TIPOS.get(t, t) if t else None
    return out


def cobertura(con: sqlite3.Connection) -> dict[str, int]:
    """Quantas faixas da biblioteca utilizável têm cada dado (para a IA saber se vale pedir)."""
    ids = consultas.utilizaveis(con)

    def um(col: str) -> int:
        return con.execute(
            f"SELECT count(*) FROM faixas WHERE {col} IS NOT NULL AND video_id IN {EM_LISTA}", (lista(ids),)
        ).fetchone()[0]

    return {
        "biblioteca": len(ids),
        "com_visualizacoes": um("visualizacoes"),
        "com_data_de_publicacao": um("publicada_em"),
        "com_tipo_de_video": um("tipo_video"),
    }
