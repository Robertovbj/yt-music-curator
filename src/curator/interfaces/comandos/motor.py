"""`curator receita`, `classificar`, `avaliar-classificacao` e `plano`: o motor determinístico (M3)."""

import json
import sys
from pathlib import Path

import typer
from pydantic import ValidationError

from curator.interfaces.cli import app
from curator.interfaces.comum import aviso, banco, falhar
from curator.motor import classificar as cl
from curator.motor import planos, receitas
from curator.motor.receita import ReceitaInvalida, carregar, resolver_playlist

receita_app = typer.Typer(help="Receitas: playlists reprodutíveis descritas em JSON.", no_args_is_help=True)
plano_app = typer.Typer(help="Planos de alteração (diffs) para o YouTube Music.", no_args_is_help=True)
app.add_typer(receita_app, name="receita")
app.add_typer(plano_app, name="plano")


def _nomes(con, ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    return {
        r["video_id"]: f"{r['titulo']} — {', '.join(a['nome'] for a in json.loads(r['artistas'])[:2])}"
        for r in con.execute(
            f"SELECT video_id, titulo, artistas FROM faixas WHERE video_id IN ({','.join('?' * len(ids))})",
            ids,
        )
    }


def _playlist(con, ref: str | None) -> str | None:
    if ref is None:
        return None
    pid = resolver_playlist(con, ref)
    if pid is None:
        falhar(f"playlist '{ref}' não encontrada (use o id ou o título exato)")
    return pid


# ------------------------------------------------------------------ receitas


@receita_app.command("criar")
def receita_criar(
    arquivo: Path = typer.Argument(..., help="JSON da receita, ou '-' para ler do stdin.", allow_dash=True),
    pedido: str | None = typer.Option(
        None, "--pedido", help="A frase original (cache por pedido normalizado)."
    ),
) -> None:
    """Valida contra o banco e salva (substitui a receita de mesmo nome)."""
    texto = sys.stdin.read() if str(arquivo) == "-" else arquivo.read_text(encoding="utf-8")
    con = banco()
    try:
        r = carregar(texto)
        receitas.salvar(con, r, pedido)
    except ValidationError as e:
        falhar(f"receita fora do schema:\n{e}")
    except ReceitaInvalida as e:
        falhar("receita inválida:\n" + "\n".join(f"  - {x}" for x in e.erros))
    typer.echo(f"receita '{r.nome}' salva (hash {r.hash()})")


@receita_app.command("listar")
def receita_listar() -> None:
    for r in receitas.listar(banco()):
        typer.echo(f"{r['nome']:<30} {r['hash_spec']}  {r['execucoes']} execução(ões)  {r['pedido'] or ''}")


@receita_app.command("mostrar")
def receita_mostrar(nome: str) -> None:
    r = receitas.obter(banco(), nome)
    if r is None:
        falhar(f"receita '{nome}' não existe")
    typer.echo(json.dumps(r.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2))


@receita_app.command("executar")
def receita_executar(
    nome: str,
    como_json: bool = typer.Option(False, "--json"),
    plano_nova: bool = typer.Option(
        False, "--plano-nova", help="Gera um plano criando uma playlist com o nome da receita."
    ),
    plano_em: str | None = typer.Option(
        None, "--plano-em", help="Gera um plano adicionando a uma playlist existente."
    ),
) -> None:
    """Executa, registra a execução e compara com a anterior."""
    con = banco()
    try:
        ex, _ = receitas.executar_e_registrar(con, nome)
    except KeyError as e:
        falhar(str(e))
    except ReceitaInvalida as e:
        falhar("a receita não vale mais para o banco atual:\n" + "\n".join(f"  - {x}" for x in e.erros))
    if como_json:
        typer.echo(
            json.dumps(
                {"versao_banco": ex.versao_banco, "itens": [vars(i) for i in ex.itens]},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        nomes = _nomes(con, ex.ids)
        typer.echo(
            f"{len(ex.ids)} faixa(s) · universo {ex.universo} · sem análise {ex.sem_analise} · após filtros {ex.apos_filtros}"
        )
        for n, item in enumerate(ex.itens, start=1):
            typer.echo(f"  {n:>3}. {nomes.get(item.video_id, item.video_id)}  ({item.score:.3f})")
    hist = receitas.execucoes(con, nome)
    if len(hist) >= 2:
        c = receitas.comparar(hist[-2]["resultado"], hist[-1]["resultado"])
        typer.echo(
            f"em relação à execução anterior: Jaccard {c.jaccard} · +{len(c.entraram)} −{len(c.sairam)}",
            err=True,
        )
    if ex.sem_analise:
        aviso(f"{ex.sem_analise} faixa(s) do universo ainda sem análise ficaram de fora")
    if plano_nova or plano_em:
        destino = None if plano_nova else _playlist(con, plano_em)
        try:
            pid = planos.de_receita(con, nome, ex.ids, {i.video_id: i.score for i in ex.itens}, destino)
            typer.echo(f"plano #{pid} criado; veja com `curator plano mostrar`")
        except ValueError as e:
            aviso(str(e))


@receita_app.command("comparar")
def receita_comparar(nome: str) -> None:
    """Compara as duas últimas execuções de uma receita."""
    con = banco()
    hist = receitas.execucoes(con, nome)
    if len(hist) < 2:
        falhar("são precisas pelo menos duas execuções")
    c = receitas.comparar(hist[-2]["resultado"], hist[-1]["resultado"])
    typer.echo(f"Jaccard {c.jaccard} · mesma ordem: {'sim' if c.mesma_ordem else 'não'}")
    nomes = _nomes(con, c.entraram + c.sairam)
    for v in c.entraram:
        typer.echo(f"  + {nomes.get(v, v)}")
    for v in c.sairam:
        typer.echo(f"  − {nomes.get(v, v)}")


# ------------------------------------------------------------------ classificação


@app.command()
def classificar(
    mae: str | None = typer.Option(None, "--mae", help="Só entre as filhas desta playlist (id ou título)."),
    plano: bool = typer.Option(False, "--plano", help="Gera um plano com as sugestões."),
    como_json: bool = typer.Option(False, "--json"),
) -> None:
    """Sugere onde colocar as curtidas não alocadas (kNN + centroide, limiar por playlist)."""
    con = banco()
    c = cl.classificar(con, mae=_playlist(con, mae))
    if como_json:
        typer.echo(json.dumps([{"video_id": d.video_id, "destinos": [vars(x) for x in d.destinos],
                                 "proximas": [vars(x) for x in d.proximas], "ambigua": d.ambigua} for d in c.decisoes],
                              ensure_ascii=False, indent=2))  # fmt: skip
    else:
        nomes = _nomes(con, [d.video_id for d in c.decisoes])
        com = [d for d in c.decisoes if d.destinos]
        typer.echo(f"{len(com)} de {len(c.decisoes)} com destino · {c.sem_analise} sem análise (fora)")
        for d in c.decisoes:
            if d.destinos:
                alvo = ", ".join(f"{x.titulo} ({x.score:.2f}≥{x.limiar:.2f})" for x in d.destinos)
            else:
                alvo = "sem destino · mais próximas: " + ", ".join(
                    f"{x.titulo} ({x.score:.2f}<{x.limiar:.2f})" for x in d.proximas
                )
            typer.echo(f"  {'?' if d.ambigua else ' '} {nomes.get(d.video_id, d.video_id)} → {alvo}")
    for titulo, n in c.frias:
        aviso(f"'{titulo}' tem só {n} membro(s) analisado(s); ficou de fora (mínimo {cl.MIN_MEMBROS})")
    if plano:
        try:
            typer.echo(f"plano #{planos.de_classificacao(con, c.decisoes)} criado")
        except ValueError as e:
            aviso(str(e))


@app.command("avaliar-classificacao")
def avaliar_classificacao(
    mae: str | None = typer.Option(None, "--mae", help="Só as folhas abaixo desta playlist."),
) -> None:
    """Leave-one-out: tira cada membro e vê se o classificador o devolve à própria playlist."""
    con = banco()
    metricas = cl.avaliar(con, mae=_playlist(con, mae))
    if not metricas:
        falhar("nenhuma playlist com membros analisados suficientes")
    typer.echo(f"{'playlist':<45} {'membros':>7} {'precisão':>9} {'revocação':>10}")
    for m in metricas:
        typer.echo(f"{m.titulo[:45]:<45} {m.membros:>7} {m.precisao:>9.2f} {m.revocacao:>10.2f}")


# ------------------------------------------------------------------ planos


@plano_app.command("listar")
def plano_listar(status: str | None = typer.Option(None, "--status")) -> None:
    for p in planos.listar(banco(), status):
        typer.echo(
            f"#{p['plano_id']:<4} {p['status']:<10} {p['origem']:<25} {p['operacoes']} op · {p['faixas']} faixa(s) · {p['criado_em']}"
        )


@plano_app.command("mostrar")
def plano_mostrar(
    plano_id: int | None = typer.Argument(None, help="Padrão: o pendente mais recente."),
) -> None:
    con = banco()
    if plano_id is None:
        pend = planos.listar(con, "pendente")
        if not pend:
            falhar("nenhum plano pendente")
        plano_id = pend[0]["plano_id"]
    achado = planos.obter(con, plano_id)
    if achado is None:
        falhar(f"plano #{plano_id} não existe")
    meta, ops = achado
    typer.echo(f"plano #{plano_id} · {meta['origem']} · {meta['status']}\n")
    typer.echo(planos.diff_legivel(con, ops))


@plano_app.command("descartar")
def plano_descartar(plano_id: int) -> None:
    if not planos.descartar(banco(), plano_id):
        falhar(f"plano #{plano_id} não existe ou não está pendente")
    typer.echo(f"plano #{plano_id} descartado")


def _confirmar(pergunta: str, sim: bool) -> None:
    import sys

    if sim:
        return
    if not sys.stdin.isatty():
        falhar("sem terminal interativo: confira o diff com `curator plano mostrar` e rode de novo com --sim")
    if not typer.confirm(pergunta, default=False):
        falhar("nada foi alterado", codigo=0)


@plano_app.command("aplicar")
def plano_aplicar(
    plano_id: int | None = typer.Argument(None, help="Padrão: o pendente mais recente."),
    sim: bool = typer.Option(False, "--sim", help="Não pergunta (para scripts)."),
    grande: bool = typer.Option(
        False, "--grande", help="Necessário quando o plano passa do limite de faixas."
    ),
) -> None:
    """Aplica um plano no YouTube Music, depois de mostrar o diff e pedir confirmação."""
    from curator.coleta import aplicar as ap
    from curator.config import carregar_config
    from curator.interfaces.comandos.auth import cliente

    con = banco()
    if plano_id is None:
        pend = planos.listar(con, "pendente")
        if not pend:
            falhar("nenhum plano pendente")
        plano_id = pend[0]["plano_id"]
    achado = planos.obter(con, plano_id)
    if achado is None:
        falhar(f"plano #{plano_id} não existe")
    meta, ops = achado
    typer.echo(f"plano #{plano_id} · {meta['origem']}\n\n{planos.diff_legivel(con, ops)}\n")
    n, limite = ap.total_faixas(ops), carregar_config()["planos"]["limite_faixas"]
    if n > limite and not grande:
        falhar(f"o plano mexe em {n} faixas (limite {limite}); confira o diff e rode de novo com --grande")
    _confirmar(f"Aplicar estas alterações ({n} faixa(s)) no YouTube Music?", sim)
    try:
        res = ap.aplicar(con, cliente(), plano_id)
    except ap.PlanoInvalido as e:
        falhar(str(e))
    for x in res.log:
        typer.echo(f"  {'ok ' if x['ok'] else 'ERRO'} {x['tipo']:<9} {x['playlist']}: {x['detalhe']}")
    for d in res.divergencias:
        aviso(d)
    typer.echo(f"plano #{plano_id}: {res.status}. Para voltar atrás: `curator plano desfazer {plano_id}`")
    if res.status != "aplicado":
        raise typer.Exit(1)


@plano_app.command("desfazer")
def plano_desfazer(plano_id: int, sim: bool = typer.Option(False, "--sim")) -> None:
    """Volta as playlists ao estado de antes do plano e apaga as que ele criou."""
    from curator.coleta import aplicar as ap
    from curator.interfaces.comandos.auth import cliente

    _confirmar(f"Desfazer o plano #{plano_id} no YouTube Music (inclui apagar playlists que ele criou)?", sim)
    try:
        for linha in ap.desfazer(banco(), cliente(), plano_id):
            typer.echo(f"  {linha}")
    except ap.PlanoInvalido as e:
        falhar(str(e))
    typer.echo(f"plano #{plano_id} desfeito")
