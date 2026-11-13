"""`curator recomendar` e `curator recomendacoes`: músicas fora da biblioteca (ADR-13).

Três intenções, três caminhos, sem padrão entre elas:
- só músicas novas: `curator recomendar rapido|lento` (fora da biblioteca, sempre);
- misturar novas e curtidas: o mesmo comando com `--misturar-curtidas` (só com pedido explícito);
- só curtidas: `curator receita executar` (como sempre).
O modo também é explícito: `rapido` (sem baixar nada) ou `lento` (baixa e analisa as candidatas antes).
Antes de coletar, o comando mostra o resumo e pede confirmação (`--sim` pula).
"""

import json
import time
from pathlib import Path

import typer
from pydantic import ValidationError

from curator.banco import consultas
from curator.config import carregar_config
from curator.interfaces.cli import app
from curator.interfaces.comandos.motor import _confirmar, _playlist
from curator.interfaces.comum import banco, falhar
from curator.motor import planos
from curator.motor import recomendacoes as rc

recomendar_app = typer.Typer(
    help="Recomenda músicas que você ainda não curtiu, a partir de sementes (faixas, playlist, cluster, artista).",
    no_args_is_help=True,
)
historico_app = typer.Typer(
    help="Recomendações já feitas: status, conclusão e taxa de acerto.", no_args_is_help=True
)
app.add_typer(recomendar_app, name="recomendar")
app.add_typer(historico_app, name="recomendacoes")

O_FAIXA = typer.Option([], "--faixa", "-f", help="video_id de uma faixa-semente (repita para várias).")
O_PLAYLIST = typer.Option(None, "--playlist", "-p", help="Playlist-semente (id ou título exato).")
O_CLUSTER = typer.Option(None, "--cluster", help="Cluster-semente do último agrupamento.")
O_ARTISTA = typer.Option(None, "--artista", "-a", help="Artista-semente (da biblioteca).")
O_TAMANHO = typer.Option(None, "--tamanho", "-n", help="Quantas faixas (padrão do config).")
O_MISTURAR = typer.Option(
    False, "--misturar-curtidas", help="Mistura músicas novas com as da biblioteca. Sem isto, só novas."
)
O_PROPORCAO = typer.Option(0.5, "--proporcao-novas", help="Com --misturar-curtidas: fração de novas (0..1).")
O_FONTE = typer.Option([], "--fonte", help="yt_radio, yt_related ou deezer (repita); padrão do config.")
O_EXCLUIR = typer.Option([], "--excluir-artista", help="Nunca recomendar este artista (repita).")
O_ESCRITA = typer.Option(
    [],
    "--escrita",
    help="Só faixas com esta escrita no título/álbum/artistas: japonesa, coreana, chinesa, "
    "cirilica, latina (repita; vale qualquer uma).",
)
O_SEM_ESCRITA = typer.Option([], "--sem-escrita", help="Nunca faixas com esta escrita (repita).")
O_CRIAR = typer.Option(
    False, "--criar-playlist", help="Gera um plano criando uma playlist NOVA com o resultado."
)
O_NOME = typer.Option(None, "--nome", help="Nome da playlist nova (padrão: 'Descobertas · <pedido>').")
O_EM = typer.Option(None, "--adicionar-em", help="Gera um plano acrescentando a uma playlist existente.")
O_PERMITIR = typer.Option(
    False,
    "--permitir-novas",
    help="Com --adicionar-em: aceita pôr músicas que você não conhece nessa playlist.",
)
O_PEDIDO = typer.Option(None, "--pedido", help="A frase original do pedido (fica no histórico).")
O_POPULARIDADE = typer.Option(
    None, "--popularidade", min=0.0, max=1.0,
    help="Preferência por popularidade (#87): 1 = as mais vistas, 0 = lado B (percentil na sua biblioteca).",
)  # fmt: skip
O_DESDE = typer.Option(
    None, "--desde", help="Só publicadas a partir desta data (AAAA-MM-DD; lançamentos, #87)."
)
O_SIM = typer.Option(False, "--sim", help="Não pergunta (para scripts).")
O_JSON = typer.Option(False, "--json", help="Saída em JSON.")


def _clientes(fontes: list[str]):
    from curator.coleta.deezer import ClienteDeezer
    from curator.interfaces.comandos.auth import cliente

    return cliente(), (ClienteDeezer() if "deezer" in fontes else None)


def _resumo(p, criar: bool, nome: str | None, em: str | None, permitir: bool) -> str:
    partes = []
    if p.faixas:
        partes.append(f"{len(p.faixas)} faixa(s)")
    if p.playlist:
        partes.append(f"playlist “{p.playlist}” (até {p.max_sementes})")
    if p.cluster is not None:
        partes.append(f"cluster {p.cluster} (até {p.max_sementes})")
    if p.artista:
        partes.append(f"artista “{p.artista}” (até {p.max_sementes})")
    escopo = (
        f"MISTURANDO novas ({p.proporcao_novas:.0%}) e da biblioteca"
        if p.escopo == "misturar"
        else "só músicas NOVAS (fora da biblioteca)"
    )
    modo = "RÁPIDA (sem baixar nada)" if p.modo == "rapido" else "LENTA (baixa e analisa as candidatas antes)"
    if em:
        destino = f"plano acrescentando a “{em}”" + (
            " — inclui músicas que você não conhece" if permitir else ""
        )
    elif criar:
        destino = f"plano de playlist NOVA “{nome or 'Descobertas · …'}”"
    else:
        destino = "só a lista (nenhum plano)"
    return (
        f"recomendação {modo} · {escopo}\n  sementes: {', '.join(partes)}\n"
        f"  fontes: {', '.join(p.fontes)} · tamanho {p.tamanho}\n  destino: {destino}"
        + (f"\n  popularidade: alvo {p.popularidade.alvo:.2f} (1 = as mais vistas)" if p.popularidade else "")
        + (f"\n  só publicadas desde {p.publicadas_desde:%d/%m/%Y}" if p.publicadas_desde else "")
        + (f"\n  só com escrita: {', '.join(p.exigir_escritas)}" if p.exigir_escritas else "")
        + (f"\n  sem escrita: {', '.join(p.excluir_escritas)}" if p.excluir_escritas else "")
    )


def _imprimir(con, res, como_json: bool) -> None:
    itens = rc.itens(con, res.recomendacao_id) if res.status == "pronta" else []
    if como_json:
        typer.echo(json.dumps({"recomendacao_id": res.recomendacao_id, "status": res.status, "pendentes": res.pendentes,
                               "descartes": dict(res.descartes), "itens": itens}, ensure_ascii=False, indent=2))  # fmt: skip
        return
    if res.status != "pronta":
        typer.echo(
            f"recomendação #{res.recomendacao_id}: {res.pendentes} candidata(s) na fila de análise (depois da "
            f"biblioteca). Conclua com `curator recomendacoes concluir {res.recomendacao_id}` (ou --aguardar)."
        )
        return
    typer.echo(f"recomendação #{res.recomendacao_id} · {len(itens)} faixa(s)")
    for i in itens:
        extra = " · já sugerida " + f"{i['vezes_antes']}×" if i["vezes_antes"] else ""
        marca = "♥ " if i["conhecida"] else ""
        fontes = ", ".join(i["fontes"]) or "motor"
        typer.echo(
            f"  {i['posicao']:>3}. {marca}{i['titulo']} — {', '.join(i['artistas'][:2])}  [{fontes}{extra}]"
        )
    if res.descartes:
        typer.echo("fora: " + ", ".join(f"{k}: {v}" for k, v in sorted(res.descartes.items())), err=True)
    ant, novas = rc.novidades(con, res.recomendacao_id)
    if ant is not None:
        typer.echo(
            f"em relação à recomendação #{ant} (mesmas sementes): {len(novas)} faixa(s) que não estavam lá"
        )


def _plano(con, rid: int, criar: bool, nome: str | None, em: str | None, permitir: bool) -> None:
    if not (criar or em):
        return
    try:
        pid = planos.de_recomendacao(con, rid, nome, _playlist(con, em) if em else None, permitir)
    except ValueError as e:
        falhar(str(e))
    typer.echo(
        f"plano #{pid} criado; revise com `curator plano mostrar {pid}` e aplique com `curator plano aplicar {pid}`"
    )


def _recomendar(modo: str, faixa, playlist, cluster, artista, tamanho, misturar_curtidas, proporcao_novas, fonte,
                excluir_artista, criar_playlist, nome, adicionar_em, permitir_novas, pedido, sim, como_json,
                spec: Path | None = None, aguardar: bool = False, popularidade: float | None = None,
                desde: str | None = None, escrita: list[str] = (), sem_escrita: list[str] = ()) -> None:  # fmt: skip
    from curator.coleta.candidatas import SementeInvalida
    from curator.motor import recomendar as rr

    if permitir_novas and not adicionar_em:
        falhar("--permitir-novas só faz sentido com --adicionar-em")
    if criar_playlist and adicionar_em:
        falhar("escolha um destino: --criar-playlist ou --adicionar-em")
    cfg = carregar_config()
    dados = json.loads(spec.read_text(encoding="utf-8")) if spec else {}
    dados.update({k: v for k, v in {
        "modo": modo, "escopo": "misturar" if misturar_curtidas else "novas", "faixas": list(faixa) or dados.get("faixas"),
        "playlist": playlist, "cluster": cluster, "artista": artista, "tamanho": tamanho, "fontes": list(fonte) or None,
        "excluir_artistas": list(excluir_artista) or dados.get("excluir_artistas"),
        "proporcao_novas": proporcao_novas if misturar_curtidas else None,
        "popularidade": {"alvo": popularidade} if popularidade is not None else None, "publicadas_desde": desde,
        "exigir_escritas": list(escrita) or dados.get("exigir_escritas"),
        "excluir_escritas": list(sem_escrita) or dados.get("excluir_escritas"),
    }.items() if v is not None})  # fmt: skip
    try:
        p = rr.Pedido.model_validate(dados)
    except ValidationError as e:
        falhar("pedido inválido:\n" + "\n".join(f"  - {x['msg']}" for x in e.errors()))
    typer.echo(_resumo(p.com_padroes(cfg), criar_playlist, nome, adicionar_em, permitir_novas))
    _confirmar("Continuar?", sim)
    con = banco()
    cli, dz = _clientes(p.com_padroes(cfg).fontes)
    try:
        res = rr.recomendar(con, cli, dz, p, cfg, pedido)
    except SementeInvalida as e:
        falhar(str(e))
    if res.status != "pronta" and aguardar:
        res = _aguardar(con, res.recomendacao_id, cfg)
    _imprimir(con, res, como_json)
    if res.status == "pronta":
        _plano(con, res.recomendacao_id, criar_playlist, nome, adicionar_em, permitir_novas)


def _aguardar(con, rid: int, cfg: dict, intervalo_s: float = 60):
    from curator.motor import lento

    while True:
        res = lento.concluir(con, rid, cfg)
        if res.status == "pronta":
            return res
        typer.echo(
            f"  {res.pendentes} candidata(s) ainda na fila (o worker `curator analisar` precisa estar rodando)…",
            err=True,
        )
        time.sleep(intervalo_s)


@recomendar_app.command("rapido")
def rapido(
    faixa: list[str] = O_FAIXA, playlist: str | None = O_PLAYLIST, cluster: int | None = O_CLUSTER,
    artista: str | None = O_ARTISTA, tamanho: int | None = O_TAMANHO, misturar_curtidas: bool = O_MISTURAR,
    proporcao_novas: float = O_PROPORCAO, fonte: list[str] = O_FONTE, excluir_artista: list[str] = O_EXCLUIR,
    criar_playlist: bool = O_CRIAR, nome: str | None = O_NOME, adicionar_em: str | None = O_EM,
    permitir_novas: bool = O_PERMITIR, pedido: str | None = O_PEDIDO, sim: bool = O_SIM, como_json: bool = O_JSON,
    popularidade: float | None = O_POPULARIDADE, desde: str | None = O_DESDE,
    escrita: list[str] = O_ESCRITA, sem_escrita: list[str] = O_SEM_ESCRITA,
) -> None:  # fmt: skip
    """Sem baixar nada: ordena pelo que o rádio do YT (e o Deezer) trazem para as sementes."""
    _recomendar("rapido", faixa, playlist, cluster, artista, tamanho, misturar_curtidas, proporcao_novas, fonte,
                excluir_artista, criar_playlist, nome, adicionar_em, permitir_novas, pedido, sim, como_json,
                popularidade=popularidade, desde=desde, escrita=escrita, sem_escrita=sem_escrita)  # fmt: skip


@recomendar_app.command("lento")
def lento_cmd(
    faixa: list[str] = O_FAIXA, playlist: str | None = O_PLAYLIST, cluster: int | None = O_CLUSTER,
    artista: str | None = O_ARTISTA, tamanho: int | None = O_TAMANHO, misturar_curtidas: bool = O_MISTURAR,
    proporcao_novas: float = O_PROPORCAO, fonte: list[str] = O_FONTE, excluir_artista: list[str] = O_EXCLUIR,
    criar_playlist: bool = O_CRIAR, nome: str | None = O_NOME, adicionar_em: str | None = O_EM,
    permitir_novas: bool = O_PERMITIR, pedido: str | None = O_PEDIDO, sim: bool = O_SIM, como_json: bool = O_JSON,
    spec: Path | None = typer.Option(None, "--spec", help="JSON com perfil_alvo/generos_preferidos (como na receita)."),
    aguardar: bool = typer.Option(False, "--aguardar", help="Espera as análises terminarem (o worker precisa rodar)."),
    popularidade: float | None = O_POPULARIDADE, desde: str | None = O_DESDE,
    escrita: list[str] = O_ESCRITA, sem_escrita: list[str] = O_SEM_ESCRITA,
) -> None:  # fmt: skip
    """Baixa e analisa as melhores candidatas (na fila, depois da biblioteca) e ordena com o motor."""
    _recomendar("lento", faixa, playlist, cluster, artista, tamanho, misturar_curtidas, proporcao_novas, fonte,
                excluir_artista, criar_playlist, nome, adicionar_em, permitir_novas, pedido, sim, como_json, spec,
                aguardar, popularidade, desde, escrita, sem_escrita)  # fmt: skip


# ------------------------------------------------------------------ histórico


@historico_app.command("listar")
def listar(limite: int = typer.Option(20, "--limite", "-n"), como_json: bool = O_JSON) -> None:
    """Recomendações feitas, das mais recentes: quantas novas viraram curtida, dislike ou ficaram sem reação
    (o status vem da última `curator sincronizar`)."""
    recs = rc.listar(banco(), limite)
    if como_json:
        typer.echo(json.dumps(recs, ensure_ascii=False, indent=2))
        return
    for r in recs:
        s = r["status_itens"]
        typer.echo(
            f"#{r['recomendacao_id']:<4} {r['criada_em'][:16]} {r['modo']:<6} {r['escopo']:<8} {r['status']:<18} "
            f"♥ {s['curtida']} · playlist {s['em_playlist']} · dislike {s['dislike']} · sem reação {s['sem_reacao']}"
            f"  {r['pedido'] or ''}"
        )


@historico_app.command("mostrar")
def mostrar(rid: int, como_json: bool = O_JSON) -> None:
    """Itens de uma recomendação com o status atual de cada um."""
    con = banco()
    r = rc.obter(con, rid)
    if r is None:
        falhar(f"recomendação #{rid} não existe")
    itens = rc.itens(con, rid)
    if como_json:
        typer.echo(json.dumps({**r, "itens": itens}, ensure_ascii=False, indent=2))
        return
    typer.echo(
        f"recomendação #{rid} · {r['modo']} · {r['escopo']} · {r['status']} · {r['criada_em']}  {r['pedido'] or ''}"
    )
    simbolo = {"curtida": "♥", "dislike": "✗", "em_playlist": "+", "sem_reacao": "·"}
    for i in itens:
        origem = "biblioteca" if i["conhecida"] else ", ".join(i["fontes"])
        typer.echo(
            f"  {simbolo[i['status']]} {i['posicao']:>3}. {i['titulo']} — {', '.join(i['artistas'][:2])}  [{origem}]"
        )


@historico_app.command("acerto")
def acerto(como_json: bool = O_JSON) -> None:
    """Taxa de acerto das músicas novas por fonte e por modo (curtida ou posta numa playlist sua)."""
    a = rc.acerto(banco())
    if como_json:
        typer.echo(json.dumps(a, ensure_ascii=False, indent=2))
        return
    if not a["fonte"]:
        typer.echo("nenhuma recomendação ainda")
        return
    for grupo in ("fonte", "modo"):
        typer.echo(f"por {grupo}:")
        for k, c in sorted(a[grupo].items()):
            typer.echo(
                f"  {k:<11} {c['recomendadas']:>5} recomendadas · acerto {c['taxa_acerto']:.0%} "
                f"(♥ {c['curtida']}, playlist {c['em_playlist']}) · dislike {c['taxa_dislike']:.0%}"
            )
    typer.echo("os pesos das fontes ficam em [recomendacoes.pesos_fontes] no config.toml")


@historico_app.command("curtidas")
def curtidas(
    plano: bool = typer.Option(
        False, "--plano", help="Plano que cria (ou completa) a playlist de descobertas curtidas."
    ),
    nome: str = typer.Option(planos.DESCOBERTAS_CURTIDAS, "--nome", help="Nome da playlist."),
) -> None:
    """Músicas que vieram de recomendação como novas e que você curtiu depois (#73)."""
    con = banco()
    ids = rc.descobertas_curtidas(con)
    typer.echo(f"{len(ids)} música(s) recomendada(s) como nova(s) e curtida(s) depois")
    for rotulo in _rotulos(con, ids):
        typer.echo(f"  {rotulo}")
    if plano:
        try:
            typer.echo(
                f"plano #{planos.de_descobertas_curtidas(con, nome)} criado; veja com `curator plano mostrar`"
            )
        except ValueError as e:
            falhar(str(e))


def _rotulos(con, ids: list[str]) -> list[str]:
    linhas = consultas.nomes_das_faixas(con, ids)
    return [linhas.get(v, v) for v in ids]


@historico_app.command("concluir")
def concluir(
    rid: int,
    parcial: bool = typer.Option(False, "--parcial", help="Fecha já, só com as candidatas analisadas."),
    aguardar: bool = typer.Option(False, "--aguardar", help="Espera as análises terminarem."),
    criar_playlist: bool = O_CRIAR, nome: str | None = O_NOME, como_json: bool = O_JSON,
) -> None:  # fmt: skip
    """Fecha uma recomendação lenta quando as análises das candidatas terminam."""
    from curator.motor import lento

    cfg, con = carregar_config(), banco()
    try:
        res = _aguardar(con, rid, cfg) if aguardar else lento.concluir(con, rid, cfg, parcial)
    except KeyError as e:
        falhar(str(e))
    _imprimir(con, res, como_json)
    if res.status == "pronta":
        _plano(con, rid, criar_playlist, nome, None, False)


@historico_app.command("plano")
def plano(
    rid: int, nome: str | None = O_NOME, adicionar_em: str | None = O_EM, permitir_novas: bool = O_PERMITIR,
) -> None:  # fmt: skip
    """Plano com uma recomendação salva: playlist nova (padrão) ou acrescentar a uma existente."""
    if permitir_novas and not adicionar_em:
        falhar("--permitir-novas só faz sentido com --adicionar-em")
    _plano(banco(), rid, not adicionar_em, nome, adicionar_em, permitir_novas)
