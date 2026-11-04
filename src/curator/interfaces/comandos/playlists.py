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
        for r in con.execute(f"SELECT curtida FROM faixas WHERE video_id IN ({','.join('?' * len(fora))})", fora)
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
