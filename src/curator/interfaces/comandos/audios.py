"""`curator audios`: os áudios guardados para reanalisar sem baixar (#84, experimental)."""

import typer

from curator.audio import guardados
from curator.config import carregar_config
from curator.interfaces.cli import app
from curator.interfaces.comum import banco

audios_app = typer.Typer(
    help="Áudios guardados (experimental): com [audio] guardar = true, a reanálise não baixa de novo.",
    no_args_is_help=True,
)
app.add_typer(audios_app, name="audios")


def _tamanho(b: int) -> str:
    return f"{b / 1e9:.2f} GB" if b >= 1e9 else f"{b / 1e6:.1f} MB"


@audios_app.command("status")
def status() -> None:
    """Quantos áudios estão guardados, o espaço que ocupam e se a flag está ligada."""
    cfg, con = carregar_config(), banco()
    n, b = guardados.resumo(con)
    ligado = (
        "ligado"
        if cfg["audio"]["guardar"]
        else "desligado ([audio] guardar = true ou analisar --guardar-audio)"
    )
    typer.echo(f"guardar áudios: {ligado}")
    typer.echo(f"{n} áudio(s) guardado(s), {_tamanho(b)} em {guardados.pasta(cfg)}")


@audios_app.command("limpar")
def limpar(sim: bool = typer.Option(False, "--sim", help="Não pede confirmação.")) -> None:
    """Apaga todos os áudios guardados (as análises continuam; uma reanálise volta a baixar)."""
    con = banco()
    n, b = guardados.resumo(con)
    if not n:
        typer.echo("nenhum áudio guardado")
        return
    if not sim and not typer.confirm(f"apagar {n} áudio(s) guardado(s) ({_tamanho(b)})?"):
        raise typer.Exit(1)
    typer.echo(f"{guardados.limpar(con)} áudio(s) apagado(s)")
