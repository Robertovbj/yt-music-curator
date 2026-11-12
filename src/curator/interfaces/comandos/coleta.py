"""`curator sincronizar`: espelha curtidas, playlists e metadados no banco."""

import typer

from curator.config import carregar_config
from curator.interfaces.cli import app
from curator.interfaces.comandos.auth import cliente
from curator.interfaces.comum import aviso, banco

TRAVA = (
    "sincronizar.lock"  # uma sincronização por vez: o timer da agenda e uma manual não rodam juntos (#103)
)


def imprimir_resumo(titulo: str, resumo) -> None:
    partes = ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in resumo.contagens.items()) or "nada a fazer"
    typer.echo(f"{titulo}: {partes}")
    for a in resumo.avisos:
        aviso(a)


@app.command()
def sincronizar(
    forcar: bool = typer.Option(False, "--forcar", help="Ignora caches e rebaixa tudo."),
    sem_metadados: bool = typer.Option(False, "--sem-metadados", help="Pula a busca de dados de álbum."),
    sem_fila: bool = typer.Option(False, "--sem-fila", help="Não põe faixas novas na fila de análise."),
    sem_estatisticas: bool = typer.Option(
        False, "--sem-estatisticas", help="Pula visualizações e data de publicação (uma chamada por faixa)."
    ),
    sem_externos: bool = typer.Option(
        False, "--sem-externos", help="Pula os metadados do Deezer e do MusicBrainz (#88)."
    ),
) -> None:
    """Curtidas, playlists próprias, pertencimento, metadados de álbum, estatísticas e metadados externos."""
    from curator.audio.trava import OutroWorkerRodando, trava_worker
    from curator.config import caminhos

    try:
        with trava_worker(caminhos().dados_dir / TRAVA, quem="outro `curator sincronizar`"):
            _sincronizar(forcar, sem_metadados, sem_fila, sem_estatisticas, sem_externos)
    except OutroWorkerRodando as e:
        typer.echo(f"{e}; nada a fazer.")  # código 0: o timer da agenda (#103) não registra falha


def _sincronizar(forcar, sem_metadados, sem_fila, sem_estatisticas, sem_externos) -> None:
    from curator.coleta.sincronizar import (
        completar_estatisticas,
        completar_metadados,
        sincronizar_curtidas,
        sincronizar_playlists,
    )

    cfg = carregar_config()
    con, cli = banco(), cliente()
    imprimir_resumo("curtidas", sincronizar_curtidas(con, cli))
    imprimir_resumo(
        "playlists", sincronizar_playlists(con, cli, forcar=forcar, ttl_h=cfg["playlists"]["ttl_h"])
    )
    from curator.coleta.sincronizar import conferir_avaliacoes
    from curator.motor.recomendacoes import a_conferir

    rec = cfg["recomendacoes"]
    if rec["conferir_avaliacao"] and (
        ids := a_conferir(con, rec["conferir_por_rodada"], rec["conferir_validade_dias"])
    ):
        imprimir_resumo("avaliações das recomendadas", conferir_avaliacoes(con, cli, ids))
    if not sem_metadados:
        imprimir_resumo(
            "álbuns", completar_metadados(con, cli, ttl_dias=cfg["metadados"]["ttl_dias"], forcar=forcar)
        )
    if not sem_estatisticas:
        meta = cfg["metadados"]
        imprimir_resumo(
            "estatísticas das faixas",
            completar_estatisticas(
                con, cli, meta["estatisticas_por_rodada"], meta["estatisticas_ttl_dias"], forcar=forcar
            ),
        )
    if not sem_externos and (e := cfg["externos"])["por_rodada"]:
        from curator.coleta import externos

        c = externos.coletar(con, cfg, externos.clientes(cfg), e["fontes"], limite=e["por_rodada"])
        typer.echo(
            "metadados externos: " + (", ".join(f"{k} {v}" for k, v in sorted(c.items())) or "nada a fazer")
        )
    if not sem_fila:
        from curator.interfaces.comandos.analise import enfileirar

        typer.echo(f"fila de análise: {enfileirar(con)} faixa(s) nova(s)")
