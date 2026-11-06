"""`curator externos`: metadados do Deezer e do MusicBrainz (#88, coleta/externos.py)."""

from pathlib import Path

import typer

from curator.config import carregar_config
from curator.interfaces.cli import app
from curator.interfaces.comum import banco, falhar

externos_app = typer.Typer(
    help="Metadados externos: gêneros, ISRC, BPM e popularidade (Deezer e MusicBrainz).",
    no_args_is_help=True,
)
app.add_typer(externos_app, name="externos")


@externos_app.command("coletar")
def coletar(
    limite: int | None = typer.Option(
        None, "--limite", "-n", help="Faixas por fonte (padrão: todas as pendentes)."
    ),
    fonte: list[str] = typer.Option(
        [], "--fonte", help="deezer ou musicbrainz (repita); padrão: [externos] fontes."
    ),
    forcar: bool = typer.Option(False, "--forcar", help="Busca de novo mesmo o que está no cache."),
    ids: Path | None = typer.Option(None, "--ids", help="Só estes video_ids (arquivo, um por linha)."),
    repetir: bool = typer.Option(
        False, "--repetir-sem-casamento", help="Tenta de novo as que não casaram, antes do ttl."
    ),
) -> None:
    """Busca as faixas da biblioteca sem dado (ou vencido) em cada fonte. Pode ser interrompido e retomado."""
    from curator.coleta import externos

    cfg, con = carregar_config(), banco()
    fontes = fonte or cfg["externos"]["fontes"]
    if desconhecidas := set(fontes) - set(externos.FONTES):
        falhar(f"fonte desconhecida: {', '.join(sorted(desconhecidas))}")
    cli = externos.clientes(cfg)
    lista = None
    if ids:
        lista = [x.split("#")[0].strip() for x in ids.read_text(encoding="utf-8").splitlines()]
        lista = [x for x in lista if x]
    c = externos.coletar(
        con, cfg, cli, fontes, limite=limite, forcar=forcar, ids=lista, repetir_sem_casamento=repetir,
        ao_processar=lambda f, t, s: typer.echo(f"{f}: {t} · {s}", err=True),
    )  # fmt: skip
    typer.echo(", ".join(f"{k}: {v}" for k, v in sorted(c.items())) or "nada pendente")


@externos_app.command("status")
def status() -> None:
    """Quantas faixas da biblioteca têm dado de cada fonte, e os gêneros externos mais comuns."""
    from curator.coleta import externos

    con = banco()
    for fonte, por in externos.resumo(con).items():
        typer.echo(f"{fonte}: " + (", ".join(f"{k} {v}" for k, v in sorted(por.items())) or "nada coletado"))
    top = con.execute(
        "SELECT fonte, rotulo, count(*) n FROM externos_generos WHERE nivel != 'faixa' OR fonte = 'deezer' "
        "GROUP BY fonte, rotulo ORDER BY n DESC LIMIT 12"
    ).fetchall()
    if top:
        typer.echo("mais comuns: " + ", ".join(f"{r['rotulo']} ({r['fonte']}, {r['n']})" for r in top))
