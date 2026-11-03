"""`curator visualizar`: o mapa interativo da biblioteca (M6). Requer o extra `organizar`."""

import shutil
import subprocess
from pathlib import Path

import typer

from curator.config import caminhos
from curator.interfaces.cli import app
from curator.interfaces.comandos.organizar import _modulos
from curator.interfaces.comum import banco, falhar


@app.command()
def visualizar(
    saida: Path | None = typer.Option(
        None, "--saida", help="Padrão: ~/.local/share/yt-music-curator/mapa.html"
    ),
    abrir: bool = typer.Option(False, "--abrir", help="Abre no navegador."),
) -> None:
    """Gera o mapa interativo da biblioteca (HTML autocontido, abre offline)."""
    _modulos()
    try:
        import plotly  # noqa: F401
    except ImportError:
        falhar("precisa do extra de organização: `uv sync --extra analise --extra organizar`")
    from curator.visual import html

    destino = saida or caminhos().dados_dir / "mapa.html"
    typer.echo("calculando projeções (a primeira vez demora; depois fica em cache)…", err=True)
    html.gerar(banco(), destino)
    typer.echo(f"mapa gerado em {destino} ({destino.stat().st_size / 1e6:.1f} MB)")
    if abrir and shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", str(destino)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
