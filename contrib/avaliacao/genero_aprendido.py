"""Avaliação do gênero-pai aprendido (#93, ADR-17; docs/avaliacao.md).

Compara, no gabarito pessoal da #83, as três réguas: a v2 (ADR-14), a com o MAEST (ADR-15) e a aprendida com os
rótulos (`motor/aprendido.py`). A aprendida é medida deixando o artista INTEIRO de fora (10 dobras por artista): o
modelo de cada dobra nunca viu uma faixa do artista que está prevendo. Mostra a AUC, a revocação e os falsos
positivos no 0,8 por artista, o rap faixa a faixa e o rock japonês (artistas em `--jrock`).

Usa os rótulos do banco (`curator generos importar-gabarito` antes). Rode numa CÓPIA do banco:

    CURATOR_DATA_DIR=<cópia> uv run python contrib/avaliacao/genero_aprendido.py
"""

import argparse
import json

import numpy as np

from curator.banco.conexao import abrir
from curator.config import caminhos
from curator.motor import aprendido, espacos, generos

JROCK = ("kessoku band", "the peggies", "Houkago Teatime", "BABYMETAL")


def main() -> None:
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--pais", nargs="+", default=["Hip Hop", "Rock", "Pop", "Electronic"])
    ap_.add_argument("--jrock", nargs="+", default=list(JROCK))
    a = ap_.parse_args()
    con = abrir(caminhos().banco)
    afin = generos.Afinidades(con)
    ap = afin.aprendido
    base = espacos.da_biblioteca(con)
    nome = {
        r[0]: (json.loads(r[1]) or [{"nome": ""}])[0]["nome"]
        for r in con.execute("SELECT video_id, artistas FROM faixas")
    }
    artista = aprendido.Aprendido._artistas(con)
    origem = {(r[0], r[1]): r[2] for r in con.execute("SELECT video_id, pai, origem FROM rotulos_genero")}
    X_base, tem_base = ap.atributos(base)
    print(f"{len(base)} faixas na biblioteca; modelos: {', '.join(sorted(ap.modelos))}")
    print("pai: AUC · revocação/falsos+ no 0,8 (por artista) | rap faixa a faixa | rock japonês ≥ 0,8")
    for pai in a.pais:
        rot = ap._rotulos.get(pai, {})
        vids = [v for v in sorted(rot) if v in ap._e.indice]
        y = np.array([rot[v] for v in vids], np.float64)
        faixa = np.array([origem.get((v, pai)) in ("gabarito_faixa", "cli", "mcp") for v in vids])
        grupos = sorted({artista.get(v, v) for v in vids})
        dobra = {g: i % aprendido.DOBRAS for i, g in enumerate(grupos)}
        X, _ = ap.atributos(vids)
        oof = np.zeros(len(vids))
        for k in range(aprendido.DOBRAS):
            te = np.array([dobra[artista.get(v, v)] == k for v in vids])
            if not te.any():
                continue
            beta = aprendido.ajustar(X[~te], y[~te])
            p = aprendido.prever(beta, X_base[tem_base])
            nucleo = p[p >= 0.5]
            ref = max(float(np.median(nucleo)) if len(nucleo) >= aprendido.MIN_NUCLEO else 0.5, 0.5)
            oof[te] = aprendido.prever(beta, X[te]) / ref
        reguas = {
            "v2": afin.relativa(pai, vids),
            "com MAEST": afin.relativa_com_maest(pai, vids),
            "aprendida": oof,
        }
        print(f"\n{pai} ({int(y.sum())} sim × {int(len(y) - y.sum())} não, {len(grupos)} artistas)")
        for nome_r, s in reguas.items():
            art = ~faixa
            linha = (f"  {nome_r:10} {aprendido._auc(s[art & (y == 1)], s[art & (y == 0)]) or float('nan'):.3f} · "
                     f"{np.mean(s[art & (y == 1)] >= 0.8):.2f}/{np.mean(s[art & (y == 0)] >= 0.8):.2f}")  # fmt: skip
            if faixa.any():
                fp, fn = s[faixa & (y == 1)], s[faixa & (y == 0)]
                linha += f" | {aprendido._auc(fp, fn) or float('nan'):.3f} · {np.mean(fp >= 0.8):.2f}/{np.mean(fn >= 0.8):.2f}"
            jr = np.array([nome.get(v) in a.jrock for v in vids]) & (y == 1)
            if jr.any():
                linha += f" | {np.mean(s[jr] >= 0.8):.2f} de {int(jr.sum())}"
            print(linha)
        if pai in ap.modelos:
            m = ap.modelos[pai]
            todas = afin.relativa_receita(pai, base)
            print(f"  na biblioteca: núcleo aprendido {m.nucleo} faixas; ≥ 0,8: {int((todas >= 0.8).sum())} "
                  f"(v2: {int((afin.relativa(pai, base) >= 0.8).sum())})")  # fmt: skip


if __name__ == "__main__":
    main()
