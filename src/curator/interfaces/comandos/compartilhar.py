"""`curator analises`: compartilhar as análises com outras pessoas (#96, banco/compartilhar.py)."""

from datetime import date
from pathlib import Path

import typer

from curator.banco import compartilhar
from curator.config import caminhos
from curator.interfaces.cli import app
from curator.interfaces.comum import banco, falhar

analises_app = typer.Typer(
    help="Exportar as suas análises e importar as de outra pessoa (sem curtidas, avaliações nem playlists).",
    no_args_is_help=True,
)
app.add_typer(analises_app, name="analises")


@analises_app.command("exportar")
def exportar(
    saida: Path | None = typer.Option(
        None,
        "--saida",
        help="Padrão: exportacoes/curator-analises-AAAA-MM-DD.zip na pasta de dados (fora do repo).",
    ),
    autor: str | None = typer.Option(
        None, "--autor", help="Seu nome no pacote, para quem importar (opcional)."
    ),
    so_biblioteca: bool = typer.Option(
        False,
        "--so-biblioteca",
        help="Só as faixas da biblioteca (sem as candidatas de recomendação analisadas).",
    ),
    com_importadas: bool = typer.Option(
        False, "--com-importadas", help="Repassa também as análises que você importou de outras pessoas."
    ),
) -> None:
    """Gera o pacote com as análises, os extras e os metadados públicos (só lê o banco)."""
    destino = (
        saida or caminhos().dados_dir / "exportacoes" / f"curator-analises-{date.today().isoformat()}.zip"
    )
    ex = compartilhar.exportar(banco(), destino, autor, so_biblioteca, com_importadas)
    c = ex.contagens
    typer.echo(
        f"pacote em {ex.caminho} ({ex.bytes / 1e6:.1f} MB): {c['analises']} análises, {c['analises_extras']} extras, "
        f"{c['externos_faixas']} metadados externos"
    )
    typer.echo(
        "vai junto: metadados públicos e análises. Não vai: curtidas, avaliações, playlists, áudio, caminhos."
    )


@analises_app.command("importar")
def importar(
    arquivo: Path = typer.Argument(
        ..., exists=True, dir_okay=False, help="O .zip exportado por outra pessoa."
    ),
    seco: bool = typer.Option(False, "--seco", help="Confere e conta, sem gravar nada."),
) -> None:
    """Confere o pacote e importa o que vale para o seu analisador. A sua análise local sempre vence."""
    from curator.audio.trava import OutroWorkerRodando, trava_worker

    try:
        with trava_worker(caminhos().dados_dir / "worker.lock"):
            res = compartilhar.importar(banco(), arquivo, seco=seco)
    except OutroWorkerRodando as e:
        falhar(f"{e}: espere o `analisar` terminar")
    except compartilhar.PacoteInvalido as e:
        falhar(f"pacote recusado, nada foi gravado: {e}")
    if not any(res.entrou.values()):
        typer.echo("nada a importar: " + ("o pacote já tinha entrado e não há nada novo nele" if res.de_novo
                                          else "o que o pacote tem já está aqui ou é de outra versão"))  # fmt: skip
    else:
        titulo = "a seco (nada gravado)" if res.seco else f"importação #{res.importacao_id}"
        if res.de_novo:
            titulo += " (pacote já importado antes: entrou só o que faltava)"
        typer.echo(f"{titulo}: " + ", ".join(f"{k}: {v}" for k, v in res.entrou.items() if v))
    if fora := {k: v for k, v in res.fora.items() if v}:
        typer.echo("ficou de fora: " + ", ".join(f"{k}: {v}" for k, v in fora.items()))
    typer.echo("as faixas importadas continuam NOVAS para você: não viram curtida, avaliação nem playlist.")


@analises_app.command("importadas")
def importadas() -> None:
    """As importações feitas, com quantas análises de cada uma continuam aqui."""
    lista = compartilhar.listar(banco())
    if not lista:
        typer.echo("nenhuma importação")
        return
    for i in lista:
        autor = f" de {i['autor']}" if i["autor"] else ""
        typer.echo(
            f"#{i['importacao_id']}{autor} · {i['arquivo']} · importada em {i['importado_em'][:10]} · "
            f"{i['analises_hoje']} análise(s) aqui"
        )


@analises_app.command("remover")
def remover(
    importacao_id: int = typer.Argument(..., help="O número em `curator analises importadas`."),
) -> None:
    """Desfaz uma importação: apaga só o que veio dela. Faixas da sua biblioteca voltam para a fila."""
    from curator.audio.trava import OutroWorkerRodando, trava_worker

    try:
        with trava_worker(caminhos().dados_dir / "worker.lock"):
            c = compartilhar.remover(banco(), importacao_id)
    except OutroWorkerRodando as e:
        falhar(f"{e}: espere o `analisar` terminar")
    except KeyError as e:
        falhar(str(e.args[0]))
    typer.echo(f"importação #{importacao_id} desfeita: " + ", ".join(f"{k}: {v}" for k, v in c.items()))
