"""`curator hierarquia`: árvore mãe/filha das playlists."""

import json
from pathlib import Path

import typer

from curator.banco import consultas, hierarquia
from curator.config import caminhos
from curator.interfaces.cli import app
from curator.interfaces.comum import banco, falhar

hier_app = typer.Typer(help="Árvore mãe/filha das playlists (hierarquia.toml).", no_args_is_help=True)
app.add_typer(hier_app, name="hierarquia")


def emitir_toml(texto: str, salvar: bool) -> None:
    """Imprime o TOML ou grava em hierarquia.toml, sem nunca sobrescrever um existente."""
    if not salvar:
        typer.echo(texto)
        return
    destino = caminhos().hierarquia
    if destino.exists():
        falhar(f"{destino} já existe; não vou sobrescrever. Rode sem --salvar e mescle à mão.")
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(texto, encoding="utf-8")
    typer.echo(f"gravado em {destino}. Revise e rode `curator hierarquia aplicar`.")


@hier_app.command("mostrar")
def mostrar() -> None:
    """Árvore com o número de faixas de cada playlist."""
    typer.echo(hierarquia.arvore(banco()) or "nenhuma playlist própria; rode `curator sincronizar`")


@hier_app.command("aplicar")
def aplicar(
    arquivo: Path | None = typer.Option(
        None, "--arquivo", help="Padrão: ~/.config/yt-music-curator/hierarquia.toml"
    ),
) -> None:
    """Valida o hierarquia.toml e espelha no banco."""
    arquivo = arquivo or caminhos().hierarquia
    if not arquivo.exists():
        falhar(f"{arquivo} não existe; gere um com `curator hierarquia sugerir --salvar`")
    con = banco()
    try:
        n = hierarquia.aplicar(con, hierarquia.ler(arquivo))
    except hierarquia.HierarquiaInvalida as e:
        falhar(f"hierarquia inválida:\n{e}")
    typer.echo(f"{n} playlist(s) aplicada(s).\n")
    typer.echo(hierarquia.arvore(con))


@hier_app.command("sugerir")
def sugerir(
    salvar: bool = typer.Option(False, "--salvar", help="Grava em hierarquia.toml (se ainda não existir)."),
) -> None:
    """Propõe uma árvore a partir de títulos 'Prefixo - Nome'."""
    emitir_toml(hierarquia.sugerir(banco()), salvar)


@hier_app.command("verificar")
def verificar() -> None:
    """Confere se cada agregadora contém todas as faixas das filhas (sai com código 2 se não)."""
    con = banco()
    faltantes = hierarquia.faltantes_nas_agregadoras(con)
    if not faltantes:
        typer.echo("ok: todas as agregadoras contêm as faixas das filhas")
        return
    for mae, ids in faltantes.items():
        titulo = con.execute("SELECT titulo FROM playlists WHERE playlist_id = ?", (mae,)).fetchone()[0]
        typer.echo(f"{titulo}: faltam {len(ids)} faixa(s)")
        for linha in con.execute(
            f"SELECT titulo, artistas FROM faixas WHERE video_id IN {consultas.EM_LISTA}",
            (consultas.lista(ids),),
        ):
            nomes = ", ".join(a["nome"] for a in json.loads(linha["artistas"]))
            typer.echo(f"  - {linha['titulo']} — {nomes}")
    raise typer.Exit(2)


@hier_app.command("corrigir")
def corrigir() -> None:
    """Gera um plano que põe nas agregadoras as faixas das filhas que estão faltando."""
    from curator.motor import planos

    try:
        pid = planos.de_agregadoras(banco())
    except ValueError:
        typer.echo("ok: nada a corrigir")
        return
    typer.echo(
        f"plano #{pid} criado; confira com `curator plano mostrar {pid}` e aplique com `curator plano aplicar {pid}`"
    )
