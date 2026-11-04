"""Coordenadas UMAP 2D e 3D para o visualizador (ADR-11, #39), separadas da UMAP de ~10-d do clustering.

Cache na tabela `projecoes`, válido enquanto o `hash_biblioteca` não muda (entrou ou foi refeita uma análise).
Depois do UMAP, a nuvem é orientada e cada eixo ganha as correlações com as features (#57, `eixos`); isso fica
em `projecao_eixos`, com a mesma chave. Uma projeção em cache sem os eixos da versão atual é refeita.
"""

import json
import sqlite3
from collections.abc import Callable

import numpy as np

from curator.banco import vetores
from curator.banco.conexao import transacao
from curator.motor import espacos
from curator.motor.clusters import Parametros, reduzir
from curator.visual import eixos as ex

# Só a biblioteca utilizável (#61): faixas guardadas fora dela e candidatas de recomendação não entram no mapa.
ESPACOS: dict[str, Callable[[sqlite3.Connection], espacos.Matriz]] = {
    "effnet": lambda con: espacos.embeddings(con, "effnet_emb").subconjunto(espacos.da_biblioteca(con)),
    "musicnn": lambda con: espacos.embeddings(con, "musicnn_emb").subconjunto(espacos.da_biblioteca(con)),
    "interpretavel": lambda con: espacos.interpretavel(con, ids=espacos.da_biblioteca(con)),
}

# Para olhar, um pouco de espaço entre os pontos ajuda (no clustering min_dist é 0).
VISUAL = Parametros(min_dist=0.1, n_neighbors=15)
VERSAO_EIXOS = 1


def _em_cache(
    con: sqlite3.Connection, espaco: str, dims: int, h: str, ids: list[str]
) -> espacos.Matriz | None:
    ok = con.execute(
        "SELECT 1 FROM projecao_eixos WHERE espaco = ? AND dims = ? AND hash_biblioteca = ? AND versao = ?",
        (espaco, dims, h, VERSAO_EIXOS),
    ).fetchone()
    if not ok:
        return None
    linhas = con.execute(
        "SELECT video_id, coords FROM projecoes WHERE espaco = ? AND dims = ? AND hash_biblioteca = ? ORDER BY video_id",
        (espaco, dims, h),
    ).fetchall()
    if not linhas or [r[0] for r in linhas] != ids:
        return None
    return espacos.Matriz(ids, np.stack([vetores.de_blob(r[1], dims) for r in linhas]))


def projetar(con: sqlite3.Connection, espaco: str, dims: int) -> espacos.Matriz:
    if espaco not in ESPACOS:
        raise ValueError(f"espaço desconhecido: {espaco} (use {', '.join(ESPACOS)})")
    h = espacos.hash_biblioteca(con)
    m = ESPACOS[espaco](con)
    if (salvo := _em_cache(con, espaco, dims, h, m.ids)) is not None:
        return salvo
    if len(m.ids) < VISUAL.n_neighbors + 2:
        return espacos.Matriz([], np.zeros((0, dims), np.float32))
    refs = ex.referencias(con, m.ids)
    Y = ex.orientar(reduzir(m.X, VISUAL, dims=dims), refs[ex.NOMES[ex.REFERENCIA]])
    rotulos = [e.como_dict() for e in ex.correlacoes(Y, refs)]
    with transacao(con):
        con.execute("DELETE FROM projecoes WHERE espaco = ? AND dims = ?", (espaco, dims))
        con.executemany(
            "INSERT INTO projecoes (espaco, dims, hash_biblioteca, video_id, coords) VALUES (?, ?, ?, ?, ?)",
            [(espaco, dims, h, v, vetores.para_blob(Y[i])) for i, v in enumerate(m.ids)],
        )
        con.execute(
            "INSERT OR REPLACE INTO projecao_eixos (espaco, dims, hash_biblioteca, versao, eixos) VALUES (?, ?, ?, ?, ?)",
            (espaco, dims, h, VERSAO_EIXOS, json.dumps(rotulos, ensure_ascii=False)),
        )
    return espacos.Matriz(m.ids, Y)


def eixos(con: sqlite3.Connection, espaco: str, dims: int) -> list[dict]:
    """Rótulo e correlações de cada eixo da última `projetar(espaco, dims)` (vazio se não houver)."""
    linha = con.execute(
        "SELECT eixos FROM projecao_eixos WHERE espaco = ? AND dims = ? AND hash_biblioteca = ? AND versao = ?",
        (espaco, dims, espacos.hash_biblioteca(con), VERSAO_EIXOS),
    ).fetchone()
    return json.loads(linha[0]) if linha else []
