"""Grade de avaliação do classificador nas filhas de uma mãe (#28, docs/avaliacao.md).

Leave-one-out sobre os membros das filhas, para cada espaço e combinação de k, α e regra de decisão:
- limiar: a regra do motor (entra em toda filha em que s ≥ τ, τ no percentil pc do LOO dos membros);
- escolha: a filha de maior s + β · (fração dos membros que são do mesmo artista principal), mais as que
  ficarem a até δ dela;
- artistas: a heurística do yt-music-helper (a filha cujo top-15 de artistas tem o artista da faixa).

A mãe não filtra aqui: toda faixa avaliada já é dela. Os números medem concordância com as filhas atuais,
o que só é acerto se elas foram montadas por você.

    CURATOR_DATA_DIR=<cópia do banco> uv run --extra organizar python contrib/avaliacao/grade_classificacao.py \\
        --mae 'Rock - All' [--espaco effnet --espaco interpretavel ...]
"""

import argparse
from collections import Counter, defaultdict
from itertools import product

import numpy as np

from curator.banco.conexao import abrir
from curator.config import caminhos
from curator.motor import espacos
from curator.motor.classificar import _centroide, _score, limiar_loo

KS, ALFAS, PERCENTIS = [5, 10, 20], [0.0, 0.6, 1.0], [10, 25, 50]
BETAS, DELTAS = [0.0, 0.25, 0.5, 0.75, 1.0], [0.0, 0.01]


def espaco(con, nome: str) -> espacos.Matriz:
    if nome == "interpretavel":
        m = espacos.interpretavel(con, ids=espacos.da_biblioteca(con))
        return espacos.Matriz(m.ids, espacos.normalizar_l2(m.X))
    return espacos.embeddings(con, f"{nome}_emb")


def macro(verdade: dict, previsto: dict, filhas: list[str]) -> tuple[float, dict]:
    por = {}
    for p in filhas:
        prev = [v for v in verdade if p in previsto[v]]
        real = [v for v in verdade if p in verdade[v]]
        ac = sum(p in verdade[v] for v in prev)
        pr, rc = (ac / len(prev) if prev else 0.0), (ac / len(real) if real else 0.0)
        por[p] = (len(real), pr, rc, 2 * pr * rc / (pr + rc) if pr + rc else 0.0)
    return float(np.mean([x[3] for x in por.values()])), por


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mae", required=True)
    ap.add_argument("--espaco", action="append", choices=["effnet", "musicnn", "interpretavel"])
    args = ap.parse_args()
    con = abrir(caminhos().banco)
    mae = con.execute("SELECT playlist_id FROM playlists WHERE titulo = ?", (args.mae,)).fetchone()[0]
    titulo = dict(
        con.execute("SELECT playlist_id, titulo FROM playlists WHERE mae_id = ? ORDER BY titulo", (mae,))
    )
    filhas = list(titulo)
    dislikes = {r[0] for r in con.execute("SELECT video_id FROM faixas WHERE avaliacao = 'dislike'")}
    artista = dict(con.execute("SELECT video_id, artista_id FROM faixa_artistas WHERE ordem = 0"))
    membros = defaultdict(set)
    for pid, vid in con.execute("SELECT playlist_id, video_id FROM playlist_faixas"):
        if pid in titulo and vid not in dislikes:
            membros[pid].add(vid)

    for nome in args.espaco or ["effnet"]:
        m = espaco(con, nome)
        verdade = defaultdict(set)
        for p in filhas:
            for v in membros[p] & set(m.indice):
                verdade[v].add(p)
        ids = {p: sorted(membros[p] & set(m.indice)) for p in filhas}
        E = {p: np.stack([m.linha(v) for v in ids[p]]) for p in filhas}
        frac = {
            (v, p): sum(artista.get(x) == artista.get(v) for x in membros[p] - {v}) / max(len(membros[p] - {v}), 1)
            for v in verdade for p in filhas
        }  # fmt: skip
        print(
            f"## {nome}: {len(verdade)} faixas, {sum(len(s) > 1 for s in verdade.values())} em mais de uma filha"
        )
        linhas = []
        for k, alfa in product(KS, ALFAS):
            base = {
                p: (_centroide(E[p]), {pc: limiar_loo(E[p], k, alfa, pc) for pc in PERCENTIS}) for p in filhas
            }
            sc = {}  # (v, p) → (score, {pc: limiar}) com v fora das filhas em que estava
            for v in verdade:
                for p in filhas:
                    if p in verdade[v]:
                        resto = np.delete(E[p], ids[p].index(v), axis=0)
                        lim = {pc: limiar_loo(resto, k, alfa, pc) for pc in PERCENTIS}
                        sc[(v, p)] = (_score(m.linha(v), resto, _centroide(resto), k, alfa), lim)
                    else:
                        sc[(v, p)] = (_score(m.linha(v), E[p], base[p][0], k, alfa), base[p][1])
            for pc in PERCENTIS:
                prev = {v: {p for p in filhas if sc[(v, p)][0] >= sc[(v, p)][1][pc]} for v in verdade}
                f1, _ = macro(verdade, prev, filhas)
                media = np.mean([len(x) for x in prev.values()])
                linhas.append((f"k={k} α={alfa} limiar pc={pc}", f1, media))
            for beta, delta in product(BETAS, DELTAS):
                prev = {}
                for v in verdade:
                    aj = {p: sc[(v, p)][0] + beta * frac[(v, p)] for p in filhas}
                    prev[v] = {p for p, x in aj.items() if x >= max(aj.values()) - delta}
                f1, _ = macro(verdade, prev, filhas)
                linhas.append(
                    (
                        f"k={k} α={alfa} escolha β={beta} δ={delta}",
                        f1,
                        np.mean([len(x) for x in prev.values()]),
                    )
                )
        for regra, f1, media in sorted(linhas, key=lambda x: (-x[1], x[0])):
            print(f"  {regra:<36} F1 macro {f1:.3f} · {media:.2f} filhas por faixa")

    prev = {}
    for v in verdade:
        prev[v] = set()
        for p in filhas:
            cont = Counter(artista.get(x) for x in membros[p] - {v})
            top = {a for a, _ in sorted(cont.items(), key=lambda x: (-x[1], str(x[0])))[:15]}
            if artista.get(v) in top:
                prev[v].add(p)
    f1, por = macro(verdade, prev, filhas)
    sem = sum(not x for x in prev.values()) / len(prev)
    print(f"## top-15 artistas: F1 macro {f1:.3f} · {sem:.0%} sem destino")
    for p, (n, pr, rc, f) in por.items():
        print(f"  {titulo[p][:45]:<45} {n:>3}  P {pr:.2f}  R {rc:.2f}  F1 {f:.2f}")


if __name__ == "__main__":
    main()
