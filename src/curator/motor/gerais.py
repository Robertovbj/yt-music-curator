"""Playlists gerais a partir das curtidas (#80), sem olhar nenhuma playlist existente.

Só a matemática do projeto: clusters sobrepostos (ADR-07) sobre todas as curtidas analisadas, com o HDBSCAN no
modo "leaf" e tamanho mínimo maior, para dar poucas playlists amplas. Na biblioteca real (04/11, 2.647 curtidas):
o modo padrão ("eom") juntava tudo em 2 blocos de 1.100–1.800 faixas, qualquer que fosse o tamanho mínimo; "leaf"
com mínimo 40 deu 14 grupos de 100 a 640 faixas, cobrindo 86% das curtidas.

O plano só CRIA playlists novas: nenhuma existente é lida nem alterada. Quando aplicado, as criadas ficam
marcadas como gerais (`playlists.geral`), e `curator classificar --nas-gerais` passa a encaixar nelas as
curtidas novas.
"""

import sqlite3
from dataclasses import replace

from curator.motor import clusters
from curator.motor.clusters import CURTIDAS, Cluster, Parametros
from curator.motor.planos import Operacao, criar

PARAMETROS = Parametros(min_cluster_size=40, selecao="leaf")
ORIGEM = "gerais"


def parametros(min_cluster: int | None = None) -> Parametros:
    return PARAMETROS if min_cluster is None else replace(PARAMETROS, min_cluster_size=min_cluster)


def obter(
    con: sqlite3.Connection, p: Parametros = PARAMETROS, reagrupar: bool = False
) -> tuple[int, list[Cluster], int]:
    """(agrupamento_id, clusters, faixas novas encaixadas). Outros parâmetros (ou `reagrupar`) recalculam."""
    return clusters.obter(con, CURTIDAS, p, reagrupar)


def plano(con: sqlite3.Connection, agrupamento_id: int, cs: list[Cluster]) -> int:
    """Plano que cria uma playlist nova por cluster, com todos os membros (a sobreposição vale: uma faixa pode ir
    para mais de uma). Não toca nenhuma playlist existente."""
    nomes = clusters.nomes_unicos(cs)
    ops = []
    for c in sorted(cs, key=lambda c: (-len(c.membros), c.cluster_id)):
        alvo = f"nova:{nomes[c.cluster_id]}"
        membros = sorted(c.membros)
        ops += [
            Operacao("criar", alvo, titulo=nomes[c.cluster_id]),
            Operacao("adicionar", alvo, membros, motivo={v: f"cluster [{c.cluster_id}], sim {c.membros[v]:.2f}" for v in membros}),
        ]  # fmt: skip
    return criar(con, f"{ORIGEM}:{agrupamento_id}", ops, propagar_agregadoras=False)


def playlists(con: sqlite3.Connection) -> list[str]:
    """As playlists gerais que já existem (criadas por um plano das gerais aplicado)."""
    return [
        r[0]
        for r in con.execute(
            "SELECT playlist_id FROM playlists WHERE geral = 1 AND propria = 1 AND ignorada = 0 ORDER BY playlist_id"
        )
    ]
