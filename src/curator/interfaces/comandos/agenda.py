"""`curator agenda`: liga e desliga as tarefas diárias no systemd do usuário (#103). Nada roda sem `ligar`."""

from pathlib import Path

import typer

from curator import agenda
from curator.interfaces.cli import app
from curator.interfaces.comum import falhar

agenda_app = typer.Typer(
    help="Tarefas diárias opcionais (systemd do usuário, sem root): sincronizar e analisar.",
    no_args_is_help=True,
)
app.add_typer(agenda_app, name="agenda")

TAREFA = typer.Argument(..., help=f"Uma de: {', '.join(agenda.TAREFAS)}.")


def _tarefa(nome: str) -> str:
    if nome not in agenda.TAREFAS:
        falhar(f"tarefa '{nome}' não existe (use {' ou '.join(agenda.TAREFAS)})")
    return nome


@agenda_app.command("ligar")
def ligar(
    nome: str = TAREFA,
    hora: str | None = typer.Option(
        None, "--hora", help="HH:MM local (padrão: sincronizar 00:45, analisar 01:30)."
    ),
    uv: Path | None = typer.Option(None, "--uv", help="Caminho do uv (padrão: o do PATH)."),
    mostrar: bool = typer.Option(False, "--mostrar", help="Só mostra os arquivos, sem instalar nada."),
    forcar: bool = typer.Option(False, "--forcar", help="Substitui unidades instaladas à mão."),
) -> None:
    """Instala e ativa a tarefa diária. Rodar de novo atualiza a hora e os caminhos."""
    nome = _tarefa(nome)
    try:
        if mostrar:
            hora_ = agenda.validar_hora(hora or agenda.TAREFAS[nome].hora)
            for arq, texto in agenda.unidades(nome, hora_, agenda.clone(), uv or agenda.achar_uv()).items():
                typer.echo(f"--- {agenda.dir_units() / arq}\n{texto}")
            return
        gravados = agenda.ligar(nome, hora, uv=uv, forcar=forcar)
        e = agenda.estado(nome)
    except agenda.ErroAgenda as e:
        falhar(str(e))
    typer.echo(
        f"{nome}: ligada, todo dia às {e.get('hora')} (até {agenda.TAREFAS[nome].atraso_min} min depois)"
    )
    for p in gravados:
        typer.echo(f"  {p}")
    if e.get("proxima"):
        typer.echo(f"próxima: {e['proxima']} · logs: journalctl --user -u curator-{nome}")


@agenda_app.command("desligar")
def desligar(
    nome: str = TAREFA,
    forcar: bool = typer.Option(False, "--forcar", help="Desliga e apaga também unidades instaladas à mão."),
) -> None:
    """Desativa o timer e apaga os arquivos. Uma execução em andamento termina normalmente."""
    nome = _tarefa(nome)
    try:
        apagados = agenda.desligar(nome, forcar=forcar)
    except agenda.ErroAgenda as e:
        falhar(str(e))
    typer.echo(f"{nome}: desligada" + ("" if apagados else " (não estava instalada)"))
    for p in apagados:
        typer.echo(f"  apagado: {p}")


def _data(valor: str | None) -> str:
    return valor if valor and valor not in ("n/a", "0") else "nunca"


@agenda_app.command("status")
def status() -> None:
    """O que está ligado, a próxima e a última execução e como a última terminou."""
    for nome in agenda.TAREFAS:
        try:
            e = agenda.estado(nome)
        except agenda.ErroAgenda as erro:
            falhar(str(erro))
        if not e["instalada"]:
            typer.echo(f"{nome:<12} desligada (`curator agenda ligar {nome}`)")
            continue
        situacao = "ligada" if e["ativa"] and e["habilitada"] else "instalada, mas o timer está parado"
        origem = "" if e["gerada_pela_agenda"] else " · instalada à mão"
        typer.echo(f"{nome:<12} {situacao}, às {e.get('hora', '?')}{origem}")
        ultima = _data(e["ultima"])
        if e["rodando"]:
            ultima += " (rodando agora)"
        elif ultima != "nunca":
            ultima += (
                " (ok)"
                if e["resultado"] == "success"
                else f" (falhou: {e['resultado']}, código {e['codigo']})"
            )
        typer.echo(f"{'':<12} próxima: {_data(e['proxima'])} · última: {ultima}")
