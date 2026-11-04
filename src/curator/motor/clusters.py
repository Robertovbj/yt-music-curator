"""Reorganização (ADR-07): UMAP → HDBSCAN → pertencimento sobreposto → descrição → nome.

Determinístico: UMAP com `random_state` fixo (o umap-learn força 1 thread nesse caso), HDBSCAN não é
aleatório, e o resto é álgebra com desempate por id. Mesmos dados + mesmos parâmetros = mesmos clusters.

Sobreposição: o HDBSCAN dá uma partição só como ponto de partida. Cada cluster tem um núcleo (os membros
com probabilidade acima da mediana do cluster) e um centroide calculado pelo núcleo. Os membros são a partição
do HDBSCAN MAIS toda faixa com cos(t, centroide) ≥ τ_c, onde τ_c é o percentil 10 da similaridade dos membros
do HDBSCAN ao centroide. Ninguém sai do próprio cluster; a sobreposição só acrescenta. (O ADR-07 previa o
percentil 25 só do núcleo, o que deixava de fora um quarto do próprio núcleo e metade do estilo.)

Estabilidade: um agrupamento fica salvo. `atualizar` encaixa faixas novas nos clusters existentes pelo
centroide; só `agrupar` (sob pedido) recalcula tudo.
"""

import hashlib
import json
import sqlite3
import warnings
from collections import Counter
from dataclasses import asdict, dataclass, field
from itertools import combinations

import numpy as np

from curator.banco import vetores
from curator.banco.conexao import agora, transacao
from curator.motor import espacos


@dataclass(frozen=True)
class Parametros:
    dims: int = 10
    n_neighbors: int = 15
    min_dist: float = 0.0
    random_state: int = 42
    min_cluster_size: int = 12
    min_samples: int | None = None
    percentil_limiar: float = 10
    jaccard_fusao: float = 0.6


@dataclass
class Cluster:
    cluster_id: int
    nucleo: list[str]
    centroide: np.ndarray
    limiar: float
    membros: dict[str, float] = field(default_factory=dict)  # video_id → similaridade ao centroide
    nome: str = ""
    estatisticas: dict = field(default_factory=dict)

    @property
    def hash_nucleo(self) -> str:
        return hashlib.sha256("\n".join(sorted(self.nucleo)).encode()).hexdigest()[:16]


class PoucasFaixas(ValueError):
    pass


# ------------------------------------------------------------------ cálculo


def reduzir(X: np.ndarray, p: Parametros, dims: int | None = None) -> np.ndarray:
    import umap

    dims = dims or p.dims
    with warnings.catch_warnings():  # "n_jobs overridden to 1 by setting random_state": é o que queremos
        warnings.simplefilter("ignore")
        modelo = umap.UMAP(
            n_components=dims, n_neighbors=min(p.n_neighbors, len(X) - 1), min_dist=p.min_dist,
            metric="cosine", random_state=p.random_state,
        )  # fmt: skip
        return modelo.fit_transform(X).astype(np.float32)


def particionar(Y: np.ndarray, p: Parametros) -> tuple[np.ndarray, np.ndarray]:
    from sklearn.cluster import HDBSCAN

    h = HDBSCAN(min_cluster_size=p.min_cluster_size, min_samples=p.min_samples, copy=True).fit(Y)
    return h.labels_, h.probabilities_


def _centroide(E: np.ndarray) -> np.ndarray:
    c = E.mean(axis=0)
    return c / (np.linalg.norm(c) or 1)


def sobrepor(m: espacos.Matriz, rotulos: np.ndarray, probs: np.ndarray, p: Parametros) -> list[Cluster]:
    """Núcleo + centroide por cluster do HDBSCAN e atribuição sobreposta de toda faixa (inclusive ruído)."""
    out = []
    for novo_id, rot in enumerate(sorted(set(rotulos) - {-1})):
        idx = np.nonzero(rotulos == rot)[0]
        mediana = np.median(probs[idx])
        nuc = [i for i in idx if probs[i] >= mediana] or list(idx)
        c = _centroide(m.X[nuc])
        sims = m.X @ c
        limiar = float(np.percentile(sims[idx], p.percentil_limiar))
        dentro = set(idx.tolist()) | set(np.nonzero(sims >= limiar)[0].tolist())
        membros = {m.ids[i]: round(float(sims[i]), 6) for i in sorted(dentro)}
        out.append(Cluster(novo_id, sorted(m.ids[i] for i in nuc), c, limiar, membros))
    return out


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def sobreposicoes(cs: list[Cluster], minimo: float = 0.0) -> list[tuple[int, int, float]]:
    pares = [
        (a.cluster_id, b.cluster_id, round(jaccard(set(a.membros), set(b.membros)), 4))
        for a, b in combinations(cs, 2)
    ]
    return sorted((x for x in pares if x[2] >= minimo), key=lambda x: (-x[2], x[0], x[1]))


# ------------------------------------------------------------------ descrição e nome (#37)


def _rotulo_curto(r: str) -> str:
    return r.split("---", 1)[1] if "---" in r else r


def _faixa(v: float | None, cortes: tuple[float, float], nomes: tuple[str, str, str]) -> str | None:
    if v is None or np.isnan(v):
        return None
    return nomes[0] if v < cortes[0] else nomes[2] if v > cortes[1] else nomes[1]


def descrever(con: sqlite3.Connection, ids: list[str]) -> dict:
    """Estatísticas agregadas: o que a IA vai ler para nomear o cluster (e o que o nome automático usa)."""
    esc = espacos.escalares(con).subconjunto(ids)
    col = {c: i for i, c in enumerate(esc.colunas)}

    def med(nome: str) -> float:
        x = esc.X[:, col[nome]]
        return float(np.nanmedian(x)) if len(x) and not np.isnan(x).all() else float("nan")

    est = {"faixas": len(ids)}
    for nome in (
        "bpm",
        "energy",
        "valence",
        "arousal",
        "danceability",
        "acousticness",
        "aggressive",
        "relaxed",
        "instrumental",
    ):
        v = med(nome)
        est[nome] = None if np.isnan(v) else round(v, 3)
    for tipo, chave, n in (
        ("discogs400", "estilos", 5),
        ("jamendo_mood", "humores", 3),
        ("jamendo_instr", "instrumentos", 3),
    ):
        pm = espacos.probabilidades(con, tipo).subconjunto(ids)
        if len(pm.ids):
            media = pm.X.mean(axis=0)
            top = sorted(range(len(media)), key=lambda i: (-float(media[i]), pm.colunas[i]))[:n]
            est[chave] = [[pm.colunas[i], round(float(media[i]), 3)] for i in top]
    artistas = Counter()
    for (lista,) in con.execute(
        f"SELECT artistas FROM faixas WHERE video_id IN ({','.join('?' * len(ids))})", ids
    ):
        artistas.update(a["nome"] for a in json.loads(lista)[:1])
    est["artistas"] = [[a, n] for a, n in sorted(artistas.items(), key=lambda x: (-x[1], x[0]))[:5]]
    return est


def nome_automatico(est: dict) -> str:
    """Nome provisório, determinístico, a partir das estatísticas. A IA (M4) pode substituí-lo."""
    partes = []
    if est.get("estilos"):
        (r1, p1), *resto = est["estilos"]
        # o segundo estilo só entra se for relevante (≥ metade do primeiro); senão é ruído
        segundo = [_rotulo_curto(r) for r, p in resto[:1] if p >= 0.5 * p1]
        partes.append(" / ".join([_rotulo_curto(r1), *segundo]))
    energia = _faixa(est.get("energy"), (0.33, 0.66), ("calmo", "moderado", "energia alta"))
    humor = _faixa(est.get("valence"), (0.4, 0.6), ("melancólico", None, "alegre"))
    partes += [x for x in (energia, humor) if x]
    return " · ".join(partes) or "sem nome"


# ------------------------------------------------------------------ orquestração e persistência


def _universo(con: sqlite3.Connection, escopo: str) -> list[str]:
    """Faixas do escopo, sem as que têm dislike (#61) ou estão numa playlist ignorada (#77)."""
    from curator.banco import consultas

    if escopo == "biblioteca":
        return consultas.utilizaveis(con)
    dislikes = consultas.bloqueadas(con)
    return [
        r[0]
        for r in con.execute(
            """WITH RECURSIVE d(id) AS (SELECT ? UNION SELECT p.playlist_id FROM playlists p JOIN d ON p.mae_id = d.id
                                         WHERE p.ignorada = 0)
               SELECT DISTINCT video_id FROM playlist_faixas WHERE playlist_id IN (SELECT id FROM d) ORDER BY video_id""",
            (escopo,),
        )
        if r[0] not in dislikes
    ]


def calcular(
    con: sqlite3.Connection, escopo: str = "biblioteca", p: Parametros = Parametros()
) -> list[Cluster]:
    emb = espacos.embeddings(con)
    m = emb.subconjunto(_universo(con, escopo))
    if len(m.ids) < max(2 * p.min_cluster_size, p.n_neighbors + 2):
        raise PoucasFaixas(
            f"só {len(m.ids)} faixa(s) analisada(s) no escopo; o mínimo é {max(2 * p.min_cluster_size, p.n_neighbors + 2)}"
        )
    rotulos, probs = particionar(reduzir(m.X, p), p)
    cs = sobrepor(m, rotulos, probs, p)
    for c in cs:
        c.estatisticas = descrever(con, sorted(c.membros))
        c.nome = _nome_em_cache(con, c.hash_nucleo) or nome_automatico(c.estatisticas)
    return cs


def _nome_em_cache(con: sqlite3.Connection, hash_nucleo: str) -> str | None:
    """Mesmo núcleo = mesmo cluster: reaproveita o nome (inclusive um dado pela IA ou por você)."""
    linha = con.execute(
        "SELECT nome FROM clusters WHERE hash_nucleo = ? ORDER BY nome_origem = 'automatico', agrupamento_id DESC LIMIT 1",
        (hash_nucleo,),
    ).fetchone()
    return linha[0] if linha else None


def salvar(con: sqlite3.Connection, cs: list[Cluster], escopo: str, p: Parametros) -> int:
    with transacao(con):
        aid = con.execute(
            "INSERT INTO agrupamentos (escopo, parametros, hash_biblioteca, criado_em) VALUES (?, ?, ?, ?)",
            (escopo, json.dumps(asdict(p), sort_keys=True), espacos.hash_biblioteca(con), agora()),
        ).lastrowid
        for c in cs:
            con.execute(
                "INSERT INTO clusters (agrupamento_id, cluster_id, nome, hash_nucleo, centroide, limiar, estatisticas) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (aid, c.cluster_id, c.nome, c.hash_nucleo, vetores.para_blob(c.centroide), c.limiar,
                 json.dumps(c.estatisticas, ensure_ascii=False)),
            )  # fmt: skip
            nucleo = set(c.nucleo)
            con.executemany(
                "INSERT INTO cluster_membros (agrupamento_id, cluster_id, video_id, nucleo, similaridade) VALUES (?, ?, ?, ?, ?)",
                [(aid, c.cluster_id, v, int(v in nucleo), s) for v, s in sorted(c.membros.items())],
            )
    return aid


def ultimo(con: sqlite3.Connection, escopo: str = "biblioteca") -> int | None:
    linha = con.execute(
        "SELECT agrupamento_id FROM agrupamentos WHERE escopo = ? ORDER BY agrupamento_id DESC LIMIT 1",
        (escopo,),
    ).fetchone()
    return linha[0] if linha else None


def carregar(con: sqlite3.Connection, agrupamento_id: int) -> list[Cluster]:
    cs = []
    for r in con.execute(
        "SELECT * FROM clusters WHERE agrupamento_id = ? ORDER BY cluster_id", (agrupamento_id,)
    ):
        membros = con.execute(
            "SELECT video_id, nucleo, similaridade FROM cluster_membros WHERE agrupamento_id = ? AND cluster_id = ? ORDER BY video_id",
            (agrupamento_id, r["cluster_id"]),
        ).fetchall()
        c = Cluster(
            r["cluster_id"], [m["video_id"] for m in membros if m["nucleo"]],
            np.frombuffer(r["centroide"], dtype="<f4").copy(), r["limiar"],
            {m["video_id"]: m["similaridade"] for m in membros}, r["nome"], json.loads(r["estatisticas"]),
        )  # fmt: skip
        cs.append(c)
    return cs


def atualizar(con: sqlite3.Connection, agrupamento_id: int) -> int:
    """Âncoras: encaixa nos clusters salvos as faixas analisadas depois, sem mexer nos membros antigos."""
    escopo = con.execute(
        "SELECT escopo FROM agrupamentos WHERE agrupamento_id = ?", (agrupamento_id,)
    ).fetchone()[0]
    emb = espacos.embeddings(con).subconjunto(_universo(con, escopo))
    # Faixas que ficaram sem cluster também são reavaliadas: com τ e centroides fixos, o resultado não muda.
    vistos = {
        r[0]
        for r in con.execute(
            "SELECT DISTINCT video_id FROM cluster_membros WHERE agrupamento_id = ?", (agrupamento_id,)
        )
    }
    novos = [v for v in emb.ids if v not in vistos]
    inseridos = 0
    with transacao(con):
        for c in carregar(con, agrupamento_id):
            for v in novos:
                s = float(emb.linha(v) @ c.centroide)
                if s >= c.limiar:
                    con.execute(
                        "INSERT OR IGNORE INTO cluster_membros (agrupamento_id, cluster_id, video_id, nucleo, similaridade) VALUES (?, ?, ?, 0, ?)",
                        (agrupamento_id, c.cluster_id, v, round(s, 6)),
                    )
                    inseridos += 1
    return inseridos


def renomear(
    con: sqlite3.Connection, agrupamento_id: int, cluster_id: int, nome: str, origem: str = "usuario"
) -> None:
    con.execute(
        "UPDATE clusters SET nome = ?, nome_origem = ? WHERE agrupamento_id = ? AND cluster_id = ?",
        (nome, origem, agrupamento_id, cluster_id),
    )


def obter(con: sqlite3.Connection, escopo: str = "biblioteca", p: Parametros = Parametros(),
          reagrupar: bool = False) -> tuple[int, list[Cluster], int]:  # fmt: skip
    """(agrupamento_id, clusters, faixas novas encaixadas): reaproveita o último agrupamento, salvo `reagrupar`."""
    aid = ultimo(con, escopo)
    if aid is None or reagrupar:
        cs = calcular(con, escopo, p)
        return salvar(con, cs, escopo, p), cs, 0
    novos = atualizar(con, aid)
    return aid, carregar(con, aid), novos
