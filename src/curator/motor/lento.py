"""Recomendação lenta (#67, ADR-13): baixa e analisa as melhores candidatas antes de ordenar.

1. As candidatas novas saem do ranking rápido; as `analisar_top` melhores vão para a fila com prioridade 1
   (depois da biblioteca, sem cota própria). As já analisadas (de outra recomendação, ou de quando eram
   curtidas) não baixam de novo: a análise nunca é apagada (#59).
2. A recomendação fica "aguardando_analise" até a fila terminar; `concluir` fecha com o que já tem.
3. Ordem final = 0,6 × motor (similaridade com as sementes analisadas, perfil-alvo e gêneros, como numa
   receita) + 0,4 × coleta, com a mesma penalidade de repetição do modo rápido. Dislikes ficam fora: a
   avaliação de cada candidata é conferida no YT antes de ela ir para a fila.
"""

import json
import sqlite3

from curator.motor import espacos
from curator.motor import recomendacoes as rc
from curator.motor import recomendar as rr

PESO_MOTOR = 0.6


def _versao() -> tuple[str, str]:
    from curator.audio.analisador import VERSAO_ANALISADOR
    from curator.audio.modelos import hash_modelos

    return VERSAO_ANALISADOR, hash_modelos()


def _escolher(con, p: rr.Pedido, cfg: dict, sementes: list[str], coleta_ids: list[int]) -> list[str]:
    novas = rr.candidatas(con, p, cfg, sementes, coleta_ids)
    return [i.video_id for i in novas.itens[: cfg["recomendacoes"]["analisar_top"]]]


def pendentes(con: sqlite3.Connection, ids: list[str], max_tentativas: int) -> list[str]:
    """As que ainda podem ser analisadas: sem análise ok e ainda com tentativas na fila."""
    out = []
    for v in ids:
        a = con.execute("SELECT status FROM analises WHERE video_id = ?", (v,)).fetchone()
        if a and a[0] in ("ok", "sem-audio"):
            continue
        q = con.execute("SELECT status, tentativas FROM fila_download WHERE video_id = ?", (v,)).fetchone()
        if q and (
            q["status"] == "sem-audio" or (q["status"] == "erro" and q["tentativas"] >= max_tentativas)
        ):
            continue
        out.append(v)
    return out


def ranquear(
    con: sqlite3.Connection, p: rr.Pedido, cfg: dict, sementes: list[str], coleta_ids: list[int],
    ref: dict | None = None, pool: list[str] | None = None,
) -> rr.Ranking:  # fmt: skip
    from curator.motor.executor import executar
    from curator.motor.receita import Receita

    p = p.com_padroes(cfg)
    if pool is None:
        pool = (
            json.loads(ref["analisar"])
            if ref and ref.get("analisar")
            else _escolher(con, p, cfg, sementes, coleta_ids)
        )
    base = rr.candidatas(con, p, cfg, sementes, coleta_ids, ref=ref)
    por_id = {i.video_id: i for i in base.itens}
    analisadas = set(espacos.analisadas(con))
    elegiveis = [v for v in pool if v in por_id and v in analisadas]
    motor: dict[str, float] = {}
    if elegiveis:
        r = Receita(
            nome="recomendacao", tamanho=min(1000, len(elegiveis)), universo="biblioteca",
            sementes=[s for s in sementes if s in analisadas], perfil_alvo=p.perfil_alvo,
            generos_preferidos=p.generos_preferidos, diversidade={"lambda": 1.0, "max_por_artista": None},
        )  # fmt: skip
        motor = {i.video_id: i.score for i in executar(con, r, universo=elegiveis).itens}
    itens = []
    for v in elegiveis:
        i = por_id[v]
        coleta = i.componentes["coleta"]
        pen = i.score / coleta if coleta else 1.0  # o fator de repetição que o ranking rápido já aplicou
        m = motor.get(v, 0.0)
        itens.append(rc.ItemRecomendado(
            v, round((PESO_MOTOR * m + (1 - PESO_MOTOR) * coleta) * pen, 6), i.fontes, i.votos, i.vezes_antes,
            False, {"motor": round(m, 6), "coleta": coleta, "_principal": i.componentes.get("_principal")},
        ))  # fmt: skip
    itens.sort(key=lambda i: (-i.score, i.video_id))
    novas = rr.Ranking(itens, base.descartes)
    novas.descartes["sem análise"] = len(pool) - len(elegiveis)
    if p.escopo == "novas":
        return rr.Ranking(rr.limitar(novas.itens, p.tamanho, p.max_por_artista), novas.descartes)
    return rr.misturar(con, p, cfg, sementes, coleta_ids, novas, ref)


def iniciar(
    con: sqlite3.Connection,
    p: rr.Pedido,
    cfg: dict,
    sementes: list[str],
    coleta_ids: list[int],
    texto: str | None,
    cli=None,
) -> rr.Resultado:
    from curator.audio import fila

    # confere a avaliação antes de baixar: dislike visto agora não gasta download (#60)
    pool = [
        i.video_id
        for i in rr.conferido(
            con, cli, cfg,
            lambda: rr.Ranking(rr.candidatas(con, p, cfg, sementes, coleta_ids).itens[: cfg["recomendacoes"]["analisar_top"]]),
        ).itens
    ]  # fmt: skip
    faltam = pendentes(con, pool, cfg["fila"]["max_tentativas"])
    fila.enfileirar_candidatas(con, faltam, *_versao())
    rid = rc.salvar(con, modo="lento", escopo=p.escopo, spec=p.spec(), sementes=sementes, coletas=coleta_ids,
                    itens=[], pedido=texto, status="aguardando_analise")  # fmt: skip
    con.execute("UPDATE recomendacoes SET analisar = ? WHERE recomendacao_id = ?", (json.dumps(pool), rid))
    if not faltam:
        return concluir(con, rid, cfg)
    return rr.Resultado(rid, "aguardando_analise", [], sementes, rr.Counter(), len(faltam))


def concluir(con: sqlite3.Connection, rid: int, cfg: dict, parcial: bool = False) -> rr.Resultado:
    """Fecha a recomendação quando as análises acabaram (ou já, com `parcial`, com o que estiver pronto)."""
    r = rc.obter(con, rid)
    if r is None:
        raise KeyError(f"recomendação #{rid} não existe")
    p = rr.Pedido.model_validate(r["spec"])
    pool = json.loads(
        con.execute("SELECT analisar FROM recomendacoes WHERE recomendacao_id = ?", (rid,)).fetchone()[0]
        or "[]"
    )
    faltam = pendentes(con, pool, cfg["fila"]["max_tentativas"])
    if r["status"] == "pronta":
        return rr.Resultado(rid, "pronta", [], r["sementes"], rr.Counter(), len(faltam))
    if faltam and not parcial:
        return rr.Resultado(rid, "aguardando_analise", [], r["sementes"], rr.Counter(), len(faltam))
    rk = ranquear(
        con, p, cfg, r["sementes"], r["coletas"], ref={**r, "analisar": json.dumps(pool)}, pool=pool
    )
    rc.concluir(con, rid, rk.itens)
    return rr.Resultado(rid, "pronta", rk.itens, r["sementes"], rk.descartes, len(faltam))
