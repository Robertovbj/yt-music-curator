"""Os dois espaços de cada faixa (ADR-04).

1. Embedding: média do EffNet-Discogs (1280-d), normalizada L2 — "som parecido", kNN, centroides.
2. Interpretável: features escalares + probabilidades, z-score sobre a biblioteca, com peso por grupo
   dividido por √(dimensão do grupo), para os 400 gêneros não engolirem as ~16 features escalares.
"""

import hashlib
import sqlite3
import warnings
from dataclasses import dataclass, field

import numpy as np

from curator.banco import vetores

# Colunas de `features` que entram no espaço interpretável e na receita (perfil-alvo, mínimos e máximos).
ESCALARES = (
    "danceability", "acousticness", "aggressive", "electronic", "happy", "sad", "relaxed", "party",
    "instrumental", "voz_feminina", "engagement", "approachability", "valence", "arousal", "energy", "bpm",
)  # fmt: skip

TIPOS_PROBABILIDADE = ("discogs400", "jamendo_genre", "jamendo_mood", "jamendo_instr")

PESOS_PADRAO = {
    "escalares": 1.0,
    "discogs400": 1.0,
    "jamendo_genre": 0.5,
    "jamendo_mood": 0.5,
    "jamendo_instr": 0.3,
}


@dataclass
class Matriz:
    ids: list[str]
    X: np.ndarray  # (N, d) float32
    colunas: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.indice = {v: i for i, v in enumerate(self.ids)}

    def linha(self, video_id: str) -> np.ndarray:
        return self.X[self.indice[video_id]]

    def subconjunto(self, ids: list[str]) -> Matriz:
        sel = [self.indice[i] for i in ids if i in self.indice]
        return Matriz([self.ids[i] for i in sel], self.X[sel], self.colunas)


def analisadas(con: sqlite3.Connection) -> list[str]:
    """Faixas com análise ok (só elas entram no motor), em ordem estável."""
    return [r[0] for r in con.execute("SELECT video_id FROM analises WHERE status = 'ok' ORDER BY video_id")]


def hash_biblioteca(con: sqlite3.Connection) -> str:
    """Muda quando entra/sai uma análise ou ela é refeita: chave de cache e "versão do banco" das execuções."""
    h = hashlib.sha256()
    for vid, quando in con.execute(
        "SELECT video_id, analisada_em FROM analises WHERE status = 'ok' ORDER BY video_id"
    ):
        h.update(f"{vid}:{quando}\n".encode())
    return h.hexdigest()[:16]


def normalizar_l2(X: np.ndarray) -> np.ndarray:
    normas = np.linalg.norm(X, axis=1, keepdims=True)
    return (X / np.where(normas == 0, 1, normas)).astype(np.float32)


_cache: dict[tuple, Matriz] = {}


def embeddings(con: sqlite3.Connection, tipo: str = "effnet_emb") -> Matriz:
    chave = ("emb", tipo, str(con.execute("PRAGMA database_list").fetchone()[2]), hash_biblioteca(con))
    if chave not in _cache:
        linhas = con.execute(
            "SELECT v.video_id, v.dim, v.dados FROM vetores v JOIN analises a USING (video_id) "
            "WHERE a.status = 'ok' AND v.tipo = ? ORDER BY v.video_id",
            (tipo,),
        ).fetchall()
        if not linhas:
            return Matriz([], np.zeros((0, 0), np.float32))
        X = np.stack([vetores.de_blob(r["dados"], r["dim"]) for r in linhas])
        _cache[chave] = Matriz([r["video_id"] for r in linhas], normalizar_l2(X))
    return _cache[chave]


def probabilidades(con: sqlite3.Connection, tipo: str) -> Matriz:
    """Vetores completos de probabilidade (sem normalizar), com os rótulos como colunas."""
    chave = ("prob", tipo, str(con.execute("PRAGMA database_list").fetchone()[2]), hash_biblioteca(con))
    if chave not in _cache:
        rotulos = [
            r[0]
            for r in con.execute("SELECT rotulo FROM rotulos_modelo WHERE tipo = ? ORDER BY indice", (tipo,))
        ]
        linhas = con.execute(
            "SELECT v.video_id, v.dim, v.dados FROM vetores v JOIN analises a USING (video_id) "
            "WHERE a.status = 'ok' AND v.tipo = ? ORDER BY v.video_id",
            (tipo,),
        ).fetchall()
        X = (
            np.stack([vetores.de_blob(r["dados"], r["dim"]) for r in linhas])
            if linhas
            else np.zeros((0, len(rotulos)), np.float32)
        )
        _cache[chave] = Matriz([r["video_id"] for r in linhas], X, rotulos)
    return _cache[chave]


def escalares(con: sqlite3.Connection) -> Matriz:
    """Features escalares brutas (NaN onde faltar), uma coluna por item de ESCALARES."""
    linhas = con.execute(
        f"SELECT f.video_id, {', '.join('f.' + c for c in ESCALARES)} FROM features f "
        "JOIN analises a USING (video_id) WHERE a.status = 'ok' ORDER BY f.video_id"
    ).fetchall()
    X = np.array(
        [[np.nan if v is None else v for v in tuple(r)[1:]] for r in linhas], dtype=np.float32
    ).reshape(-1, len(ESCALARES))
    return Matriz([r[0] for r in linhas], X, list(ESCALARES))


def zscore(X: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(Z, média, desvio) por coluna; NaN vira 0 (= a média), desvio zero vira 1."""
    with warnings.catch_warnings():  # coluna toda NaN (feature ausente) vira média 0, desvio 1 logo abaixo
        warnings.simplefilter("ignore", RuntimeWarning)
        media = np.nanmean(X, axis=0) if len(X) else np.zeros(X.shape[1])
        desvio = np.nanstd(X, axis=0) if len(X) else np.ones(X.shape[1])
    desvio = np.where((desvio == 0) | np.isnan(desvio), 1, desvio)
    media = np.nan_to_num(media)
    Z = np.nan_to_num((X - media) / desvio)
    return Z.astype(np.float32), media, desvio


def interpretavel(con: sqlite3.Connection, pesos: dict[str, float] | None = None) -> Matriz:
    """Escalares + probabilidades em z-score, cada grupo com peso/√dim. Colunas nomeadas "grupo:rótulo"."""
    pesos = {**PESOS_PADRAO, **(pesos or {})}
    base = escalares(con)
    blocos, colunas = [], []
    if pesos["escalares"]:
        Z, _, _ = zscore(base.X)
        blocos.append(Z * pesos["escalares"] / np.sqrt(Z.shape[1]))
        colunas += [f"escalares:{c}" for c in base.colunas]
    for tipo in TIPOS_PROBABILIDADE:
        if not pesos.get(tipo):
            continue
        p = probabilidades(con, tipo).subconjunto(base.ids)
        if len(p.ids) != len(base.ids):
            continue  # análises antigas sem este vetor: o grupo fica de fora em vez de desalinhar
        Z, _, _ = zscore(p.X)
        blocos.append(Z * pesos[tipo] / np.sqrt(max(Z.shape[1], 1)))
        colunas += [f"{tipo}:{r}" for r in p.colunas]
    X = np.hstack(blocos) if blocos else np.zeros((len(base.ids), 0), np.float32)
    return Matriz(base.ids, X.astype(np.float32), colunas)


def vizinhos(
    m: Matriz, consulta: np.ndarray, k: int = 10, excluir: set[str] | None = None
) -> list[tuple[str, float]]:
    """Top-k por cosseno (as linhas de `m` já estão normalizadas). Empate: video_id. Determinístico."""
    if not len(m.ids):
        return []
    q = consulta / (np.linalg.norm(consulta) or 1)
    sims = m.X @ q.astype(np.float32)
    excluir = excluir or set()
    precisa = min(len(sims), k + len(excluir))
    if precisa < len(sims):
        corte = np.partition(sims, -precisa)[-precisa]
        candidatos = np.nonzero(sims >= corte)[0]  # inclui empates na fronteira: resultado exato
    else:
        candidatos = np.arange(len(sims))
    ordem = sorted(candidatos.tolist(), key=lambda i: (-float(sims[i]), m.ids[i]))
    return [(m.ids[i], float(sims[i])) for i in ordem if m.ids[i] not in excluir][:k]
