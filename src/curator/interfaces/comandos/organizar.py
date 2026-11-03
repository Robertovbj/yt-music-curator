"""`curator reorganizar` e `curator clusters` (M5). Requerem o extra `organizar`."""

import json

import typer

from curator.interfaces.cli import app
from curator.interfaces.comum import aviso, banco, falhar
from curator.motor.receita import resolver_playlist

clusters_app = typer.Typer(help="Clusters sugeridos (UMAP + HDBSCAN).", no_args_is_help=True)
app.add_typer(clusters_app, name="clusters")


def _modulos():
    try:
        import sklearn  # noqa: F401
        import umap  # noqa: F401
    except ImportError:
        falhar("precisa do extra de organização: `uv sync --extra analise --extra organizar`")
    from curator.motor import clusters, reorganizar

    return clusters, reorganizar


def _escopo(con, mae: str | None) -> str:
    if mae is None:
        return "biblioteca"
    pid = resolver_playlist(con, mae)
    if pid is None:
        falhar(f"playlist '{mae}' não encontrada")
    return pid


@app.command()
def reorganizar(
    mae: str | None = typer.Option(
        None, "--mae", help="Só dentro desta playlist e das filhas (id ou título)."
    ),
    reagrupar: bool = typer.Option(
        False,
        "--reagrupar",
        help="Recalcula os clusters do zero (senão, usa o último e só encaixa faixas novas).",
    ),
    plano: bool = typer.Option(False, "--plano", help="Gera um plano com as sugestões de criar e completar."),
    min_cluster: int = typer.Option(12, "--min-cluster", help="Tamanho mínimo de um cluster."),
) -> None:
    """Sugere playlists novas, fusões e divisões a partir de clusters sobrepostos."""
    clusters, reorg = _modulos()
    from curator.motor import planos

    con = banco()
    escopo = _escopo(con, mae)
    p = clusters.Parametros(min_cluster_size=min_cluster)
    if reagrupar or clusters.ultimo(con, escopo) is None:
        typer.echo("agrupando (UMAP + HDBSCAN)…", err=True)
    try:
        aid, cs, novos = clusters.obter(con, escopo, p, reagrupar)
    except clusters.PoucasFaixas as e:
        falhar(str(e))
    if novos:
        typer.echo(f"{novos} faixa(s) nova(s) encaixada(s) nos clusters #{aid}", err=True)
    typer.echo(f"agrupamento #{aid}: {len(cs)} cluster(s)")
    for c in cs:
        typer.echo(f"  [{c.cluster_id}] {c.nome} — {len(c.membros)} faixa(s)")
    sugestoes = reorg.sugerir(con, cs, escopo, p.jaccard_fusao)
    if sugestoes:
        typer.echo("\nsugestões:")
    for s in sugestoes:
        typer.echo(f"  {s.tipo:<9} {s.texto}")
    if plano:
        try:
            pid = planos.de_reorganizacao(con, sugestoes, escopo)
            typer.echo(f"\nplano #{pid} criado; veja com `curator plano mostrar`")
        except ValueError as e:
            aviso(str(e))


@clusters_app.command("mostrar")
def clusters_mostrar(
    cluster_id: int | None = typer.Argument(None), mae: str | None = typer.Option(None, "--mae")
) -> None:
    """Estatísticas do último agrupamento (ou de um cluster)."""
    clusters, _ = _modulos()
    con = banco()
    aid = clusters.ultimo(con, _escopo(con, mae))
    if aid is None:
        falhar("nenhum agrupamento ainda; rode `curator reorganizar`")
    for c in clusters.carregar(con, aid):
        if cluster_id is None or c.cluster_id == cluster_id:
            typer.echo(f"[{c.cluster_id}] {c.nome} (limiar {c.limiar:.3f})")
            typer.echo(json.dumps(c.estatisticas, ensure_ascii=False, indent=2))


@clusters_app.command("renomear")
def clusters_renomear(cluster_id: int, nome: str, mae: str | None = typer.Option(None, "--mae")) -> None:
    """Dá um nome seu a um cluster; ele fica em cache pelo núcleo e sobrevive a reagrupamentos."""
    clusters, _ = _modulos()
    con = banco()
    aid = clusters.ultimo(con, _escopo(con, mae))
    if aid is None:
        falhar("nenhum agrupamento ainda")
    clusters.renomear(con, aid, cluster_id, nome)
    typer.echo(f"cluster [{cluster_id}] agora é “{nome}”")
