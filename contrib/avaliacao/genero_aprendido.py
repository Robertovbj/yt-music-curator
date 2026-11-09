"""Avaliação do gênero-pai aprendido (#93, #101, ADR-17; docs/avaliacao.md): a tabela da decisão por gênero.

No gabarito pessoal da #83, compara as réguas:
- `v2` (ADR-14) e `com MAEST` (ADR-15);
- `aprendida`: só o modelo, sem portão nem guarda;
- `com guarda`: o que a receita veria se o gênero adotasse a régua aprendida (portão + guarda; fora deles, a v2).

As aprendidas são medidas deixando o artista INTEIRO de fora (10 dobras por artista): o modelo e a guarda da dobra
nunca viram uma faixa do artista que estão prevendo. Por pai: a AUC, a revocação e os falsos positivos no 0,8 por
artista (gabarito por artista), faixa a faixa (gabarito faixa a faixa, o rap), nos rótulos do uso (CLI e MCP: poucos e
os mais difíceis, porque o aprendizado ativo pergunta onde as réguas discordam) e o rock japonês (`--jrock`).

Na biblioteca (faixas sem rótulo): quantas passam de 0,8 em cada régua, nas trilhas de jogo (artistas em
`--trilhas`) e no resto; que fração das trilhas e das faixas japonesas o modelo julga; e exemplos de faixas que
mudariam de lado no 0,8 se o gênero adotasse a régua aprendida.

Usa os rótulos do banco (`curator generos importar-gabarito` antes). Rode numa CÓPIA do banco:

    CURATOR_DATA_DIR=<cópia> uv run python contrib/avaliacao/genero_aprendido.py
"""

import argparse
import json
import re

import numpy as np

from curator.banco.conexao import abrir
from curator.config import caminhos
from curator.motor import aprendido, espacos, generos

JROCK = ("kessoku band", "the peggies", "Houkago Teatime", "BABYMETAL")
TRILHAS = ("HOYO-MiX", "Lorien Testard", "Yasunori Nishiki", "Masayoshi Soken", "Yu-Peng Chen", "MONACA",
           "Keiichi Okabe", "Keigo Hoashi", "Nobuo Uematsu", "Binary Haze Interactive", "Mili", "Kumi Tanioka",
           "Kuniyuki Takahashi")  # fmt: skip
JAPONES = re.compile(r"[぀-ヿ一-鿿]")


def _fmt(auc, pos, neg) -> str:
    return f"{auc if auc is not None else float('nan'):.3f} · {np.mean(pos >= 0.8):.2f}/{np.mean(neg >= 0.8):.2f}"


def main() -> None:
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--pais", nargs="+", default=["Hip Hop", "Rock", "Pop", "Electronic"])
    ap_.add_argument("--jrock", nargs="+", default=list(JROCK))
    ap_.add_argument("--trilhas", nargs="+", default=list(TRILHAS), help="Artistas de trilha de jogo.")
    ap_.add_argument("--exemplos", type=int, default=6, help="Faixas que mudariam de lado, por sentido.")
    a = ap_.parse_args()
    con = abrir(caminhos().banco)
    afin = generos.Afinidades(con)
    ap = afin.aprendido
    base = espacos.da_biblioteca(con)
    meta = {r[0]: (r[1], (json.loads(r[2]) or [{"nome": ""}])[0]["nome"])
            for r in con.execute("SELECT video_id, titulo, artistas FROM faixas")}  # fmt: skip
    origem = {(r[0], r[1]): r[2] for r in con.execute("SELECT video_id, pai, origem FROM rotulos_genero")}
    rotuladas = set(ap._rotuladas)
    livres = [v for v in base if v not in rotuladas and v in afin.d.indice]
    trilha = np.array([meta[v][1] in a.trilhas for v in livres])
    japonesa = np.array([bool(JAPONES.search(meta[v][0] + meta[v][1])) for v in livres]) & ~trilha
    julga = ~afin.nao_julgadas(livres)
    print(
        f"{len(base)} faixas na biblioteca, {len(livres)} sem rótulo; modelos: {', '.join(sorted(ap.modelos))}"
    )
    print(f"guarda: {aprendido.K_GUARDA} vizinhas, corte {ap.corte_guarda:.3f} (percentil "
          f"{aprendido.PERCENTIL_GUARDA:g} das rotuladas)")  # fmt: skip
    print(f"o modelo julga {julga.mean():.0%} das sem rótulo: trilhas {julga[trilha].mean():.0%} de {trilha.sum()}, "
          f"japonesas {julga[japonesa].mean():.0%} de {japonesa.sum()}, "
          f"outras {julga[~trilha & ~japonesa].mean():.0%} de {(~trilha & ~japonesa).sum()}")  # fmt: skip
    print(
        "\npai: AUC · revocação/falsos+ no 0,8 — por artista | faixa a faixa | rótulos do uso | rock japonês ≥ 0,8"
    )
    for pai in a.pais:
        r = aprendido.fora_da_amostra(con, pai)
        if r is None:
            print(f"\n{pai}: sem modelo (régua do ADR-14)")
            continue
        vids, y = r["vids"], r["y"]
        faixa = np.array([origem.get((v, pai)) == "gabarito_faixa" for v in vids])
        uso = np.array([origem.get((v, pai)) in ("cli", "mcp") for v in vids])
        reguas = {"v2": r["v2"], "com MAEST": afin.relativa_com_maest(pai, vids), "aprendida": r["aprendida"],
                  "com guarda": r["com_guarda"]}  # fmt: skip
        print(f"\n{pai} ({int(y.sum())} sim × {int(len(y) - y.sum())} não, {r['artistas']} artistas, "
              f"{int(faixa.sum())} faixa a faixa, {int(uso.sum())} do uso; "
              f"a guarda julga {r['dentro'].mean():.0%} das rotuladas)")  # fmt: skip
        for nome_r, s in reguas.items():
            art = ~faixa & ~uso
            linha = f"  {nome_r:10} " + _fmt(aprendido._auc(s[art & (y == 1)], s[art & (y == 0)]),
                                             s[art & (y == 1)], s[art & (y == 0)])  # fmt: skip
            for m in (faixa, uso):
                fp, fn = s[m & (y == 1)], s[m & (y == 0)]
                linha += (" | " + _fmt(aprendido._auc(fp, fn), fp, fn)) if len(fp) and len(fn) else " | —"
            jr = np.array([meta[v][1] in a.jrock for v in vids]) & (y == 1)
            if jr.any():
                linha += f" | {np.mean(s[jr] >= 0.8):.2f} de {int(jr.sum())}"
            print(linha)
        v2 = afin.relativa(pai, livres)
        pura = ap.relativa(pai, livres)
        guarda = afin.relativa_aprendida(pai, livres)
        print("  sem rótulo ≥ 0,8 (trilhas · japonesas · outras):")
        for nome_r, s in (("v2", v2), ("aprendida", np.nan_to_num(pura)), ("com guarda", guarda)):
            print(f"    {nome_r:10} {int((s >= 0.8).sum()):5}  ({int((s[trilha] >= 0.8).sum())} · "
                  f"{int((s[japonesa] >= 0.8).sum())} · {int((s[~trilha & ~japonesa] >= 0.8).sum())})")  # fmt: skip
        for titulo, mask in (
            ("entrariam", (guarda >= 0.8) & (v2 < 0.8)),
            ("sairiam", (guarda < 0.8) & (v2 >= 0.8)),
        ):
            idx = sorted(np.flatnonzero(mask), key=lambda i: (-abs(guarda[i] - v2[i]), livres[i]))
            print(f"  {titulo} no 0,8 com a régua aprendida: {int(mask.sum())}")
            for i in idx[: a.exemplos]:
                t, art = meta[livres[i]]
                print(f"    {art} — {t}  (v2 {v2[i]:.2f} → {guarda[i]:.2f})")


if __name__ == "__main__":
    main()
