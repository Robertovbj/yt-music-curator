"""`curator marcacoes`: etiquetas por artista ("k-pop", "vtuber"…) e exceções por faixa (#107)."""

import json

import typer

from curator.banco.conexao import transacao
from curator.config import carregar_config
from curator.interfaces.cli import app
from curator.interfaces.comum import banco, falhar
from curator.motor import marcacoes as mc
from curator.motor import planos

marcacoes_app = typer.Typer(
    help="Marcações por artista (k-pop, vtuber…), com exceção por faixa. A IA usa como filtro ou fator; as regras "
    "de destino (etiqueta → master) ficam no config.toml, na seção marcacoes.regras.",
    no_args_is_help=True,
)
app.add_typer(marcacoes_app, name="marcacoes")


def _titulo(con, vid: str) -> str:
    r = con.execute("SELECT titulo, artistas FROM faixas WHERE video_id = ?", (vid,)).fetchone()
    if r is None:
        return vid
    return f"{r['titulo']} — {', '.join(a['nome'] for a in json.loads(r['artistas'])[:2])} ({vid})"


@marcacoes_app.command("artista")
def artista(
    etiqueta: str = typer.Argument(..., help="Ex.: k-pop, vtuber, pop-ocidental."),
    artistas: list[str] = typer.Argument(
        ..., help="Nome exato (sem acento/caixa) ou id do canal; aceita vários."
    ),
    desfazer: bool = typer.Option(False, "--desfazer", help="Tira a etiqueta dos artistas."),
) -> None:
    """Dá a etiqueta a todas as faixas dos artistas (todos os canais com o mesmo nome). Nada é gravado se algum
    nome não existir."""
    con = banco()
    feitos = []
    try:
        with transacao(con):
            for a in artistas:
                feitos.append(mc.marcar_artista(con, a, etiqueta, "cli", desfazer))
    except mc.MarcacaoInvalida as e:
        falhar(f"{e}\nnada foi gravado")
    e = mc.etiqueta(etiqueta)
    for achados in feitos:
        canais = f" ({len(achados)} canais)" if len(achados) > 1 else ""
        typer.echo(f"{'sem' if desfazer else 'com'} '{e}': {achados[0][1]}{canais}")


@marcacoes_app.command("faixa")
def faixa(
    etiqueta: str = typer.Argument(...),
    video_ids: list[str] = typer.Argument(..., help="video_id; aceita vários (use -- antes de ids com '-')."),
    nao: bool = typer.Option(False, "--nao", help="A faixa NÃO tem a etiqueta, mesmo que o artista tenha."),
    desfazer: bool = typer.Option(False, "--desfazer", help="Apaga a exceção: volta a valer o artista."),
) -> None:
    """Exceção por faixa, que vence o artista: dá a etiqueta (padrão) ou tira (--nao)."""
    if nao and desfazer:
        falhar("use --nao ou --desfazer, não os dois")
    con = banco()
    try:
        with transacao(con):
            for v in video_ids:
                mc.marcar_faixa(con, v, etiqueta, None if desfazer else not nao, "cli")
    except mc.MarcacaoInvalida as e:
        falhar(f"{e}\nnada foi gravado")
    e = mc.etiqueta(etiqueta)
    acao = "exceção apagada" if desfazer else f"sem '{e}'" if nao else f"com '{e}'"
    for v in video_ids:
        typer.echo(f"{acao}: {_titulo(con, v)}")


@marcacoes_app.command("listar")
def listar(
    etiqueta: str | None = typer.Argument(None, help="Só esta etiqueta."),
    faixas: bool = typer.Option(False, "--faixas", help="Lista as faixas com a etiqueta."),
) -> None:
    """As etiquetas, os artistas marcados, as exceções e quantas faixas da biblioteca ficam com cada uma."""
    con = banco()
    try:
        linhas = mc.resumo(con, etiqueta)
    except mc.MarcacaoInvalida as e:
        falhar(str(e))
    if not linhas:
        typer.echo("nenhuma marcação" + (f" '{etiqueta}'" if etiqueta else ""))
        return
    por_faixa = mc.da_faixa(con) if faixas else {}
    for r in linhas:
        typer.echo(f"{r['etiqueta']}: {r['faixas']} faixa(s) · {len(r['artistas'])} artista(s)")
        if r["artistas"]:
            typer.echo(f"  artistas: {', '.join(r['artistas'])}")
        for campo, rot in (("faixas_com", "com, pela faixa"), ("faixas_sem", "sem, pela faixa")):
            for v in r[campo]:
                typer.echo(f"  {rot}: {_titulo(con, v)}")
        if faixas:
            for v in sorted(v for v, es in por_faixa.items() if r["etiqueta"] in es):
                typer.echo(f"    {_titulo(con, v)}")


@marcacoes_app.command("regras")
def regras() -> None:
    """As regras de destino do config.toml (seção marcacoes.regras), com as masters resolvidas."""
    con = banco()
    try:
        rs = mc.regras(con, carregar_config())
    except mc.MarcacaoInvalida as e:
        falhar(str(e))
    if not rs:
        typer.echo("nenhuma regra: as marcações só informam (filtro e fator nas receitas)")
        return
    titulos = dict(con.execute("SELECT playlist_id, titulo FROM playlists"))
    for r in rs:
        partes = []
        if r.entrar_em:
            partes.append("entra em " + ", ".join(titulos.get(p, p) for p in r.entrar_em))
        if r.sair_de:
            partes.append("sai de " + ", ".join(titulos.get(p, p) for p in r.sair_de))
        typer.echo(f"  {r.etiqueta}: {'; '.join(partes)}")


@marcacoes_app.command("plano")
def plano() -> None:
    """Plano que corrige as masters atuais, só com o que contraria as regras (nada é aplicado sem `plano aplicar`)."""
    con = banco()
    try:
        pid = mc.plano(con, mc.regras(con, carregar_config()))
    except (mc.MarcacaoInvalida, ValueError) as e:
        falhar(str(e))
    typer.echo(f"plano #{pid} criado:\n\n{planos.diff_legivel(con, planos.obter(con, pid)[1])}")
    typer.echo(f"\npara aplicar: curator plano aplicar {pid}")
