"""Importa a relação mãe/filha do banco do yt-music-helper como um trecho de `hierarquia.toml`.

O helper guardou as filhas com ids de 13 caracteres (ids de playlist têm 34), então o casamento com a
biblioteca atual tenta: id exato, depois id atual que começa com o id antigo, e por fim título normalizado.
"""

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from curator.banco.hierarquia import bloco_toml
from curator.texto import normalizar


@dataclass
class Importacao:
    toml: str = ""
    casadas: list[tuple[str, str, str]] = field(default_factory=list)  # (id antigo, id atual, como)
    sem_par: list[tuple[str, str]] = field(default_factory=list)  # (id antigo, título)


def _casar(con: sqlite3.Connection, antigo_id: str, titulo: str) -> tuple[sqlite3.Row | None, str]:
    exato = con.execute(
        "SELECT playlist_id, titulo FROM playlists WHERE propria = 1 AND playlist_id = ?", (antigo_id,)
    ).fetchone()
    if exato:
        return exato, "id exato"
    prefixo = con.execute(
        "SELECT playlist_id, titulo FROM playlists WHERE propria = 1 AND playlist_id LIKE ? || '%'",
        (antigo_id,),
    ).fetchall()
    if len(prefixo) == 1:
        return prefixo[0], "prefixo do id"
    por_titulo = [
        p
        for p in con.execute("SELECT playlist_id, titulo FROM playlists WHERE propria = 1")
        if normalizar(p["titulo"]) == normalizar(titulo)
    ]
    if len(por_titulo) == 1:
        return por_titulo[0], "título"
    return None, ""


def importar(con: sqlite3.Connection, banco_helper: Path) -> Importacao:
    antigo = sqlite3.connect(f"file:{banco_helper}?mode=ro", uri=True)
    antigo.row_factory = sqlite3.Row
    linhas = antigo.execute("SELECT playlist_id, titulo, mae_id FROM playlists").fetchall()
    antigo.close()

    imp = Importacao()
    atual_de: dict[str, sqlite3.Row] = {}
    for linha in linhas:
        par, como = _casar(con, linha["playlist_id"], linha["titulo"])
        if par is None:
            imp.sem_par.append((linha["playlist_id"], linha["titulo"]))
        else:
            atual_de[linha["playlist_id"]] = par
            imp.casadas.append((linha["playlist_id"], par["playlist_id"], como))

    maes = {linha["mae_id"] for linha in linhas if linha["mae_id"]}
    blocos = ["# Importado do yt-music-helper. Revise antes de `curator hierarquia aplicar`.\n"]
    for mae in sorted(maes):
        if mae not in atual_de:
            continue
        blocos.append(bloco_toml(atual_de[mae], tipo="agregadora"))
        for linha in linhas:
            if linha["mae_id"] == mae and linha["playlist_id"] in atual_de:
                blocos.append(bloco_toml(atual_de[linha["playlist_id"]], mae=atual_de[mae]["playlist_id"]))
    imp.toml = "\n".join(blocos)
    return imp
