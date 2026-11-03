"""Normalização de nomes e títulos (busca, ids de artista sem channelId, casamento entre provedores)."""

import re
import unicodedata

# Sufixos que o YouTube e outros provedores penduram no título e não identificam a música.
_RUIDO = re.compile(
    r"[\(\[][^\)\]]*(official|oficial|video|vídeo|audio|áudio|lyric|letra|visualizer|hd|4k|mv|m/v|remaster)"
    r"[^\)\]]*[\)\]]",
    re.IGNORECASE,
)
_FEAT = re.compile(r"[\(\[]?\s*\b(feat|ft|featuring|with)\b\.?.*$", re.IGNORECASE)


def sem_acentos(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def normalizar(s: str | None) -> str:
    """Minúsculas, sem acentos, só letras/dígitos separados por um espaço."""
    if not s:
        return ""
    s = sem_acentos(s).casefold()
    s = re.sub(r"[^\w]+", " ", s)
    return " ".join(s.split())


def normalizar_titulo(s: str | None) -> str:
    """Como `normalizar`, mas antes remove '(Official Video)', 'feat. X' e afins."""
    if not s:
        return ""
    s = _RUIDO.sub(" ", s)
    s = _FEAT.sub(" ", s)
    return normalizar(s)


def id_artista(artista_id: str | None, nome: str) -> str:
    return artista_id or f"nome:{normalizar(nome)}"
