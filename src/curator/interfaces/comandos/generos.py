"""`curator generos`: os seus rótulos de gênero-pai e o gênero aprendido com eles (#93, ADR-17)."""

import json
import tomllib
from pathlib import Path

import typer

from curator.interfaces.cli import app
from curator.interfaces.comum import banco, falhar
from curator.motor import aprendido

generos_app = typer.Typer(
    help="Rotular faixas por gênero-pai (é / não é Rock…) e ver o gênero aprendido. Nunca a partir das playlists.",
    no_args_is_help=True,
)
app.add_typer(generos_app, name="generos")


def _pai(con, pai: str) -> str:
    from curator.motor import generos

    pais = generos.Afinidades(con).pais
    if pai in pais:
        return pai
    perto = [p for p in pais if pai.lower() in p.lower()]
    falhar(f"gênero-pai '{pai}' não existe" + (f" (quis dizer: {', '.join(perto)}?)" if perto else ""))


def _titulo(con, vid: str) -> str:
    r = con.execute("SELECT titulo, artistas FROM faixas WHERE video_id = ?", (vid,)).fetchone()
    if r is None:
        return vid
    return f"{r['titulo']} — {', '.join(a['nome'] for a in json.loads(r['artistas'])[:2])}"


@generos_app.command("importar-gabarito")
def importar_gabarito(arquivo: Path = typer.Argument(..., exists=True, dir_okay=False)) -> None:
    """Traz o gabarito pessoal (TOML da #83) como rótulos: artistas → todas as faixas; faixas → uma a uma."""
    c = aprendido.importar_gabarito(banco(), tomllib.loads(arquivo.read_text(encoding="utf-8")))
    typer.echo(
        f"rótulos: {c['artista']} faixa(s) pelo artista, {c['faixa']} faixa a faixa"
        + (f" ({c['faixa_desconhecida']} id(s) que não estão no banco)" if c["faixa_desconhecida"] else "")
    )


@generos_app.command("status")
def status(
    validar: bool = typer.Option(
        False,
        "--validar",
        help="Mede o modelo de cada pai deixando o artista inteiro de fora (leva alguns segundos).",
    ),
) -> None:
    """Rótulos por pai e quais já têm o gênero aprendido (o resto fica com a régua do ADR-14)."""
    con = banco()
    linhas = aprendido.resumo(con)
    if not linhas:
        typer.echo(
            "nenhum rótulo ainda: `curator generos importar-gabarito` ou `curator generos rotular PAI`"
        )
        return
    for r in linhas:
        if r["modelo"]:
            extra = f"aprendido · {r['positivos']} × {r['negativos']} de {r['artistas']} artistas · núcleo {r['nucleo_aprendido']}"
        else:
            faltam = []
            if r["positivos"] < aprendido.MIN_POSITIVOS:
                faltam.append(f"{aprendido.MIN_POSITIVOS - r['positivos']} positivo(s)")
            if r["negativos"] < aprendido.MIN_NEGATIVOS:
                faltam.append(f"{aprendido.MIN_NEGATIVOS - r['negativos']} negativo(s)")
            if r["artistas"] < aprendido.MIN_ARTISTAS:
                faltam.append(f"{aprendido.MIN_ARTISTAS - r['artistas']} artista(s)")
            extra = "régua do ADR-14 (faltam " + ", ".join(faltam) + ")"
        typer.echo(f"  {r['pai']:<24} sim {r['sim']:>4} · não {r['nao']:>4} · {extra}")
        if validar and r["modelo"]:
            v = aprendido.validar(con, r["pai"])
            for nome, m in (("modelo", v), ("com a guarda", v["com_guarda"]), ("régua v2", v["v2"])):
                typer.echo(
                    f"  {'':<24} {nome:<13} AUC {m['auc']:.3f} · no 0,8 revocação {m['revocacao_0_8']:.2f}, "
                    f"falsos positivos {m['falsos_positivos_0_8']:.2f}"
                )
            typer.echo(
                f"  {'':<24} (artista de fora; a guarda deixou o modelo julgar {v['julgadas_pela_guarda']:.0%})"
            )


@generos_app.command("rotulo")
def rotulo(
    video_id: str,
    pai: str,
    valor: str = typer.Argument(..., help="sim, nao ou apagar"),
) -> None:
    """Marca uma faixa: é (sim) ou não é (nao) do gênero-pai. `apagar` tira o rótulo."""
    con = banco()
    pai = _pai(con, pai)
    if not con.execute("SELECT 1 FROM faixas WHERE video_id = ?", (video_id,)).fetchone():
        falhar(f"faixa {video_id} não está no banco")
    escolha = {"sim": True, "nao": False, "não": False, "apagar": None}
    if valor not in escolha:
        falhar("use sim, nao ou apagar")
    aprendido.rotular(con, video_id, pai, escolha[valor], "cli")
    typer.echo(f"{_titulo(con, video_id)}: {pai} = {valor}")


@generos_app.command("rotular")
def rotular(
    pai: str,
    n: int = typer.Option(10, "--n", "-n", help="Quantas faixas perguntar."),
) -> None:
    """Pergunta, uma a uma, as faixas mais duvidosas do gênero (as réguas discordam, ou perto do corte)."""
    con = banco()
    pai = _pai(con, pai)
    lista = aprendido.duvidosas(con, pai, n)
    if not lista:
        typer.echo("nenhuma faixa sem rótulo neste gênero")
        return
    feitas = 0
    for i, d in enumerate(lista, 1):
        notas = " · ".join(
            f"{k.removeprefix('afinidade_')} {v}" for k, v in d.items() if k.startswith("afinidade")
        )
        typer.echo(f"\n[{i}/{len(lista)}] {_titulo(con, d['video_id'])}\n  {d['motivo']}: {notas}")
        r = typer.prompt(f"  é {pai}? [s]im / [n]ão / [p]ular / [q] sair", default="p").strip().lower()
        if r.startswith("q"):
            break
        if r[:1] in ("s", "n"):
            aprendido.rotular(con, d["video_id"], pai, r.startswith("s"), "cli")
            feitas += 1
    typer.echo(f"\n{feitas} rótulo(s) gravado(s). O modelo é refeito na próxima consulta.")
