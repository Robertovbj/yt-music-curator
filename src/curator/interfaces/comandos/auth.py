"""`curator auth`: credenciais do YouTube Music (headers do navegador)."""

import sys
from pathlib import Path

import typer

from curator.config import caminhos, carregar_config
from curator.interfaces.cli import app
from curator.interfaces.comum import falhar

auth_app = typer.Typer(help="Credenciais do YouTube Music (headers do navegador).")
app.add_typer(auth_app, name="auth")

INSTRUCOES = """\
Como obter os headers:
  1. Abra https://music.youtube.com logado na sua conta, com as ferramentas de desenvolvedor (F12) → Rede.
  2. Filtre por "browse" e clique numa requisição POST para /youtubei/v1/browse (role a página se não houver).
  3. Copie os headers da REQUISIÇÃO (Firefox: "Headers da requisição" → "Bruto" → copiar;
     Chrome: clique direito → Copy → Copy request headers).
  4. Rode `curator auth --clipboard` (ou salve num arquivo e use `--headers ARQUIVO`, apagando-o depois).
"""


def cliente():
    """Cliente ytmusicapi autenticado; encerra com instruções se não houver credenciais."""
    from curator.coleta.cliente import CredencialAusente, abrir_cliente

    try:
        return abrir_cliente(caminhos().credenciais, carregar_config()["metadados"]["intervalo_s"])
    except CredencialAusente as e:
        falhar(str(e))


@auth_app.callback(invoke_without_command=True)
def auth(
    ctx: typer.Context,
    headers: Path | None = typer.Option(
        None, "--headers", exists=True, dir_okay=False, help="Arquivo com os headers."
    ),
    clipboard: bool = typer.Option(False, "--clipboard", help="Lê os headers da área de transferência."),
) -> None:
    """Grava as credenciais a partir dos headers de uma requisição do music.youtube.com."""
    if ctx.invoked_subcommand:
        return
    from curator.coleta.cliente import configurar, ler_area_de_transferencia

    if headers:
        bruto = headers.read_text(encoding="utf-8")
    elif clipboard:
        bruto = ler_area_de_transferencia()
    else:
        typer.echo(INSTRUCOES)
        if sys.stdin.isatty():
            typer.echo("Cole os headers e termine com Ctrl-D:")
        bruto = sys.stdin.read()
    destino = caminhos().credenciais
    try:
        configurar(bruto, destino)
    except Exception as e:
        falhar(f"headers inválidos: {e}\n\n{INSTRUCOES}")
    typer.echo(f"credenciais gravadas em {destino} (permissão 600). Teste com `curator auth testar`.")


@auth_app.command("testar")
def auth_testar() -> None:
    """Confere a conta e o número de curtidas."""
    from curator.coleta.cliente import testar

    try:
        r = testar(cliente())
    except Exception as e:
        falhar(f"falha ao acessar o YouTube Music ({e}). Os headers podem ter expirado: rode `curator auth`.")
    typer.echo(f"conta: {r['conta']} · curtidas: {r['curtidas']}")
