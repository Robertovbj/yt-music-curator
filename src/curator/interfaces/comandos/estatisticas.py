"""`curator estatisticas`: visão geral da biblioteca."""

import json

import typer

from curator.banco import consultas
from curator.interfaces.cli import app
from curator.interfaces.comum import banco


@app.command()
def estatisticas(como_json: bool = typer.Option(False, "--json", help="Saída em JSON.")) -> None:
    """Totais da biblioteca, não alocadas, faixas por playlist e estado da análise."""
    est = consultas.estatisticas(banco())
    if como_json:
        typer.echo(json.dumps(est, ensure_ascii=False, indent=2))
        return
    typer.echo(
        f"biblioteca: {est['biblioteca']} faixas ({est['duracao_total_h']} h) · curtidas: {est['curtidas']} · "
        f"não alocadas: {est['nao_alocadas']} · playlists próprias: {est['playlists_proprias']}"
    )
    if est["dislikes"]:
        typer.echo(f"com dislike (nunca usadas em playlists e recomendações): {est['dislikes']}")
    if est["analisadas_fora_da_biblioteca"]:
        typer.echo(f"analisadas fora da biblioteca (guardadas): {est['analisadas_fora_da_biblioteca']}")
    typer.echo("análise: " + (", ".join(f"{k}: {v}" for k, v in sorted(est["analise"].items())) or "—"))
    for p in est["playlists"]:
        agreg = " [agregadora]" if p["tipo"] == "agregadora" else ""
        mae = f"  ← {p['mae']}" if p["mae"] else ""
        typer.echo(f"  {p['faixas']:>5}  {p['titulo']}{agreg}{mae}")
