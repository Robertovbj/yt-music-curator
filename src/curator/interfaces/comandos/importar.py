"""`curator importar-helper`: traz a hierarquia do yt-music-helper."""

from pathlib import Path

import typer

from curator.interfaces.cli import app
from curator.interfaces.comandos.hierarquia import emitir_toml
from curator.interfaces.comum import aviso, banco


@app.command("importar-helper")
def importar_helper(
    banco_helper: Path = typer.Argument(
        ..., exists=True, dir_okay=False, help="cache_yt_music.db do yt-music-helper."
    ),
    salvar: bool = typer.Option(False, "--salvar", help="Grava em hierarquia.toml (se ainda não existir)."),
) -> None:
    """Converte a relação mãe/filha do yt-music-helper num trecho de hierarquia.toml."""
    from curator.banco.importar_helper import importar

    imp = importar(banco(), banco_helper)
    for antigo, atual, como in imp.casadas:
        typer.echo(f"  {antigo} → {atual} ({como})", err=True)
    for antigo, titulo in imp.sem_par:
        aviso(f"sem correspondência na biblioteca atual: {antigo} ({titulo})")
    emitir_toml(imp.toml, salvar)
