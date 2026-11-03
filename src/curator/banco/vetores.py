"""Vetores float32 em BLOB (tabela `vetores`)."""

import sqlite3

import numpy as np

DTYPE = np.dtype("<f4")


def para_blob(v) -> bytes:
    return np.ascontiguousarray(v, dtype=DTYPE).tobytes()


def de_blob(dados: bytes, dim: int) -> np.ndarray:
    v = np.frombuffer(dados, dtype=DTYPE)
    if v.size != dim:
        raise ValueError(f"vetor com {v.size} valores, esperado {dim}")
    return v


def gravar(con: sqlite3.Connection, video_id: str, tipo: str, v) -> None:
    v = np.asarray(v, dtype=DTYPE).ravel()
    con.execute(
        "INSERT INTO vetores (video_id, tipo, dim, dados) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (video_id, tipo) DO UPDATE SET dim = excluded.dim, dados = excluded.dados",
        (video_id, tipo, v.size, para_blob(v)),
    )


def ler(con: sqlite3.Connection, video_id: str, tipo: str) -> np.ndarray | None:
    linha = con.execute(
        "SELECT dim, dados FROM vetores WHERE video_id = ? AND tipo = ?", (video_id, tipo)
    ).fetchone()
    return None if linha is None else de_blob(linha["dados"], linha["dim"])
