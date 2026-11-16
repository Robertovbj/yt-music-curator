"""Recomendações fora da biblioteca (ADR-13): o pedido, a coleta e o ranking.

Escopos (nunca há padrão; quem chama diz qual):
- `novas`: só faixas fora da biblioteca (nem curtidas, nem em playlists próprias comuns);
- `misturar`: novas e da biblioteca, na proporção pedida. Só quando o usuário pede isso explicitamente.

Modos:
- `rapido` (#66): sem baixar nada. Ordena as candidatas pelas coletas: em quantas sementes apareceram,
  a posição em cada rádio e o peso da fonte;
- `lento` (#67): baixa e analisa as melhores candidatas e ordena com o motor (ver `lento.py`).

Sempre fora: faixas com dislike, as próprias sementes e o que já foi recomendado 3 vezes sem reação nos
últimos 30 dias (#62). Uma recomendação anterior sem reação custa 15% do score a cada vez.

Popularidade e novidade (#87): `popularidade` (alvo 0..1 no percentil das visualizações da biblioteca, como na
receita) multiplica o score por exp(−peso·dz²/2); `publicadas_desde` deixa só o que foi publicado a partir da data.
As candidatas não têm esses dados: antes de ordenar, as `estatisticas_top` melhores pelas coletas recebem as
visualizações e a data (uma chamada por faixa, guardada com validade). Sem o dado, a popularidade custa um desvio e
a data tira a faixa.

Dislike fora das playlists (#60): o rádio não mostra dislike, então antes de salvar cada música nova da lista
tem a avaliação conferida no botão de like do player; as que vierem com dislike (ou já curtidas) saem e o
ranking é refeito. Parecer com o que levou dislike também custa (#61, `fatores_dislike`).
Mesmo snapshot de coletas + mesmo histórico → mesma lista (desempate por video_id).
"""

import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from curator.banco import consultas
from curator.banco.consultas import EM_LISTA, lista
from curator.motor import recomendacoes as rc
from curator.motor.escrita import Escrita
from curator.motor.receita import Alvo
from curator.texto import normalizar

Fonte = Literal["yt_radio", "yt_related", "deezer"]


class PedidoInvalido(ValueError):
    pass


class Pedido(BaseModel):
    """O que foi pedido. `modo` e `escopo` não têm padrão de propósito: na dúvida, perguntar ao usuário."""

    model_config = ConfigDict(extra="forbid")
    modo: Literal["rapido", "lento"]
    escopo: Literal["novas", "misturar"]
    faixas: list[str] = []
    playlist: str | None = None
    cluster: int | None = None
    artista: str | None = None
    tamanho: Annotated[int, Field(ge=1, le=200)] | None = None
    max_sementes: Annotated[int, Field(ge=1, le=20)] | None = None
    fontes: list[Fonte] | None = None
    proporcao_novas: Annotated[float, Field(ge=0, le=1)] = 0.5  # só no escopo "misturar"
    excluir_artistas: list[str] = []
    duracao_s: tuple[int, int] | None = None
    max_por_artista: Annotated[int, Field(ge=1)] | None = None
    # #87: preferência por popularidade (alvo 0..1 = percentil das visualizações na biblioteca; 1 = as mais
    # conhecidas, 0 = lado B) e só lançamentos a partir de uma data
    popularidade: Alvo | None = None
    publicadas_desde: date | None = None
    # só no modo lento: as novas precisam de análise para isto
    perfil_alvo: dict[str, Alvo] = {}
    generos_preferidos: dict[str, Annotated[float, Field(gt=0, le=10)]] = {}
    # #111: escrita do título/álbum/artistas (motor/escrita.py), pelos metadados: vale nos dois modos
    exigir_escritas: list[Escrita] = []  # pelo menos uma delas
    excluir_escritas: list[Escrita] = []

    @model_validator(mode="after")
    def _coerente(self):
        if not (self.faixas or self.playlist or self.cluster is not None or self.artista):
            raise ValueError("dê ao menos uma semente: faixas, playlist, cluster ou artista")
        if self.modo == "rapido" and (self.perfil_alvo or self.generos_preferidos):
            raise ValueError(
                "perfil_alvo e generos_preferidos precisam do modo lento (as novas ainda não têm análise)"
            )
        if self.duracao_s and self.duracao_s[0] > self.duracao_s[1]:
            raise ValueError("duracao_s: mínimo maior que o máximo")
        if self.popularidade and not 0 <= self.popularidade.alvo <= 1:
            raise ValueError("popularidade: alvo fora de 0..1 (percentil na biblioteca)")
        return self

    def com_padroes(self, cfg: dict) -> Pedido:
        r = cfg["recomendacoes"]
        return self.model_copy(update={
            "tamanho": self.tamanho or r["tamanho"],
            "max_sementes": self.max_sementes or r["max_sementes"],
            "fontes": self.fontes or list(r["fontes"]),
            "max_por_artista": self.max_por_artista or r["max_por_artista"],
        })  # fmt: skip

    def spec(self) -> dict:
        d = self.model_dump(mode="json")
        for campo in ("exigir_escritas", "excluir_escritas"):  # #111: pedidos anteriores mantêm o hash
            if not d[campo]:
                del d[campo]
        return d


# ------------------------------------------------------------------ agregação das coletas


@dataclass
class Agregado:
    score: float = 0.0
    sementes: set[str] = field(default_factory=set)
    fontes: set[str] = field(default_factory=set)


def agregar(coletas: list[dict], pesos: dict[str, float]) -> dict[str, Agregado]:
    """Cada aparição vale peso_da_fonte × (1 − ½·posição/tamanho); somado entre sementes e fontes."""
    out: dict[str, Agregado] = defaultdict(Agregado)
    for c in coletas:
        n = len(c["itens"]) or 1
        for pos, vid in enumerate(c["itens"]):
            a = out[vid]
            a.score += pesos.get(c["fonte"], 1.0) * (1 - 0.5 * pos / n)
            a.sementes.add(c["semente"])
            a.fontes.add(c["fonte"])
    return dict(out)


def _meta(con: sqlite3.Connection, ids: list[str]) -> dict[str, dict]:
    out = {}
    for r in con.execute(
        f"SELECT video_id, artistas, duracao_s, avaliacao, curtida FROM faixas WHERE video_id IN {EM_LISTA}",
        (lista(ids),),
    ):
        arts = json.loads(r["artistas"])
        out[r["video_id"]] = {
            "artistas": [normalizar(a["nome"]) for a in arts],
            "principal": arts[0]["id"] if arts else f"sem-artista:{r['video_id']}",
            "duracao_s": r["duracao_s"],
            "avaliacao": r["avaliacao"],
        }
    return out


def fatores_dislike(con: sqlite3.Connection, ids: list[str], cfg: dict) -> dict[str, float]:
    """Penalidade (fator 0..1) por parecer com o que levou dislike, a partir dos seus próprios dados (#61):

    - artista: peso × fração de dislikes entre as faixas avaliadas do artista (curtidas + dislikes). Um dislike
      num artista com 400 curtidas quase não pesa; um artista só com dislikes pesa inteiro;
    - som: se a candidata já tem análise e a faixa mais parecida com ela (embedding) é um dislike, e não uma da
      biblioteca, peso × (diferença de similaridade / margem), até o peso inteiro.
    """
    r = cfg["recomendacoes"]
    if not ids or not consultas.com_dislike(con):
        return {}
    avaliadas = {
        a: (d, c)
        for a, d, c in con.execute(
            f"""SELECT fa.artista_id, sum(f.avaliacao = 'dislike'), sum(f.curtida = 1 AND f.avaliacao != 'dislike')
               FROM faixa_artistas fa JOIN faixas f USING (video_id)
               WHERE f.video_id NOT IN ({consultas.IGNORADAS}) GROUP BY fa.artista_id
               HAVING sum(f.avaliacao = 'dislike') > 0"""
        )
    }
    out: dict[str, float] = {}
    for vid, art in con.execute(
        f"SELECT video_id, artista_id FROM faixa_artistas WHERE video_id IN {EM_LISTA}",
        (lista(ids),),
    ):
        if art in avaliadas:
            d, c = avaliadas[art]
            out[vid] = min(out.get(vid, 1.0), 1 - r["dislike_peso_artista"] * d / (d + c))
    import numpy as np

    from curator.motor import espacos

    emb = espacos.embeddings(con)
    dislikes = [v for v in sorted(consultas.com_dislike(con)) if v in emb.indice]
    gosto = [v for v in espacos.da_biblioteca(con) if v in emb.indice]
    analisadas = [v for v in ids if v in emb.indice]
    if dislikes and gosto and analisadas:
        C = np.stack([emb.linha(v) for v in analisadas])
        s_dis = (C @ np.stack([emb.linha(v) for v in dislikes]).T).max(axis=1)
        s_gos = (C @ np.stack([emb.linha(v) for v in gosto]).T).max(axis=1)
        for v, a, b in zip(analisadas, s_dis, s_gos, strict=True):
            if a > b:
                f = 1 - r["dislike_peso_som"] * min(1.0, float(a - b) / r["dislike_margem_som"])
                out[v] = out.get(v, 1.0) * f
    return {v: round(f, 6) for v, f in out.items() if f < 1}


@dataclass
class Ranking:
    itens: list[rc.ItemRecomendado]
    descartes: Counter = field(default_factory=Counter)


def _bloqueio_por_repeticao(h: rc.Historico | None, cfg: dict, agora: datetime) -> bool:
    r = cfg["recomendacoes"]
    if h is None or h.vezes < r["max_repeticoes"] or not h.ultima:
        return False
    return datetime.fromisoformat(h.ultima) > agora - timedelta(days=r["pausa_dias"])


def candidatas(
    con: sqlite3.Connection,
    p: Pedido,
    cfg: dict,
    sementes: list[str],
    coleta_ids: list[int],
    conhecidas: bool = False,
    ref: dict | None = None,
) -> Ranking:
    """Candidatas das coletas, filtradas e pontuadas (0..1), em ordem. `conhecidas=True` dá as da biblioteca
    (escopo "misturar"); senão, só as novas."""
    # `ref`: a recomendação salva que está sendo refeita (o histórico e o relógio param nela)
    agora = datetime.fromisoformat(ref["criada_em"]) if ref else datetime.now(UTC)
    agregados = agregar(rc.ler_coletas(con, coleta_ids), cfg["recomendacoes"]["pesos_fontes"])
    meta = _meta(con, sorted(agregados))
    dentro = set(consultas.biblioteca(con))
    ignoradas = consultas.ignoradas(con)
    hist = {} if conhecidas else rc.historico(con, ref["recomendacao_id"] if ref else None)
    pen = cfg["recomendacoes"]["penalidade_repeticao"]
    proibidos = [normalizar(a) for a in p.excluir_artistas]
    maior = max((a.score for a in agregados.values()), default=1.0) or 1.0
    penalidade = fatores_dislike(con, sorted(agregados), cfg)
    f_pop = _fatores_popularidade(con, sorted(agregados), p.popularidade)
    publicadas = _publicadas(con, sorted(agregados)) if p.publicadas_desde else {}
    escritas = {}
    if p.exigir_escritas or p.excluir_escritas:
        from curator.motor import escrita

        escritas = escrita.calcular(con, sorted(agregados))
    rk = Ranking([])
    for vid, a in agregados.items():
        m = meta.get(vid)
        if m is None:
            continue
        if vid in sementes:
            continue
        if m["avaliacao"] == "dislike":
            rk.descartes["dislike"] += 1
            continue
        if vid in ignoradas:
            rk.descartes["ignorada"] += 1
            continue
        if conhecidas != (vid in dentro):
            if not conhecidas:
                rk.descartes["já na biblioteca"] += 1
            continue
        if not conhecidas and m["avaliacao"] == "like":
            rk.descartes["já curtida"] += 1  # curtida no YT, ainda não sincronizada
            continue
        if proibidos and any(x in proibidos for x in m["artistas"]):
            rk.descartes["artista excluído"] += 1
            continue
        if p.duracao_s and not (m["duracao_s"] and p.duracao_s[0] <= m["duracao_s"] <= p.duracao_s[1]):
            rk.descartes["duração"] += 1
            continue
        if p.exigir_escritas and not escritas.get(vid, frozenset()) & set(p.exigir_escritas):
            rk.descartes["sem a escrita exigida"] += 1
            continue
        if p.excluir_escritas and escritas.get(vid, frozenset()) & set(p.excluir_escritas):
            rk.descartes["escrita excluída"] += 1
            continue
        if p.publicadas_desde and not (
            publicadas.get(vid) and publicadas[vid] >= p.publicadas_desde.isoformat()
        ):
            rk.descartes["publicada antes" if publicadas.get(vid) else "sem data de publicação"] += 1
            continue
        h = hist.get(vid)
        if _bloqueio_por_repeticao(h, cfg, agora):
            rk.descartes["recomendada demais sem reação"] += 1
            continue
        vezes = h.vezes if h else 0
        f_dislike = penalidade.get(vid, 1.0)
        score = a.score / maior * (1 - pen) ** vezes * f_dislike * f_pop.get(vid, 1.0)
        comp = {"coleta": round(a.score / maior, 6)} | ({"dislike": f_dislike} if f_dislike < 1 else {})
        if vid in f_pop:
            comp["popularidade"] = round(f_pop[vid], 6)
        rk.itens.append(rc.ItemRecomendado(
            vid, round(score, 6), sorted(a.fontes), len(a.sementes), vezes, conhecidas, comp,
        ))  # fmt: skip
    rk.itens.sort(key=lambda i: (-i.score, i.video_id))
    for i in rk.itens:
        i.componentes["_principal"] = meta[i.video_id]["principal"]  # removido em `limitar`
    return rk


def _fatores_popularidade(con: sqlite3.Connection, ids: list[str], alvo: Alvo | None) -> dict[str, float]:
    """exp(−peso·dz²/2), dz = (percentil − alvo) ÷ desvio do percentil; sem visualizações, dz = 1 (#87)."""
    if alvo is None:
        return {}
    from curator.motor import popularidade

    pct = popularidade.percentis(con, ids)
    dz = np.where(np.isnan(pct), 1.0, (pct - alvo.alvo) / popularidade.DESVIO)
    return {v: float(np.exp(-alvo.peso * d**2 / 2)) for v, d in zip(ids, dz, strict=True)}


def _publicadas(con: sqlite3.Connection, ids: list[str]) -> dict[str, str]:
    out = {}
    out |= dict(con.execute(
        f"SELECT video_id, publicada_em FROM faixas WHERE publicada_em IS NOT NULL "
        f"AND video_id IN {EM_LISTA}", (lista(ids),),
    ).fetchall())  # fmt: skip
    return out


def buscar_estatisticas(
    con: sqlite3.Connection, cli, p: Pedido, cfg: dict, sementes: list[str], coleta_ids: list[int]
) -> int:
    """Visualizações e data de publicação das melhores candidatas, quando o pedido usa uma das duas (#87).

    "Melhores" = o ranking sem popularidade nem data (senão, sem o dado, nenhuma passaria). Novas e, no escopo
    misturar, as da biblioteca que o rádio trouxe (a biblioteca já tem o dado pelo `sincronizar`)."""
    if cli is None or not (p.popularidade or p.publicadas_desde):
        return 0
    from curator.coleta.sincronizar import estatisticas_de

    base = p.model_copy(update={"popularidade": None, "publicadas_desde": None})
    top = cfg["recomendacoes"]["estatisticas_top"]
    ids = [i.video_id for i in candidatas(con, base, cfg, sementes, coleta_ids).itens[:top]]
    if p.escopo == "misturar":
        ids += [
            i.video_id for i in candidatas(con, base, cfg, sementes, coleta_ids, conhecidas=True).itens[:top]
        ]
    res = estatisticas_de(con, cli, ids, cfg["metadados"]["estatisticas_ttl_dias"])
    return res.contagens.get("buscadas", 0)


def limitar(
    itens: list[rc.ItemRecomendado], tamanho: int, max_por_artista: int | None
) -> list[rc.ItemRecomendado]:
    """Corta no tamanho com no máximo N faixas por artista principal, mantendo a ordem."""
    por: Counter = Counter()
    out = []
    for i in itens:
        art = i.componentes.pop("_principal", None)
        if len(out) >= tamanho:
            continue
        if max_por_artista and art is not None and por[art] >= max_por_artista:
            continue
        por[art] += 1
        out.append(i)
    return out


def intercalar(novas: list, conhecidas: list, tamanho: int, proporcao_novas: float) -> list:
    """Mistura determinística: a cada posição, entra uma nova se ainda estiver abaixo da proporção.
    Se um lado acabar, o outro completa."""
    out, n, c = [], 0, 0
    while len(out) < tamanho and (n < len(novas) or c < len(conhecidas)):
        quer_nova = n < int(
            (len(out) + 1) * proporcao_novas + 0.5
        )  # arredonda .5 para cima: começa pela nova
        if (quer_nova and n < len(novas)) or c >= len(conhecidas):
            out.append(novas[n])
            n += 1
        else:
            out.append(conhecidas[c])
            c += 1
    return out


def conhecidas_do_motor(
    con: sqlite3.Connection, p: Pedido, sementes: list[str], ja: set[str], n: int
) -> list[rc.ItemRecomendado]:
    """Faixas da biblioteca parecidas com as sementes (receita com as sementes analisadas), para completar o
    lado "conhecidas" do escopo misturar quando os rádios trazem poucas. As análises já existem: nada é baixado."""
    from curator.motor import espacos
    from curator.motor.executor import executar
    from curator.motor.receita import Receita

    analisadas = set(espacos.analisadas(con))
    sem = [s for s in sementes if s in analisadas]
    if not sem and not p.perfil_alvo and not p.generos_preferidos:
        return []
    r = Receita(
        nome="recomendacao", tamanho=min(1000, n + len(ja) + len(sementes)), universo="biblioteca", sementes=sem,
        perfil_alvo=p.perfil_alvo, generos_preferidos=p.generos_preferidos,
        filtros={"excluir_artistas": p.excluir_artistas, "duracao_s": p.duracao_s,
                 "exigir_escritas": p.exigir_escritas, "excluir_escritas": p.excluir_escritas},
    )  # fmt: skip
    out = []
    for i in executar(con, r).itens:
        if i.video_id in ja or i.video_id in sementes:
            continue
        out.append(
            rc.ItemRecomendado(i.video_id, round(0.5 * i.score, 6), [], 0, 0, True, {"motor": i.score})
        )
        if len(out) >= n:
            break
    return out


# ------------------------------------------------------------------ modo rápido (#66)


def ranquear_rapido(
    con: sqlite3.Connection, p: Pedido, cfg: dict, sementes: list[str], coleta_ids: list[int],
    ref: dict | None = None,
) -> Ranking:  # fmt: skip
    p = p.com_padroes(cfg)
    novas = candidatas(con, p, cfg, sementes, coleta_ids, ref=ref)
    if p.escopo == "novas":
        return Ranking(limitar(novas.itens, p.tamanho, p.max_por_artista), novas.descartes)
    return misturar(con, p, cfg, sementes, coleta_ids, novas, ref)


def misturar(
    con: sqlite3.Connection, p: Pedido, cfg: dict, sementes: list[str], coleta_ids: list[int], novas: Ranking,
    ref: dict | None = None,
) -> Ranking:  # fmt: skip
    """Escopo misturar: as conhecidas que os rádios trouxeram, completadas pelo motor, intercaladas às novas."""
    conh = candidatas(con, p, cfg, sementes, coleta_ids, conhecidas=True, ref=ref)
    falta = p.tamanho - int(p.tamanho * p.proporcao_novas + 0.5)
    a = limitar(novas.itens, p.tamanho, p.max_por_artista)
    b = limitar(conh.itens, p.tamanho, p.max_por_artista)
    if len(b) < falta:
        b += conhecidas_do_motor(
            con, p, sementes, {i.video_id for i in b} | {i.video_id for i in a}, falta - len(b)
        )
    return Ranking(intercalar(a, b, p.tamanho, p.proporcao_novas), novas.descartes + conh.descartes)


# ------------------------------------------------------------------ orquestração


@dataclass
class Resultado:
    recomendacao_id: int
    status: str
    itens: list[rc.ItemRecomendado]
    sementes: list[str]
    descartes: Counter
    pendentes: int = 0  # modo lento: candidatas ainda sem análise


def coletar(con: sqlite3.Connection, cli, dz, p: Pedido, cfg: dict, sementes: list[str]) -> list[int]:
    from curator.coleta import candidatas as cd
    from curator.coleta import deezer

    ids = cd.coletar(con, cli, sementes, p.fontes, cfg["recomendacoes"]["cache_h"])
    if "deezer" in p.fontes and dz is not None:
        ids += deezer.coletar(con, cli, dz, sementes, cfg)
    return sorted(set(ids))


def recomendar(
    con: sqlite3.Connection, cli, dz, pedido: Pedido, cfg: dict, texto: str | None = None
) -> Resultado:
    """Resolve as sementes, coleta (YT + Deezer), ordena e salva. No modo lento, enfileira as análises."""
    from curator.coleta.candidatas import resolver_sementes

    p = pedido.com_padroes(cfg)
    sementes = resolver_sementes(con, p.faixas, p.playlist, p.cluster, p.artista, p.max_sementes)
    coleta_ids = coletar(con, cli, dz, p, cfg, sementes)
    buscar_estatisticas(con, cli, p, cfg, sementes, coleta_ids)
    if p.modo == "lento":
        from curator.motor import lento

        return lento.iniciar(con, p, cfg, sementes, coleta_ids, texto, cli)
    rk = conferido(con, cli, cfg, lambda: ranquear_rapido(con, p, cfg, sementes, coleta_ids))
    rid = rc.salvar(con, modo=p.modo, escopo=p.escopo, spec=pedido.spec(), sementes=sementes, coletas=coleta_ids,
                    itens=rk.itens, pedido=texto)  # fmt: skip
    return Resultado(rid, "pronta", rk.itens, sementes, rk.descartes)


def conferido(con: sqlite3.Connection, cli, cfg: dict, ranquear, rodadas: int = 5) -> Ranking:
    """Ranking cujas músicas novas tiveram a avaliação conferida no YT (#60). Se alguma vier com dislike ou já
    curtida, o banco é atualizado e o ranking refeito (ela sai e entra a próxima)."""
    from curator.coleta.sincronizar import conferir_avaliacoes

    r = cfg["recomendacoes"]
    rk = ranquear()
    conferidas = Counter()
    for _ in range(rodadas):
        if cli is None or not r["conferir_avaliacao"]:
            break
        faltam = rc.precisa_conferir(
            con, [i.video_id for i in rk.itens if not i.conhecida], r["conferir_validade_dias"]
        )
        if not faltam:
            break
        res = conferir_avaliacoes(con, cli, faltam)
        conferidas.update(res.contagens)
        if not (res.contagens.get("dislikes_novos") or res.contagens.get("likes_novos")):
            break
        rk = ranquear()
    if conferidas.get("dislikes_novos"):
        rk.descartes["dislike visto na conferência"] = conferidas["dislikes_novos"]
    return rk


def refazer(con: sqlite3.Connection, rid: int, cfg: dict) -> list[rc.ItemRecomendado]:
    """O ranking de uma recomendação salva, sobre as mesmas coletas e o histórico de antes dela."""
    r = rc.obter(con, rid)
    p = Pedido.model_validate(r["spec"])
    if p.modo == "lento":
        from curator.motor import lento

        return lento.ranquear(con, p, cfg, r["sementes"], r["coletas"], ref=r).itens
    return ranquear_rapido(con, p, cfg, r["sementes"], r["coletas"], ref=r).itens
