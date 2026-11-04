"""Coordenadas UMAP 2D e 3D para o visualizador (ADR-11, #39), separadas da UMAP de ~10-d do clustering.

Cache na tabela `projecoes`, válido enquanto o `hash_biblioteca` não muda (entrou ou foi refeita uma análise).
"""

import sqlite3
from collections.abc import Callable

import numpy as np

from curator.banco import vetores
from curator.banco.conexao import transacao
from curator.motor import espacos
from curator.motor.clusters import Parametros, reduzir

# Só a biblioteca utilizável (#61): faixas guardadas fora dela e candidatas de recomendação não entram no mapa.
ESPACOS: dict[str, Callable[[sqlite3.Connection], espacos.Matriz]] = {
    "effnet": lambda con: espacos.embeddings(con, "effnet_emb").subconjunto(espacos.da_biblioteca(con)),
    "musicnn": lambda con: espacos.embeddings(con, "musicnn_emb").subconjunto(espacos.da_biblioteca(con)),
    "interpretavel": lambda con: espacos.interpretavel(con, ids=espacos.da_biblioteca(con)),
}

# Para olhar, um pouco de espaço entre os pontos ajuda (no clustering min_dist é 0).
VISUAL = Parametros(min_dist=0.1, n_neighbors=15)


def projetar(con: sqlite3.Connection, espaco: str, dims: int) -> espacos.Matriz:
    if espaco not in ESPACOS:
        raise ValueError(f"espaço desconhecido: {espaco} (use {', '.join(ESPACOS)})")
    h = espacos.hash_biblioteca(con)
    m = ESPACOS[espaco](con)
    linhas = con.execute(
        "SELECT video_id, coords FROM projecoes WHERE espaco = ? AND dims = ? AND hash_biblioteca = ? ORDER BY video_id",
        (espaco, dims, h),
    ).fetchall()
    if linhas and [r[0] for r in linhas] == m.ids:
        return espacos.Matriz(m.ids, np.stack([vetores.de_blob(r[1], dims) for r in linhas]))
    if len(m.ids) < VISUAL.n_neighbors + 2:
        return espacos.Matriz([], np.zeros((0, dims), np.float32))
    Y = reduzir(m.X, VISUAL, dims=dims)
    with transacao(con):
        con.execute("DELETE FROM projecoes WHERE espaco = ? AND dims = ?", (espaco, dims))
        con.executemany(
            "INSERT INTO projecoes (espaco, dims, hash_biblioteca, video_id, coords) VALUES (?, ?, ?, ?, ?)",
            [(espaco, dims, h, v, vetores.para_blob(Y[i])) for i, v in enumerate(m.ids)],
        )
    return espacos.Matriz(m.ids, Y)
