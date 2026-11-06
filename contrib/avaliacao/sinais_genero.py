"""Avaliação dos sinais novos de gênero-pai (#88; ver docs/avaliacao.md e ADR-14).

Compara, no gabarito pessoal da #83, a afinidade atual do motor (fração Discogs400 + Jamendo) com os sinais novos:
- MAEST: fração dos estilos do pai (a taxonomia é a do Discogs, "Pai---Estilo"), na média e no maior trecho de 30 s.
- YAMNet: fração das classes de gênero do AudioSet que correspondem ao pai (`generos.MAPA_YAMNET`); no Hip Hop,
  também "Rapping" (média e percentil 90) e "Rapping" menos "Singing".
- Tags do MSD-MusiCNN: fração das tags de gênero que correspondem ao pai (`generos.MAPA_MSD`).
- Metadados externos: fração dos votos de gênero (MusicBrainz na faixa, no release group e no artista; Deezer no
  álbum) que correspondem ao pai (`generos.pais_de_rotulo`).
- Fusões definidas antes de olhar o resultado: média das réguas relativas de cada sinal (sinal ÷ mediana dele no
  núcleo do pai), sem nenhum peso ajustado no gabarito.

AUC com IC 95% por bootstrap no gabarito faixa a faixa (Hip Hop) e AUC no gabarito por artista. Cada sinal é medido
só nas faixas que o têm; a cobertura aparece ao lado.

Ressalva (Flexer, 2007): o gabarito por artista dá o mesmo rótulo a todas as faixas do artista, então os gêneros do
ARTISTA nos metadados saem favorecidos nele por construção. O teste justo para eles é o faixa a faixa.

    CURATOR_DATA_DIR=<cópia do banco> uv run python contrib/avaliacao/sinais_genero.py [--gabarito arquivo.toml]
"""

import argparse
import json
import tomllib
from pathlib import Path

import numpy as np

from curator.banco.conexao import abrir
from curator.config import caminhos
from curator.motor import espacos, generos

AUC_IC = 2000


def auc(pos: np.ndarray, neg: np.ndarray) -> float:
    from scipy.stats import rankdata

    if not len(pos) or not len(neg):
        return float("nan")
    r = rankdata(np.concatenate([pos, neg]))
    return float((r[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def ic(pos: np.ndarray, neg: np.ndarray, semente: int = 0) -> tuple[float, float]:
    rng = np.random.default_rng(semente)
    a = [auc(rng.choice(pos, len(pos)), rng.choice(neg, len(neg))) for _ in range(AUC_IC)]
    return float(np.quantile(a, 0.025)), float(np.quantile(a, 0.975))


def ic_diferenca(pa, na, pb, nb, semente: int = 0) -> tuple[float, float]:
    """IC 95% de AUC(b) − AUC(a), reamostrando as MESMAS faixas nos dois sinais (pareado)."""
    rng = np.random.default_rng(semente)
    d = []
    for _ in range(AUC_IC):
        ip, ineg = rng.integers(0, len(pa), len(pa)), rng.integers(0, len(na), len(na))
        d.append(auc(pb[ip], nb[ineg]) - auc(pa[ip], na[ineg]))
    return float(np.quantile(d, 0.025)), float(np.quantile(d, 0.975))


def fracao(M: espacos.Matriz, ids: list[str], colunas_por_pai: dict[str, list[str]], universo: list[str]):
    """{pai: vetor} com a fração da massa do universo de colunas que cai no pai; NaN onde a faixa não tem o vetor."""
    col = {c: i for i, c in enumerate(M.colunas)}
    uni = [col[c] for c in universo if c in col]
    out = {}
    linhas = [M.indice.get(v) for v in ids]
    tem = np.array([i is not None for i in linhas])
    X = np.zeros((len(ids), len(M.colunas)))
    if tem.any():
        X[tem] = M.X[[i for i in linhas if i is not None]]
    total = X[:, uni].sum(axis=1)
    for pai, cs in colunas_por_pai.items():
        idx = [col[c] for c in cs if c in col]
        v = X[:, idx].sum(axis=1) / np.maximum(total, 1e-12) if idx else np.zeros(len(ids))
        out[pai] = np.where(tem, v, np.nan)
    return out


def externos_por_pai(con, ids: list[str], pais: list[str]) -> dict[str, np.ndarray]:
    """Média, por faixa, das frações por nível (MB faixa, MB álbum, MB artista, Deezer álbum) que existem."""
    pos = {v: i for i, v in enumerate(ids)}
    somas: dict[tuple[str, str, str], dict[str, float]] = {}
    for vid, fonte, nivel, rotulo, peso in con.execute(
        "SELECT video_id, fonte, nivel, rotulo, peso FROM externos_generos"
    ):
        if vid not in pos:
            continue
        for p in generos.pais_de_rotulo(rotulo):
            d = somas.setdefault((vid, fonte, nivel), {})
            d[p] = d.get(p, 0.0) + peso
    out = {p: np.full(len(ids), np.nan) for p in pais}
    por_faixa: dict[str, list[dict[str, float]]] = {}
    for (vid, _f, _n), d in somas.items():
        t = sum(d.values())
        por_faixa.setdefault(vid, []).append({p: x / t for p, x in d.items()})
    for vid, niveis in por_faixa.items():
        for p in pais:
            out[p][pos[vid]] = float(np.mean([n.get(p, 0.0) for n in niveis]))
    return out


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
    pais = afin.pais
    F = afin.afinidade(ids)
    sinais: dict[str, dict[str, np.ndarray]] = {
        "motor (Discogs+Jamendo)": {p: F[:, k] for k, p in enumerate(pais)}
    }

    for tipo, nome in (("maest519", "MAEST média"), ("maest519_max", "MAEST maior trecho")):
        M = espacos.probabilidades(con, tipo)
        if len(M.ids):
            por_pai = {p: [c for c in M.colunas if c.startswith(p + "---")] for p in pais}
            sinais[nome] = fracao(M, ids, por_pai, M.colunas)
    for tipo, nome in (("yamnet", "YAMNet média"), ("yamnet_p90", "YAMNet p90")):
        M = espacos.probabilidades(con, tipo)
        if len(M.ids):
            universo = sorted({c for cs in generos.MAPA_YAMNET.values() for c in cs})
            sinais[nome] = fracao(M, ids, generos.MAPA_YAMNET, universo)
    M = espacos.probabilidades(con, "msd_tags")
    if len(M.ids):
        sinais["MSD tags"] = fracao(
            M, ids, generos.MAPA_MSD, sorted({c for cs in generos.MAPA_MSD.values() for c in cs})
        )
    ext = externos_por_pai(con, ids, pais)
    if any(np.isfinite(v).any() for v in ext.values()):
        sinais["externos (MB + Deezer)"] = ext

    # Rapping: só para o Hip Hop
    for tipo, nome in (("yamnet", "YAMNet Rapping média"), ("yamnet_p90", "YAMNet Rapping p90")):
        M = espacos.probabilidades(con, tipo)
        if len(M.ids):
            raw = np.array(
                [M.X[M.indice[v], M.colunas.index("Rapping")] if v in M.indice else np.nan for v in ids]
            )
            sing = np.array(
                [M.X[M.indice[v], M.colunas.index("Singing")] if v in M.indice else np.nan for v in ids]
            )
            sinais[nome] = {"Hip Hop": raw}
            sinais[nome.replace("Rapping", "Rapping−Singing")] = {"Hip Hop": raw - sing}

    # réguas relativas e fusões (núcleo = faixas cujo 1º pai no motor é o pai)
    primeiro = F.argmax(axis=1)

    def relativa(s: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        out = {}
        for k, p in enumerate(pais):
            if p not in s:
                continue
            nuc = s[p][(primeiro == k) & np.isfinite(s[p])]
            ref = float(np.median(nuc)) if len(nuc) >= generos.MIN_NUCLEO else float(np.nanmedian(s[p]))
            out[p] = s[p] / max(ref, 1e-6)
        return out

    def fundir(nomes: list[str]) -> dict[str, np.ndarray]:
        rels = [relativa(sinais[n]) for n in nomes if n in sinais]
        out = {}
        for p in pais:
            vs = [r[p] for r in rels if p in r]
            if vs:
                out[p] = np.nanmean(np.vstack(vs), axis=0)  # sem o sinal (NaN), a média é dos que existem
        return out

    audio_novos = ["MAEST média", "YAMNet média", "MSD tags"]
    if all(n in sinais for n in audio_novos):
        sinais["fusão: motor + MAEST"] = fundir(["motor (Discogs+Jamendo)", "MAEST média"])
        sinais["fusão: áudio (motor + MAEST + YAMNet + MSD)"] = fundir(
            ["motor (Discogs+Jamendo)", *audio_novos]
        )
        if "externos (MB + Deezer)" in sinais:
            sinais["fusão: áudio + externos"] = fundir(
                ["motor (Discogs+Jamendo)", *audio_novos, "externos (MB + Deezer)"]
            )

    principal = {
        r[0]: (json.loads(r[1]) or [{"nome": ""}])[0]["nome"]
        for r in con.execute("SELECT video_id, artistas FROM faixas")
    }
    base = "motor (Discogs+Jamendo)"
    for pai, grupos in gab.get("faixas", {}).items():
        sim = [pos_id[v] for v in grupos.get("sim", []) if v in pos_id]
        nao = [pos_id[v] for v in grupos.get("nao", []) if v in pos_id]
        print(f"\nGabarito faixa a faixa, {pai} ({len(sim)} sim × {len(nao)} não): AUC [IC 95%] · cobertura")
        for nome, s in sinais.items():
            if pai not in s:
                continue
            x = s[pai]
            ps, ns = (
                np.array([i for i in sim if np.isfinite(x[i])]),
                np.array([i for i in nao if np.isfinite(x[i])]),
            )
            if len(ps) < 5 or len(ns) < 5:
                print(f"  {nome:44} poucas faixas ({len(ps)} × {len(ns)})")
                continue
            lo, hi = ic(x[ps], x[ns])
            linha = f"  {nome:44} {auc(x[ps], x[ns]):.3f} [{lo:.3f}, {hi:.3f}] · {len(ps)}+{len(ns)}"
            if nome != base:
                b = sinais[base][pai]
                dlo, dhi = ic_diferenca(b[ps], b[ns], x[ps], x[ns])
                linha += f" · Δ vs motor [{dlo:+.3f}, {dhi:+.3f}] (mesmas faixas: {auc(b[ps], b[ns]):.3f})"
            print(linha)

    rotulo = {}
    for pai, nomes in gab.get("artistas", {}).items():
        for v in ids:
            if principal.get(v) in nomes:
                rotulo.setdefault(v, set()).add(pai)
    rotulo = {v: next(iter(s)) for v, s in rotulo.items() if len(s) == 1}
    print(
        f"\nGabarito por artista ({len(rotulo)} faixas): AUC de cada sinal por pai (cobertura entre parênteses)"
    )
    nomes_sinais = [n for n in sinais if "Rapping" not in n]
    print("  " + " " * 12 + " | ".join(f"{n[:18]:>18}" for n in nomes_sinais))
    for pai in gab.get("artistas", {}):
        cel = []
        for nome in nomes_sinais:
            x = sinais[nome].get(pai)
            if x is None:
                cel.append(f"{'-':>18}")
                continue
            ps = np.array([pos_id[v] for v, g in rotulo.items() if g == pai and np.isfinite(x[pos_id[v]])])
            ns = np.array([pos_id[v] for v, g in rotulo.items() if g != pai and np.isfinite(x[pos_id[v]])])
            cel.append(f"{auc(x[ps], x[ns]) if len(ps) and len(ns) else float('nan'):>11.3f} ({len(ps):>3})")
        print(f"  {pai[:12]:12}" + " | ".join(cel))


if __name__ == "__main__":
    main()
