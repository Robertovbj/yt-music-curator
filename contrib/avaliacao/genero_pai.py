"""Avaliação do gênero-pai (#83, docs/avaliacao.md, ADR-14).

Compara, num gabarito pessoal (fora do repositório), o critério antigo (maior estilo do Discogs400), a fração do
Discogs e a afinidade do motor (fração Discogs + Jamendo), e mostra a régua relativa por gênero.

- AUC: chance de uma faixa do gênero ficar com nota maior que uma de fora (1,0 = separação perfeita), com
  intervalo de 95% por bootstrap no gabarito faixa a faixa.
- Régua: revocação e falsos positivos em cada nível (amplo 0,7, normal 0,8, estrito 1,0).

Ressalva: o gabarito por artista dá o mesmo rótulo a todas as faixas do artista. Qualquer sinal que use o artista
ou faixas vizinhas (que muitas vezes são do mesmo artista ou álbum) sai favorecido nele (Flexer, 2007).

Gabarito (TOML), por padrão em ~/.local/share/yt-music-curator/avaliacao/gabarito_generos.toml:

    [artistas]
    "Hip Hop" = ["Eminem", "NF"]       # todas as faixas do artista principal
    "Rock" = ["Sabaton"]
    [faixas."Hip Hop"]
    sim = ["<video_id>", ...]           # faixa a faixa: os casos difíceis
    nao = ["<video_id>", ...]

    CURATOR_DATA_DIR=<cópia do banco> uv run python contrib/avaliacao/genero_pai.py [--gabarito arquivo.toml]
"""

import argparse
import json
import tomllib
from pathlib import Path

import numpy as np

from curator.banco.conexao import abrir
from curator.config import caminhos
from curator.motor import espacos, generos


def auc(pos: np.ndarray, neg: np.ndarray) -> float:
    if not len(pos) or not len(neg):
        return float("nan")
    from scipy.stats import rankdata

    r = rankdata(np.concatenate([pos, neg]))
    return float((r[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def bootstrap(pos: np.ndarray, neg: np.ndarray, vezes: int = 2000, semente: int = 0) -> tuple[float, float]:
    rng = np.random.default_rng(semente)
    a = [auc(rng.choice(pos, len(pos)), rng.choice(neg, len(neg))) for _ in range(vezes)]
    return float(np.quantile(a, 0.025)), float(np.quantile(a, 0.975))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--gabarito", type=Path, default=caminhos().dados_dir / "avaliacao" / "gabarito_generos.toml"
    )
    gab = tomllib.loads(ap.parse_args().gabarito.read_text(encoding="utf-8"))
    con = abrir(caminhos().banco)
    afin = generos.Afinidades(con)
    ids = [v for v in espacos.da_biblioteca(con) if v in afin.d.indice]
    pos_id = {v: i for i, v in enumerate(ids)}
    principal = {
        r[0]: (json.loads(r[1]) or [{"nome": ""}])[0]["nome"]
        for r in con.execute("SELECT video_id, artistas FROM faixas")
    }
    D = afin.d.subconjunto(ids).X.astype(np.float64)
    F = afin.afinidade(ids)

    def sinais(pai: str) -> dict[str, np.ndarray]:
        cols = [i for i, c in enumerate(afin.d.colunas) if c.startswith(pai + "---")]
        k = afin.pais.index(pai)
        return {
            "maior estilo (antigo)": D[:, cols].max(axis=1),
            "fração Discogs": D[:, cols].sum(axis=1) / np.maximum(D.sum(axis=1), 1e-12),
            "afinidade (motor)": F[:, k],
            "relativa": F[:, k] / afin.reguas[pai].referencia,
        }

    rotulo = {}
    for pai, nomes in gab.get("artistas", {}).items():
        for v in ids:
            if principal.get(v) in nomes:
                rotulo.setdefault(v, set()).add(pai)
    rotulo = {v: next(iter(s)) for v, s in rotulo.items() if len(s) == 1}
    print(f"{len(ids)} faixas analisadas; gabarito por artista: {len(rotulo)} faixas")
    print("\nRégua de cada gênero:")
    for p, r in afin.reguas.items():
        print(f"  {p:24} {r.tipo:13} núcleo {r.nucleo:4d}  referência {r.referencia:.3f}")

    print("\nGabarito por artista (AUC; revocação/falsos+ na régua relativa):")
    for pai in gab.get("artistas", {}):
        pos = np.array([pos_id[v] for v, g in rotulo.items() if g == pai])
        neg = np.array([pos_id[v] for v, g in rotulo.items() if g != pai])
        s = sinais(pai)
        aucs = "  ".join(f"{n} {auc(x[pos], x[neg]):.3f}" for n, x in s.items() if n != "relativa")
        rel = s["relativa"]
        regua = "  ".join(
            f"≥{t}: {np.mean(rel[pos] >= t):.2f}/{np.mean(rel[neg] >= t):.2f}"
            for t in generos.NIVEIS.values()
        )
        print(f"  {pai:10} ({len(pos)} × {len(neg)}): {aucs}\n             {regua}")

    for pai, grupos in gab.get("faixas", {}).items():
        pos = np.array([pos_id[v] for v in grupos.get("sim", []) if v in pos_id])
        neg = np.array([pos_id[v] for v in grupos.get("nao", []) if v in pos_id])
        print(f"\nGabarito faixa a faixa, {pai} ({len(pos)} × {len(neg)}): AUC [IC 95%]")
        s = sinais(pai)
        for n, x in s.items():
            if n != "relativa":
                lo, hi = bootstrap(x[pos], x[neg])
                print(f"  {n:22} {auc(x[pos], x[neg]):.3f} [{lo:.3f}, {hi:.3f}]")
        rel = s["relativa"]
        print(
            "  régua: "
            + "  ".join(
                f"≥{t}: {np.mean(rel[pos] >= t):.2f}/{np.mean(rel[neg] >= t):.2f}"
                for t in generos.NIVEIS.values()
            )
        )


if __name__ == "__main__":
    main()
