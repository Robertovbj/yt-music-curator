"""O que cada eixo do mapa acompanha (#57).

Os eixos do UMAP não têm significado próprio: ele preserva vizinhanças, e a mesma projeção pode sair girada ou
espelhada. Depois de projetar, então:

1. `orientar`: centra a nuvem, gira para os eixos principais dela (PCA dos próprios pontos) e fixa o sinal de
   cada eixo para que ele cresça com a energia. Mesmos pontos → mesma orientação.
2. `correlacoes`: Spearman de cada eixo com as features escalares e com a probabilidade de cada gênero-pai.
   O rótulo do eixo cita as 2–3 mais fortes; abaixo de |ρ| = LIMIAR, "sem direção clara".

A vista PCA do espaço interpretável (`pca_interpretavel`) é diferente: ali cada eixo é uma combinação exata das
features, com os pesos e a variância explicada.
"""

import sqlite3
from dataclasses import asdict, dataclass

import numpy as np

from curator.motor import espacos

LIMIAR = 0.3
REFERENCIA = "energy"  # o sinal de cada eixo do UMAP é o que faz ele crescer com a energia
MAX_NO_ROTULO = 3
MAX_PESOS = 8

NOMES = {
    "danceability": "dançabilidade", "acousticness": "acústico", "aggressive": "agressivo",
    "electronic": "eletrônico", "happy": "feliz", "sad": "triste", "relaxed": "relaxado", "party": "festa",
    "instrumental": "instrumental", "voz_feminina": "voz feminina", "engagement": "envolvimento",
    "approachability": "acessibilidade", "valence": "valência", "arousal": "excitação", "energy": "energia",
    "bpm": "BPM",
}  # fmt: skip
GRUPOS = {"discogs400": "gênero", "jamendo_genre": "gênero (Jamendo)", "jamendo_mood": "humor",
          "jamendo_instr": "instrumento"}  # fmt: skip


@dataclass
class Eixo:
    rotulo: str  # "eixo 1: ↑ energia, ↓ acústico (ρ 0,62)"
    correlacoes: list[tuple[str, float]]  # (nome legível, ρ), da mais forte para a mais fraca
    variancia: float | None = None  # só na PCA: fração da variância total
    pesos: list[tuple[str, float]] | None = None  # só na PCA: maiores pesos (com sinal)

    def como_dict(self) -> dict:
        return asdict(self)


def _num(x: float) -> str:
    return f"{x:.2f}".replace(".", ",").replace("-", "−")


def _postos(x: np.ndarray) -> np.ndarray:
    from scipy.stats import rankdata

    return rankdata(x).astype(np.float64)


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    """ρ de Spearman (empates pela média dos postos); 0 se uma das séries for constante ou curta demais."""
    ok = ~(np.isnan(a) | np.isnan(b))
    if ok.sum() < 3:
        return 0.0
    ra, rb = _postos(a[ok]), _postos(b[ok])
    ra, rb = ra - ra.mean(), rb - rb.mean()
    den = np.sqrt((ra**2).sum() * (rb**2).sum())
    return float((ra * rb).sum() / den) if den else 0.0


def referencias(con: sqlite3.Connection, ids: list[str]) -> dict[str, np.ndarray]:
    """Séries com que os eixos são comparados, alinhadas a `ids`: features escalares e gêneros-pai."""
    out = {}
    esc = espacos.escalares(con)
    linhas = [esc.indice.get(v) for v in ids]
    for j, c in enumerate(esc.colunas):
        out[NOMES.get(c, c)] = np.array(
            [esc.X[i, j] if i is not None else np.nan for i in linhas], np.float64
        )
    d400 = espacos.probabilidades(con, "discogs400")
    if d400.colunas:
        pais = sorted({c.split("---")[0] for c in d400.colunas})
        soma = {p: [j for j, c in enumerate(d400.colunas) if c.split("---")[0] == p] for p in pais}
        for p in pais:
            col = d400.X[:, soma[p]].sum(axis=1)
            out[f"gênero {p}"] = np.array(
                [col[d400.indice[v]] if v in d400.indice else np.nan for v in ids], np.float64
            )
    return out


def orientar(Y: np.ndarray, energia: np.ndarray) -> np.ndarray:
    """Centra, gira para os eixos principais da própria nuvem e põe cada eixo crescendo com a energia."""
    Y = np.asarray(Y, np.float64)
    if len(Y) < 2:
        return Y.astype(np.float32)
    C = Y - Y.mean(axis=0)
    _, _, Vt = np.linalg.svd(C, full_matrices=False)
    R = C @ Vt.T
    for k in range(R.shape[1]):
        rho = spearman(R[:, k], energia)
        sinal = np.sign(rho) if abs(rho) > 1e-9 else np.sign(np.sum(R[:, k] ** 3)) or 1.0
        R[:, k] *= sinal
    return R.astype(np.float32)


def _rotulo(k: int, corr: list[tuple[str, float]], prefixo: str = "eixo") -> str:
    fortes = [(n, r) for n, r in corr if abs(r) >= LIMIAR][:MAX_NO_ROTULO]
    if not fortes:
        return f"{prefixo} {k + 1}: sem direção clara"
    partes = ", ".join(f"{'↑' if r > 0 else '↓'} {n}" for n, r in fortes)
    return f"{prefixo} {k + 1}: {partes} (ρ {_num(abs(fortes[0][1]))})"


def correlacoes(Y: np.ndarray, refs: dict[str, np.ndarray]) -> list[Eixo]:
    """Um `Eixo` por coluna de Y, com todas as correlações em ordem de |ρ| (empate pelo nome)."""
    out = []
    for k in range(Y.shape[1]):
        corr = [(n, round(spearman(Y[:, k].astype(np.float64), s), 3)) for n, s in refs.items()]
        corr.sort(key=lambda x: (-abs(x[1]), x[0]))
        out.append(Eixo(_rotulo(k, corr), corr))
    return out


def _nome_coluna(c: str) -> str:
    grupo, _, rotulo = c.partition(":")
    if grupo == "escalares":
        return NOMES.get(rotulo, rotulo)
    return f"{GRUPOS.get(grupo, grupo)} {rotulo.replace('---', ' · ')}"


def pca_interpretavel(
    con: sqlite3.Connection, ids: list[str], dims: int
) -> tuple[espacos.Matriz, list[Eixo]]:
    """PCA (SVD exato) do espaço interpretável das faixas `ids`. O sinal de cada componente põe o maior peso
    positivo, então mesmos dados → mesmos eixos. Cada eixo traz os maiores pesos e a variância explicada."""
    m = espacos.interpretavel(con, ids=ids)
    if len(m.ids) < dims + 1 or not m.X.shape[1]:
        return espacos.Matriz([], np.zeros((0, dims), np.float32)), []
    X = m.X.astype(np.float64)
    C = X - X.mean(axis=0)
    _, S, Vt = np.linalg.svd(C, full_matrices=False)
    total = float((S**2).sum()) or 1.0
    refs = referencias(con, m.ids)
    Y = np.zeros((len(m.ids), dims))
    eixos = []
    for k in range(dims):
        v = Vt[k]
        j = int(np.argmax(np.abs(v)))
        if v[j] < 0:
            v = -v
        Y[:, k] = C @ v
        ordem = sorted(range(len(v)), key=lambda i: (-abs(v[i]), m.colunas[i]))[:MAX_PESOS]
        pesos = [(_nome_coluna(m.colunas[i]), round(float(v[i]), 3)) for i in ordem]
        var = round(float(S[k] ** 2) / total, 4)
        fortes = ", ".join(f"{'↑' if p > 0 else '↓'} {n}" for n, p in pesos[:MAX_NO_ROTULO])
        corr = [(n, round(spearman(Y[:, k], s), 3)) for n, s in refs.items()]
        corr.sort(key=lambda x: (-abs(x[1]), x[0]))
        eixos.append(Eixo(f"PC{k + 1} ({var * 100:.0f}% da variância): {fortes}", corr, var, pesos))
    return espacos.Matriz(m.ids, Y.astype(np.float32)), eixos
