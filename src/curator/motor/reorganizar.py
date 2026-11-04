"""Clusters → sugestões de reorganização, comparadas com a árvore atual (ADR-07, #38).

- cluster parecido com uma playlist existente (Jaccard ≥ 0,5) → completar a playlist com o que falta
- cluster sem playlist correspondente → criar uma playlist nova com o nome do cluster
- dois clusters (ou duas playlists) muito sobrepostos (Jaccard ≥ 0,6) → talvez sejam uma só (fusão)
- playlist cujos membros se dividem entre clusters distintos → talvez sejam duas (divisão)

Só criar e completar viram operações de plano: fusão e divisão mexem no que você já organizou, então
ficam como sugestões em texto para você decidir. A comparação usa apenas faixas já analisadas.

`recategorizar` (#74) é o pedido explícito de refazer as filhas de UMA mãe: as filhas atuais não servem de
referência, cada cluster vira uma filha nova e as antigas são esvaziadas (o que sai delas continua na mãe).
"""

import sqlite3
from dataclasses import dataclass, field
from itertools import combinations

from curator.banco import consultas
from curator.motor import espacos
from curator.motor.clusters import Cluster, _universo, jaccard, nomes_unicos, sobreposicoes
from curator.motor.planos import Operacao

CORRESPONDE = 0.5
FATIA_DIVISAO = 0.3


@dataclass
class Sugestao:
    tipo: str  # criar | completar | fundir | dividir
    texto: str
    operacoes: list[Operacao] = field(default_factory=list)


def _playlists(con: sqlite3.Connection, escopo: str, analisadas: set[str]) -> dict[str, tuple[str, set[str]]]:
    universo = set(_universo(con, escopo)) if escopo != "biblioteca" else None
    out = {}
    for pid, titulo in con.execute(
        "SELECT playlist_id, titulo FROM playlists WHERE propria = 1 AND descobertas = 0 AND ignorada = 0 ORDER BY playlist_id"
    ):
        membros = {
            r[0] for r in con.execute("SELECT video_id FROM playlist_faixas WHERE playlist_id = ?", (pid,))
        } & analisadas
        if membros and (universo is None or membros <= universo):
            out[pid] = (titulo, membros)
    return out


def sugerir(
    con: sqlite3.Connection, cs: list[Cluster], escopo: str = "biblioteca", jaccard_fusao: float = 0.6
) -> list[Sugestao]:
    analisadas = set(espacos.analisadas(con)) - consultas.bloqueadas(con)
    pls = _playlists(con, escopo, analisadas)
    out: list[Sugestao] = []

    for c in cs:
        membros = set(c.membros)
        melhor = max(pls.items(), key=lambda kv: (jaccard(membros, kv[1][1]), kv[0]), default=None)
        j = jaccard(membros, melhor[1][1]) if melhor else 0.0
        if melhor and j >= CORRESPONDE:
            pid, (titulo, atuais) = melhor
            faltam = sorted(membros - atuais)
            if faltam:
                out.append(Sugestao(
                    "completar", f"“{c.nome}” corresponde a “{titulo}” (Jaccard {j:.2f}): {len(faltam)} faixa(s) parecida(s) fora dela",
                    [Operacao("adicionar", pid, faltam, motivo={v: f"cluster “{c.nome}”, sim {c.membros[v]:.2f}" for v in faltam})],
                ))  # fmt: skip
        else:
            alvo = f"nova:{c.nome}"
            ref = f"; a mais próxima é “{melhor[1][0]}” (Jaccard {j:.2f})" if melhor else ""
            out.append(Sugestao(
                "criar", f"“{c.nome}”: {len(membros)} faixa(s) sem playlist correspondente{ref}",
                [Operacao("criar", alvo, titulo=c.nome),
                 Operacao("adicionar", alvo, sorted(membros), motivo={v: f"sim {c.membros[v]:.2f}" for v in membros})],
            ))  # fmt: skip

    nomes = {c.cluster_id: c.nome for c in cs}
    for a, b, j in sobreposicoes(cs, jaccard_fusao):
        out.append(
            Sugestao(
                "fundir",
                f"os clusters “{nomes[a]}” e “{nomes[b]}” se sobrepõem (Jaccard {j:.2f}): talvez sejam uma playlist só",
            )
        )
    for (_pa, (ta, ma)), (_pb, (tb, mb)) in combinations(sorted(pls.items()), 2):
        if (j := jaccard(ma, mb)) >= jaccard_fusao:
            out.append(Sugestao("fundir", f"as playlists “{ta}” e “{tb}” se sobrepõem (Jaccard {j:.2f})"))

    for _pid, (titulo, membros) in sorted(pls.items()):
        if len(membros) < 10:
            continue
        fatias = [(c, len(membros & set(c.membros)) / len(membros)) for c in cs]
        fortes = [(c, f) for c, f in fatias if f >= FATIA_DIVISAO]
        separados = [
            (x, y)
            for (x, _), (y, _) in combinations(fortes, 2)
            if jaccard(set(x.membros), set(y.membros)) < 0.3
        ]
        if separados:
            x, y = separados[0]
            out.append(
                Sugestao(
                    "dividir",
                    f"“{titulo}” se divide entre “{x.nome}” e “{y.nome}”: talvez sejam duas playlists",
                )
            )
    return out


def filhas_atuais(con: sqlite3.Connection, mae: str) -> list[str]:
    """As descendentes da mãe (sem ela), válidas para o motor."""
    return [
        r[0]
        for r in con.execute(
            """WITH RECURSIVE d(id) AS (
                   SELECT playlist_id FROM playlists WHERE mae_id = ? AND ignorada = 0
                   UNION SELECT p.playlist_id FROM playlists p JOIN d ON p.mae_id = d.id WHERE p.ignorada = 0)
               SELECT id FROM d ORDER BY id""",
            (mae,),
        )
    ]


def recategorizar(con: sqlite3.Connection, mae: str, cs: list[Cluster]) -> list[Operacao]:
    """Uma filha nova por cluster (com todos os membros; a sobreposição vale) e as filhas atuais esvaziadas.

    Ninguém sai da mãe: as faixas já estão nela (o escopo dos clusters é a mãe com as descendentes)."""
    if not cs:
        raise ValueError("nenhum cluster: nada para recategorizar")
    nomes = nomes_unicos(cs)
    ops: list[Operacao] = []
    for c in sorted(cs, key=lambda c: (-len(c.membros), c.cluster_id)):
        alvo = f"nova:{nomes[c.cluster_id]}"
        membros = sorted(c.membros)
        ops += [
            Operacao("criar", alvo, titulo=nomes[c.cluster_id]),
            Operacao("adicionar", alvo, membros, motivo={v: f"cluster [{c.cluster_id}], sim {c.membros[v]:.2f}" for v in membros}),
        ]  # fmt: skip
    for pid in filhas_atuais(con, mae):
        ids = [
            r[0]
            for r in con.execute(
                "SELECT DISTINCT video_id FROM playlist_faixas WHERE playlist_id = ?", (pid,)
            )
        ]
        if ids:
            ops.append(
                Operacao(
                    "remover", pid, sorted(ids), motivo=dict.fromkeys(ids, "recategorização: continua na mãe")
                )
            )
    return ops
