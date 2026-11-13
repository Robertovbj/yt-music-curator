"""`curator playlists`: marcar playlists que não são de música como ignoradas (#77)."""

import typer

from curator.banco import consultas
from curator.interfaces.cli import app
from curator.interfaces.comum import banco, falhar

playlists_app = typer.Typer(
    help="Playlists ignoradas: os vídeos delas ficam fora de tudo (análises já feitas são guardadas).",
    no_args_is_help=True,
)
app.add_typer(playlists_app, name="playlists")


def _resolver(con, ref: str) -> tuple[str, str]:
    """Por id ou título exato, ignoradas incluídas (o `resolver_playlist` do motor não as enxerga)."""
    linha = con.execute(
        "SELECT playlist_id, titulo FROM playlists WHERE propria = 1 AND (playlist_id = ? OR titulo = ?) "
        "ORDER BY playlist_id = ? DESC LIMIT 1",
        (ref, ref, ref),
    ).fetchone()
    if linha is None:
        falhar(f"playlist '{ref}' não encontrada (use o id ou o título exato; `curator hierarquia mostrar`)")
    return linha[0], linha[1]


def _efeito(con, pid: str) -> str:
    """Quantos vídeos da playlist saem de tudo e quantos continuam por estarem numa playlist válida."""
    ids = [
        r[0]
        for r in con.execute("SELECT DISTINCT video_id FROM playlist_faixas WHERE playlist_id = ?", (pid,))
    ]
    if not ids:
        return "0 vídeos"
    marcas = ",".join("?" * len(ids))
    em_outras = {
        r[0]
        for r in con.execute(
            f"""SELECT DISTINCT pf.video_id FROM playlist_faixas pf JOIN playlists p USING (playlist_id)
                WHERE {consultas.PLAYLISTS_VALIDAS} AND p.playlist_id != ? AND pf.video_id IN ({marcas})""",
            [pid, *ids],
        )
    }
    fora = [v for v in ids if v not in em_outras]
    curtidos = sum(
        r[0]
        for r in con.execute(f"SELECT curtida FROM faixas WHERE video_id IN {consultas.EM_LISTA}", (consultas.lista(fora),))
    ) if fora else 0  # fmt: skip
    return (
        f"{len(ids)} vídeo(s): {len(fora)} fora de tudo ({curtidos} curtido(s)), "
        f"{len(em_outras)} continuam por estarem em outras playlists"
    )


@playlists_app.command("ignorar")
def ignorar(refs: list[str] = typer.Argument(..., help="Id ou título exato; aceita várias.")) -> None:
    """Tira a playlist de tudo, e os vídeos que só estão em playlists ignoradas (mesmo curtidos) também."""
    con = banco()
    for ref in refs:
        pid, titulo = _resolver(con, ref)
        con.execute("UPDATE playlists SET ignorada = 1 WHERE playlist_id = ?", (pid,))
        typer.echo(f"ignorada: {titulo} ({_efeito(con, pid)})")


@playlists_app.command("considerar")
def considerar(refs: list[str] = typer.Argument(..., help="Id ou título exato; aceita várias.")) -> None:
    """Desfaz o `ignorar`."""
    con = banco()
    for ref in refs:
        pid, titulo = _resolver(con, ref)
        con.execute("UPDATE playlists SET ignorada = 0 WHERE playlist_id = ?", (pid,))
        typer.echo(f"considerada de novo: {titulo}")


@playlists_app.command("ignoradas")
def ignoradas() -> None:
    """Lista as playlists ignoradas."""
    con = banco()
    linhas = con.execute(
        "SELECT playlist_id, titulo FROM playlists WHERE ignorada = 1 ORDER BY titulo"
    ).fetchall()
    if not linhas:
        typer.echo("nenhuma playlist ignorada")
        return
    for pid, titulo in linhas:
        typer.echo(f"  {titulo} ({_efeito(con, pid)})")
    typer.echo(f"{len(consultas.ignoradas(con))} vídeo(s) na lista negra")


@playlists_app.command("sugerir")
def sugerir(
    reagrupar: bool = typer.Option(
        False, "--reagrupar", help="Recalcula do zero (senão reaproveita o último)."
    ),
    min_cluster: int | None = typer.Option(
        None,
        "--min-cluster",
        help="Tamanho mínimo de cada playlist geral (padrão 40; menor = mais playlists).",
    ),
    quantas: int | None = typer.Option(
        None,
        "--quantas",
        help="Playlists master (#105): exatamente N playlists amplas, cobrindo todas as curtidas.",
    ),
    fronteira: float | None = typer.Option(
        None,
        "--fronteira",
        help="Com --quantas: quanto mais longe da própria master uma faixa pode estar para entrar também na "
        "segunda (padrão 0,02; 0 = só quem está mais perto da outra).",
    ),
    plano: bool = typer.Option(False, "--plano", help="Gera um plano que cria as playlists gerais."),
) -> None:
    """Playlists gerais a partir de TODAS as curtidas, ignorando as playlists que existem (#80).

    Com --quantas N, as playlists master (#105): N grupos exatos por consenso de vários UMAPs (cerca de 1 minuto);
    cada faixa fica na sua e, na fronteira, também na segunda.

    Só a matemática do projeto (clusters sobrepostos): o plano só cria playlists novas e não toca nas suas.
    Depois de aplicado, `curator classificar --nas-gerais` encaixa nelas as curtidas novas."""
    from curator.interfaces.comandos.organizar import _modulos

    clusters, _ = _modulos()
    from curator.motor import gerais

    con = banco()
    try:
        p = gerais.parametros(min_cluster, quantas, fronteira)
    except ValueError as e:
        falhar(str(e))
    typer.echo(
        f"agrupando as curtidas ({'consenso de UMAP + Ward' if p.quantas else 'UMAP + HDBSCAN'})…", err=True
    )
    try:
        aid, cs, novos = gerais.obter(con, p, reagrupar)
    except clusters.PoucasFaixas as e:
        falhar(str(e))
    if novos:
        typer.echo(f"{novos} curtida(s) nova(s) encaixada(s) nos clusters #{aid}", err=True)
    cobertas = {v for c in cs for v in c.membros}
    total = len(clusters._universo(con, clusters.CURTIDAS))
    tipo = "master" if p.quantas else "geral(is)"
    typer.echo(
        f"agrupamento #{aid}: {len(cs)} playlist(s) {tipo}, cobrindo {len(cobertas)} de {total} curtidas"
    )
    if p.quantas:
        duas = sum(len(c.membros) for c in cs) - len(cobertas)
        fr = f"{p.fronteira:g}".replace(".", ",")
        typer.echo(f"  {duas} faixa(s) na fronteira, em duas masters (--fronteira {fr})")
    for c in sorted(cs, key=lambda c: (-len(c.membros), c.cluster_id)):
        extra = f" (+{len(c.membros) - len(c.nucleo)} da fronteira)" if p.quantas else ""
        typer.echo(f"  [{c.cluster_id}] {c.nome} — {len(c.membros)} faixa(s){extra}")
    typer.echo('renomeie antes do plano com `curator clusters renomear N "nome" --gerais`', err=True)
    if plano:
        try:
            typer.echo(
                f"\nplano #{gerais.plano(con, aid, cs)} criado (só playlists novas); veja com `curator plano mostrar`"
            )
        except ValueError as e:
            falhar(str(e))
