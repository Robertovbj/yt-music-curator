"""`curator backup`: guardar e levar para outro computador os dados e as configurações."""

from datetime import date
from pathlib import Path

import typer

from curator.banco import backup
from curator.config import caminhos
from curator.interfaces.cli import app
from curator.interfaces.comum import aviso, falhar

backup_app = typer.Typer(
    help="Backup em zip do banco e das configurações (e restauração).", no_args_is_help=True
)
app.add_typer(backup_app, name="backup")


def _resumo(m: dict) -> str:
    c = m["contagens"]
    return (
        f"{c['faixas']} faixas ({c['curtidas']} curtidas), {c['analises_ok']} análises, {c['receitas']} receitas, "
        f"{c['planos']} planos · schema {m['schema']} · curator {m['versao_curator']} · {m['criado_em']} em {m['origem']}"
    )


@backup_app.command("criar")
def criar(
    saida: Path | None = typer.Option(None, "--saida", help="Padrão: ./yt-music-curator-AAAA-MM-DD.zip"),
    com_credenciais: bool = typer.Option(
        False, "--com-credenciais", help="Inclui browser.json e saidas.env."
    ),
) -> None:
    """Gera o zip (pode rodar com o worker gravando)."""
    destino = saida or Path(f"yt-music-curator-{date.today().isoformat()}.zip")
    try:
        m = backup.criar(destino, caminhos(), com_credenciais)
    except backup.BackupInvalido as e:
        falhar(str(e))
    if com_credenciais:
        aviso(
            "o zip contém os cookies da sua conta do YouTube e a chave do Tailscale: guarde-o com cuidado (permissão 600)"
        )
    typer.echo(f"backup em {destino} ({destino.stat().st_size / 1e6:.1f} MB)\n  {_resumo(m)}")


@backup_app.command("verificar")
def verificar(arquivo: Path = typer.Argument(..., exists=True, dir_okay=False)) -> None:
    """Confere os checksums e mostra o manifesto."""
    try:
        m, problemas = backup.verificar(arquivo)
    except backup.BackupInvalido as e:
        falhar(str(e))
    typer.echo(_resumo(m))
    typer.echo("arquivos: " + ", ".join(f"{n} ({i['bytes'] / 1e6:.1f} MB)" for n, i in m["arquivos"].items()))
    if problemas:
        falhar("problemas:\n  " + "\n  ".join(problemas))
    typer.echo("ok: todos os checksums conferem")
