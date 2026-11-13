"""`curator faixas`: ignorar faixas uma a uma (#100), como edições de meme ou covers de piada que você curtiu."""

import typer

from curator.banco import consultas
from curator.banco.conexao import transacao
from curator.interfaces.cli import app
from curator.interfaces.comum import banco, falhar

faixas_app = typer.Typer(
    help="Faixas ignoradas: ficam fora de tudo, sem mexer na conta do YouTube (a análise já feita é guardada).",
    no_args_is_help=True,
)
app.add_typer(faixas_app, name="faixas")


def _titulo(con, vid: str) -> str:
    nome = consultas.nomes_das_faixas(con, [vid]).get(vid)
    return f"{nome} ({vid})" if nome else vid


@faixas_app.command("ignorar")
def ignorar(
    video_ids: list[str] = typer.Argument(..., help="video_id; aceita vários."),
    motivo: str | None = typer.Option(None, "--motivo", help="Por que a faixa não conta (fica guardado)."),
) -> None:
    """Tira a faixa de tudo: biblioteca, receitas, classificação, clusters, mapa, recomendações, MCP, pacote
    compartilhado e rótulos de gênero. Continua curtida no YouTube; `considerar` desfaz."""
    con = banco()
    with transacao(con):
        r = consultas.ignorar_faixas(con, video_ids, motivo)
        if r["desconhecidas"]:
            falhar(f"não está(ão) no banco, nada foi gravado: {', '.join(r['desconhecidas'])}")
    for vid in r["ignoradas"]:
        typer.echo(f"ignorada: {_titulo(con, vid)}")
    for vid in r["ja_estavam"]:
        typer.echo(f"já estava ignorada: {_titulo(con, vid)}" + (" (motivo atualizado)" if motivo else ""))


@faixas_app.command("considerar")
def considerar(video_ids: list[str] = typer.Argument(..., help="video_id; aceita vários.")) -> None:
    """Desfaz o `ignorar`: a faixa volta ao normal."""
    con = banco()
    with transacao(con):
        r = consultas.considerar_faixas(con, video_ids)
    for vid in r["consideradas"]:
        typer.echo(f"considerada de novo: {_titulo(con, vid)}")
        if vid in consultas.ignoradas(con):
            typer.echo("  continua fora: só está em playlists ignoradas (`curator playlists ignoradas`)")
    for vid in r["nao_estavam"]:
        typer.echo(f"não estava ignorada: {_titulo(con, vid)}")


@faixas_app.command("ignoradas")
def ignoradas() -> None:
    """Lista as faixas ignoradas uma a uma, com o motivo."""
    con = banco()
    linhas = consultas.faixas_ignoradas(con)
    if not linhas:
        typer.echo("nenhuma faixa ignorada")
        return
    for r in linhas:
        typer.echo(f"  {_titulo(con, r['video_id'])}" + (f": {r['motivo']}" if r["motivo"] else ""))
    typer.echo(f"{len(linhas)} faixa(s) ignorada(s); {len(consultas.ignoradas(con))} vídeo(s) na lista negra")
