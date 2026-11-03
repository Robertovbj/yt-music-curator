"""`curator sincronizar`: espelha curtidas, playlists e metadados no banco."""

import typer

from curator.config import carregar_config
from curator.interfaces.cli import app
from curator.interfaces.comandos.auth import cliente
from curator.interfaces.comum import aviso, banco


def imprimir_resumo(titulo: str, resumo) -> None:
    partes = ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in resumo.contagens.items()) or "nada a fazer"
    typer.echo(f"{titulo}: {partes}")
    for a in resumo.avisos:
        aviso(a)


@app.command()
def sincronizar(
    forcar: bool = typer.Option(False, "--forcar", help="Ignora caches e rebaixa tudo."),
    sem_metadados: bool = typer.Option(False, "--sem-metadados", help="Pula a busca de dados de álbum."),
) -> None:
    """Curtidas, playlists próprias, pertencimento e metadados de álbum."""
    from curator.coleta.sincronizar import completar_metadados, sincronizar_curtidas, sincronizar_playlists

    cfg = carregar_config()
    con, cli = banco(), cliente()
    imprimir_resumo("curtidas", sincronizar_curtidas(con, cli))
    imprimir_resumo(
        "playlists", sincronizar_playlists(con, cli, forcar=forcar, ttl_h=cfg["playlists"]["ttl_h"])
    )
    if not sem_metadados:
        imprimir_resumo(
            "álbuns", completar_metadados(con, cli, ttl_dias=cfg["metadados"]["ttl_dias"], forcar=forcar)
        )
