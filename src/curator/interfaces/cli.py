"""CLI `curator`. Cada módulo em `comandos/` registra seus comandos neste `app`."""

import importlib
import logging
import pkgutil
import sys

import typer

from curator import __version__

app = typer.Typer(help="Organiza a biblioteca do YouTube Music com features de áudio.", no_args_is_help=True)


def _configurar_log(verboso: int) -> None:
    nivel = logging.WARNING if verboso == 0 else logging.INFO if verboso == 1 else logging.DEBUG
    logging.basicConfig(
        level=nivel,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _versao(valor: bool) -> None:
    if valor:
        typer.echo(f"yt-music-curator {__version__}")
        raise typer.Exit()


@app.callback()
def principal(
    verboso: int = typer.Option(0, "--verbose", "-v", count=True, help="-v para info, -vv para debug."),
    versao: bool = typer.Option(False, "--version", callback=_versao, is_eager=True, help="Mostra a versão."),
) -> None:
    _configurar_log(verboso)


def _carregar_comandos() -> None:
    from curator.interfaces import comandos

    for mod in sorted(pkgutil.iter_modules(comandos.__path__), key=lambda m: m.name):
        importlib.import_module(f"{comandos.__name__}.{mod.name}")


_carregar_comandos()
