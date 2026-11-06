"""`curator audios`: os áudios guardados para reanalisar sem baixar (#84, #90, experimentais)."""

from pathlib import Path

import typer

from curator.audio import guardados
from curator.config import caminhos, carregar_config
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
    from curator.audio import prebaixar

    if faltam := len(prebaixar.pendentes(con)):
        typer.echo(f"faixas da biblioteca ainda sem áudio guardado: {faltam} (`curator audios baixar`)")


@audios_app.command("baixar")
def baixar(
    limite: int | None = typer.Option(None, "--limite", "-n", help="Para depois de N faixas."),
    aguardar: bool = typer.Option(
        False, "--aguardar", help="Com saídas em quarentena ou sem cota, espera em vez de parar."
    ),
    primeiro: Path | None = typer.Option(
        None, "--primeiro", help="Arquivo com video_ids (um por linha) que passam na frente da fila."
    ),
) -> None:
    """Experimental (#90): só baixa e guarda o áudio da biblioteca, sem analisar (adianta uma reanálise).

    Usa a fonte da análise atual de cada faixa, as mesmas saídas, cotas e pausas do worker, e a mesma trava:
    enquanto roda, o `curator analisar` (e o timer) não roda."""
    from curator.audio import prebaixar, provedores, proxies
    from curator.audio.trava import OutroWorkerRodando, trava_worker
    from curator.audio.worker import Compartilhado, instalar_sinais

    cfg, con = carregar_config(), banco()
    na_frente = []
    if primeiro:
        linhas = (x.split("#")[0].strip() for x in primeiro.read_text(encoding="utf-8").splitlines())
        na_frente = [x for x in linhas if x]
    try:
        with trava_worker(caminhos().dados_dir / "worker.lock"):
            proxies.sincronizar_config(con, cfg)
            ids = prebaixar.pendentes(con, na_frente)
            typer.echo(f"{len(ids)} faixa(s) sem áudio guardado; destino: {guardados.pasta(cfg)}", err=True)
            comp = Compartilhado(limite)
            instalar_sinais(comp)
            c = prebaixar.baixar_em_paralelo(
                caminhos().banco, cfg, ids, provedores.montar, comp, aguardar=aguardar,
                ao_baixar=lambda p: typer.echo(p.linha(), err=True),
            )  # fmt: skip
    except OutroWorkerRodando as e:
        typer.echo(f"{e}; nada a fazer.")
        return
    n, b = guardados.resumo(con)
    typer.echo(
        ", ".join(f"{k}: {v}" for k, v in sorted(c.items())) + f" · total guardado: {n} ({_tamanho(b)})"
    )


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
