"""Provedores de áudio: de onde baixar a faixa inteira (ADR-08.4)."""

import sqlite3

from curator.audio.provedores.base import ProvedorAudio


def montar(cfg: dict, con: sqlite3.Connection) -> dict[str, ProvedorAudio]:
    """Provedores habilitados, na ordem de `provedores.ordem` do config, depois do áudio guardado (#84)."""
    from curator.audio.guardados import Guardado
    from curator.audio.provedores.bandcamp import Bandcamp
    from curator.audio.provedores.pasta_local import PastaLocal
    from curator.audio.provedores.ytdlp import SoundCloud, YouTubeBusca, YouTubeMusic

    tol = cfg["provedores"]["tolerancia_duracao_s"]
    comum = dict(
        formato=cfg["audio"]["formato"],
        sleep_requests_s=cfg["fila"]["sleep_requests_s"],
        cookies=cfg["audio"]["cookies"],
        tolerancia_s=tol,
    )
    disponiveis = {
        "youtube_music": lambda: YouTubeMusic(**comum),
        "youtube_busca": lambda: YouTubeBusca(**comum),
        "soundcloud": lambda: SoundCloud(**comum),
        # o SoundCloud/Bandcamp não precisam do formato restrito do YouTube
        "bandcamp": lambda: Bandcamp(**{**comum, "formato": "bestaudio/best"}),
        "pasta_local": lambda: PastaLocal(con, tol) if cfg["pasta_local"]["caminho"] else None,
    }
    # o guardado vem sempre primeiro: sem arquivo da faixa, ele não devolve candidato e a fila segue
    out: dict[str, ProvedorAudio] = {"guardado": Guardado(con)}
    for nome in cfg["provedores"]["ordem"]:
        if nome not in disponiveis:
            raise ValueError(f"provedor desconhecido em provedores.ordem: {nome}")
        if (p := disponiveis[nome]()) is not None:
            out[nome] = p
    return out
