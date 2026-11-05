"""Afinidade de gênero-pai (#83, ADR-14): um sinal e uma régua que valem igual para todos os gêneros.

Antes, o gênero-pai era o MAIOR estilo do Discogs400 ("Hip Hop" = máximo dos 26 estilos Hip Hop---*). O modelo é
multirrótulo (saída sigmoide) e espalha a probabilidade entre estilos irmãos: "Without Me" (Eminem) tinha 0,18 no
maior estilo e 1,31 somando os de Hip Hop. Com isso, o mesmo limite valia coisas diferentes em cada gênero.

Agora, por faixa e por pai:
1. **fração do Discogs400**: soma dos estilos do pai ÷ soma dos 400 estilos, ou seja, quanto da "atenção" do modelo
   cai naquele pai. Ao contrário do noisy-OR, não cresce com o número de estilos do pai (Electronic tem 106, Hip
   Hop 26).
2. **fração do MTG-Jamendo**: a mesma conta na outra cabeça, pelos rótulos equivalentes (`MAPA_JAMENDO`; décadas
   fora). As duas cabeças usam o mesmo embedding EffNet, então concordam muito; a média ainda ganha um pouco.
3. **afinidade** = média das frações disponíveis (só o Discogs, se o pai não tem equivalente no Jamendo).
4. **régua relativa**: afinidade ÷ mediana da afinidade no NÚCLEO do pai, que são as faixas da biblioteca que
   todas as cabeças põem nesse pai em 1º lugar. 1,0 = tão típica quanto a faixa mediana do núcleo. O mesmo
   limite (0,8) separou Hip Hop e Rock na avaliação (docs/avaliacao.md). Núcleo com menos de `MIN_NUCLEO` faixas:
   núcleo amplo (1º em qualquer cabeça); sem isso, a referência global (mediana das medianas dos núcleos).

Testados e deixados de fora: suavizar pelos vizinhos no embedding (ganho dentro do ruído, e parte dele vem de
vizinhos do mesmo artista) e puxar para a média do artista (o gabarito é por artista, e compositores de trilha
sonora tocam vários gêneros).

Pop e Electronic não se separam bem só pelo áudio: esses modelos chamam a produção pop moderna de Electronic.
"""

import sqlite3
from dataclasses import dataclass

import numpy as np

from curator.motor import espacos

# gênero-pai do Discogs400 → rótulos equivalentes do MTG-Jamendo (só os inequívocos)
MAPA_JAMENDO = {
    "Blues": ["blues", "bluesrock"],
    "Classical": ["classical", "orchestral", "symphonic", "choir"],
    "Electronic": ["electronic", "electronica", "edm", "techno", "house", "deephouse", "trance", "drumnbass",
                   "dubstep", "breakbeat", "idm", "minimal", "club", "eurodance", "electropop", "synthpop"],
    "Folk, World, & Country": ["folk", "country", "world", "ethno", "celtic", "worldfusion"],
    "Funk / Soul": ["funk", "soul", "rnb", "disco"],
    "Hip Hop": ["hiphop", "rap"],
    "Jazz": ["jazz", "acidjazz", "jazzfusion", "swing"],
    "Latin": ["latin", "bossanova"],
    "Pop": ["pop", "poprock", "electropop", "synthpop", "popfolk", "dance"],
    "Reggae": ["reggae", "dub"],
    "Rock": ["rock", "metal", "hardrock", "punkrock", "alternativerock", "classicrock", "grunge", "poprock",
             "progressive", "psychedelic", "rocknroll", "postrock"],
    "Stage & Screen": ["soundtrack"],
}  # fmt: skip
DECADAS = {"60s", "70s", "80s", "90s"}  # rótulos de época do Jamendo: não são gênero
MIN_NUCLEO = 10
NIVEIS = {"amplo": 0.7, "normal": 0.8, "estrito": 1.0}


@dataclass(frozen=True)
class Regua:
    pai: str
    referencia: float  # a afinidade que vale 1,0
    tipo: str  # "nucleo" | "nucleo_amplo" | "global"
    nucleo: int  # faixas do núcleo usado (0 na global)


class Afinidades:
    """Afinidade e régua de todos os gêneros-pai, medidas sobre a biblioteca analisada deste banco."""

    def __init__(self, con: sqlite3.Connection):
        self.d = espacos.probabilidades(con, "discogs400")
        self.j = espacos.probabilidades(con, "jamendo_genre")
        self.pais = sorted({c.split("---")[0] for c in self.d.colunas if "---" in c})
        # matrizes de pertencimento: estilo → pai (Discogs) e rótulo → pai (Jamendo)
        self._md = np.zeros((len(self.d.colunas), len(self.pais)), np.float64)
        for i, c in enumerate(self.d.colunas):
            if "---" in c:
                self._md[i, self.pais.index(c.split("---")[0])] = 1
        col_j = {c: i for i, c in enumerate(self.j.colunas)}
        self._mj = np.zeros((len(self.j.colunas), len(self.pais)), np.float64)
        for p, rotulos in MAPA_JAMENDO.items():
            if p in self.pais:
                for r in rotulos:
                    if r in col_j:
                        self._mj[col_j[r], self.pais.index(p)] = 1
        self._com_jamendo = self._mj.any(axis=0)
        self._genero_j = np.array([c not in DECADAS for c in self.j.colunas], bool)
        base = [v for v in espacos.da_biblioteca(con) if v in self.d.indice]
        fd, fj, tem_j = self._fracoes(base)
        afin = self._fundir(fd, fj, tem_j)
        self.reguas = self._medir_reguas(afin, fd, fj, tem_j)

    # ------------------------------------------------------------------ cálculo

    def _fracoes(self, ids: list[str]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(fração Discogs, fração Jamendo, tem Jamendo), linhas alinhadas a `ids`; faixa sem Discogs = 0."""
        n, p = len(ids), len(self.pais)
        fd, fj, tem_j = np.zeros((n, p)), np.zeros((n, p)), np.zeros(n, bool)
        li = [k for k, v in enumerate(ids) if v in self.d.indice]
        if li:
            X = self.d.X[[self.d.indice[ids[k]] for k in li]].astype(np.float64)
            fd[li] = (X @ self._md) / np.maximum(X.sum(axis=1, keepdims=True), 1e-12)
        lj = [k for k, v in enumerate(ids) if v in self.j.indice]
        if lj and self._mj.any():
            Y = self.j.X[[self.j.indice[ids[k]] for k in lj]].astype(np.float64)
            total = Y[:, self._genero_j].sum(axis=1, keepdims=True)
            fj[lj] = (Y @ self._mj) / np.maximum(total, 1e-12)
            tem_j[lj] = True
        return fd, fj, tem_j

    def _fundir(self, fd: np.ndarray, fj: np.ndarray, tem_j: np.ndarray) -> np.ndarray:
        usa_j = tem_j[:, None] & self._com_jamendo[None, :]
        return np.where(usa_j, (fd + fj) / 2, fd)

    def _medir_reguas(self, afin, fd, fj, tem_j) -> dict[str, Regua]:
        if not len(afin):
            return {p: Regua(p, 1.0, "global", 0) for p in self.pais}
        primeiro_d = fd.argmax(axis=1)
        # no Jamendo, só os pais que ele tem disputam o 1º lugar
        fj_mascarado = np.where(self._com_jamendo[None, :], fj, -1.0)
        primeiro_j = fj_mascarado.argmax(axis=1)
        medidas: dict[str, tuple[float, str, int]] = {}
        for k, p in enumerate(self.pais):
            em_d = primeiro_d == k
            em_j = (primeiro_j == k) & tem_j if self._com_jamendo[k] else np.zeros(len(afin), bool)
            todas = em_d & (em_j | ~tem_j) if self._com_jamendo[k] else em_d
            qualquer = em_d | em_j
            for nome, mascara in (("nucleo", todas), ("nucleo_amplo", qualquer)):
                if mascara.sum() >= MIN_NUCLEO:
                    medidas[p] = (float(np.median(afin[mascara, k])), nome, int(mascara.sum()))
                    break
        nucleos = [m for m, tipo, _ in medidas.values() if tipo == "nucleo"]
        # sem núcleo nenhum (biblioteca pequena demais): a afinidade típica do gênero dominante de cada faixa
        global_ = float(np.median(nucleos)) if nucleos else float(np.median(afin.max(axis=1))) or 1.0
        return {
            p: Regua(p, *medidas[p]) if p in medidas else Regua(p, global_, "global", 0) for p in self.pais
        }

    # ------------------------------------------------------------------ consulta

    def afinidade(self, ids: list[str]) -> np.ndarray:
        """Matriz (faixas × pais) da afinidade bruta, na ordem de `self.pais`."""
        return self._fundir(*self._fracoes(ids))

    def relativa(self, pai: str, ids: list[str]) -> np.ndarray:
        """Afinidade de `pai` na régua do gênero (1,0 = típica do núcleo). Pai que não existe: zeros."""
        if pai not in self.pais:
            return np.zeros(len(ids), np.float64)
        k = self.pais.index(pai)
        return self.afinidade(ids)[:, k] / self.reguas[pai].referencia
