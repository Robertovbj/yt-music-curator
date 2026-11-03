"""`curator modelos`: download e verificação dos modelos Essentia."""

import typer

from curator.audio import modelos
from curator.config import caminhos
from curator.interfaces.cli import app
from curator.interfaces.comum import falhar

modelos_app = typer.Typer(help="Modelos Essentia (manifesto com SHA-256).", no_args_is_help=True)
app.add_typer(modelos_app, name="modelos")


@modelos_app.command("baixar")
def baixar() -> None:
    """Baixa os modelos que faltam ou estão corrompidos."""
    pasta = caminhos().modelos_dir
    try:
        n = modelos.baixar(pasta)
    except Exception as e:
        falhar(f"falha no download: {e}")
    typer.echo(f"{n} arquivo(s) baixado(s) em {pasta}; conjunto {modelos.hash_modelos()}")


@modelos_app.command("verificar")
def verificar() -> None:
    """Confere presença e SHA-256 de todos os arquivos do manifesto."""
    pasta = caminhos().modelos_dir
    problemas = modelos.verificar(pasta)
    if problemas:
        falhar("\n".join(problemas) + "\n\nrode `curator modelos baixar`")
    typer.echo(f"ok: {len(modelos.manifesto()[1])} modelos em {pasta}; conjunto {modelos.hash_modelos()}")
