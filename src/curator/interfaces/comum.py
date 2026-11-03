"""Utilidades compartilhadas pelos comandos da CLI."""

import sqlite3

import typer

from curator.banco.conexao import abrir
from curator.config import caminhos


def falhar(msg: str, codigo: int = 1) -> None:
    typer.secho(msg, fg=typer.colors.RED, err=True)
    raise typer.Exit(codigo)


def aviso(msg: str) -> None:
    typer.secho(f"  aviso: {msg}", fg=typer.colors.YELLOW, err=True)


def banco() -> sqlite3.Connection:
    return abrir(caminhos().banco)
