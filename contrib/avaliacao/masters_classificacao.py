"""Quanto o classificador acerta nas playlists master (#105, docs/avaliacao.md).

Leave-one-out: cada faixa sai da sua master, é pontuada contra todas (score do ADR-06, com os limiares do motor) e
a decisão é comparada com as masters em que ela estava. Regras comparadas:
- limiar: toda master em que s ≥ τ (a regra multi-rótulo do ADR-06);
- duas: a de maior score e, a até `fronteira` dela, a segunda (a regra com que as masters foram criadas).

As masters nasceram de clusters do mesmo embedding, então os números são um teto, não o acerto em curtidas novas.

    CURATOR_DATA_DIR=<cópia do banco> uv run --extra organizar python contrib/avaliacao/masters_classificacao.py
"""

import numpy as np

from curator.banco import consultas
from curator.banco.conexao import abrir
from curator.config import caminhos
from curator.motor import classificar as cl
from curator.motor import espacos, gerais

FRONTEIRAS = (0.0, 0.01, 0.02, 0.03, 0.05)


def scores_loo(X: np.ndarray, membros: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    """(score de cada faixa em cada master, tirando a própria faixa quando é membro; limiar de cada master)."""
    S, lim = np.zeros((len(X), len(membros))), np.zeros(len(membros))
    for j, idx in enumerate(membros):
        E = X[idx]
        sims = X @ E.T
        sims[idx, np.arange(len(idx))] = -np.inf
        topk = np.sort(sims, axis=1)[:, -min(cl.K, len(idx) - 1) :].mean(axis=1)
        membro = np.zeros(len(X), bool)
        membro[idx] = True
        C = np.where(membro[:, None], E.sum(axis=0) - X, E.sum(axis=0))
        C /= np.linalg.norm(C, axis=1, keepdims=True)
        S[:, j] = cl.ALFA * topk + (1 - cl.ALFA) * (X * C).sum(axis=1)
        lim[j] = np.percentile(S[idx, j], cl.PERCENTIL)
    return S, lim


def medir(S, lim, real, regra) -> tuple[float, float]:
    tp = fp = fn = 0
    for s, r in zip(S, real, strict=True):
        prev = regra(s, lim)
        tp, fp, fn = tp + len(prev & r), fp + len(prev - r), fn + len(r - prev)
    return tp / max(tp + fp, 1), tp / max(tp + fn, 1)


def duas(fronteira: float):
    def regra(s, lim):
        ok = sorted(np.flatnonzero(s >= lim).tolist(), key=lambda j: -s[j])
        return set(ok[:1] + [j for j in ok[1:2] if s[j] >= s[ok[0]] - fronteira])

    return regra


def main() -> None:
    con = abrir(caminhos().banco)
    emb, fora = espacos.embeddings(con), consultas.bloqueadas(con)
    pids = gerais.playlists(con)
    de = {
        p: {r[0] for r in con.execute("SELECT video_id FROM playlist_faixas WHERE playlist_id = ?", (p,))}
        for p in pids
    }
    ids = sorted({v for m in de.values() for v in m if v in emb.indice and v not in fora})
    pos = {v: i for i, v in enumerate(ids)}
    X = np.stack([emb.linha(v) for v in ids])
    S, lim = scores_loo(X, [np.array(sorted(pos[v] for v in de[p] if v in pos)) for p in pids])
    real = [{j for j, p in enumerate(pids) if v in de[p]} for v in ids]
    print(
        f"{len(ids)} faixas em {len(pids)} masters; em duas ou mais: {np.mean([len(r) > 1 for r in real]):.1%}"
    )
    print(f"{'regra':28s} precisão  revocação")
    p, r = medir(S, lim, real, lambda s, lim: set(np.flatnonzero(s >= lim).tolist()))
    print(f"{'limiar (todas com s ≥ τ)':28s} {p:8.3f}  {r:9.3f}")
    for f in FRONTEIRAS:
        p, r = medir(S, lim, real, duas(f))
        print(f"{f'duas, fronteira {f:.2f}':28s} {p:8.3f}  {r:9.3f}")


if __name__ == "__main__":
    main()
