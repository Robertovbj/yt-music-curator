"""Executa uma receita (ADR-05). Nenhuma etapa usa aleatoriedade: mesma receita + mesmo banco = mesma lista.

1. universo ∩ faixas analisadas
2. filtros duros (exclusões exatas por nome normalizado, intervalos, mínimos/máximos, gêneros)
3. score = média ponderada de: proximidade ao perfil-alvo (z-score da biblioteca), similaridade ao centroide
   das sementes (Rocchio) e afinidade com os gêneros preferidos
4. ordenação total por (-score, video_id)
5. MMR (Carbonell & Goldstein, 1998) com λ fixo + limite por artista
6. corte no tamanho

`VERSAO_MOTOR` muda quando o significado de um campo da receita muda; a execução guarda a versão (ADR-05).
v2 (#83): `genero_pai_min` passou a ser a afinidade relativa ao núcleo do gênero (ADR-14), não o maior estilo.
"""

import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import numpy as np

from curator.banco import consultas
from curator.motor import espacos, generos
from curator.motor.receita import Receita, resolver_playlist, validar
from curator.texto import normalizar

VERSAO_MOTOR = "2"


@dataclass
class Item:
    video_id: str
    score: float
    componentes: dict[str, float]


@dataclass
class Execucao:
    receita_hash: str
    versao_banco: str
    itens: list[Item]
    universo: int
    sem_analise: int
    apos_filtros: int
    descartes: Counter = field(default_factory=Counter)
    versao_motor: str = VERSAO_MOTOR

    @property
    def ids(self) -> list[str]:
        return [i.video_id for i in self.itens]


# ------------------------------------------------------------------ universo e metadados


def _universo(con: sqlite3.Connection, r: Receita) -> list[str]:
    if r.universo == "biblioteca":
        return consultas.biblioteca(con)
    if r.universo == "curtidas":
        return [x[0] for x in con.execute("SELECT video_id FROM faixas WHERE curtida = 1 ORDER BY video_id")]
    if r.universo == "nao_alocadas":
        return sorted(consultas.nao_alocadas(con))
    pid = resolver_playlist(con, r.universo)
    return [
        x[0]
        for x in con.execute(
            "SELECT DISTINCT video_id FROM playlist_faixas WHERE playlist_id = ? ORDER BY video_id", (pid,)
        )
    ]


def _metadados(con: sqlite3.Connection, ids: list[str]) -> dict[str, dict]:
    meta = {}
    for linha in con.execute(
        f"SELECT video_id, titulo, artistas, album_nome, duracao_s FROM faixas WHERE video_id IN ({','.join('?' * len(ids))})",
        ids,
    ):
        artistas = json.loads(linha["artistas"])
        meta[linha["video_id"]] = {
            "titulo": normalizar(linha["titulo"]),
            "artistas": [normalizar(a["nome"]) for a in artistas],
            "artista_principal": artistas[0]["id"] if artistas else f"sem-artista:{linha['video_id']}",
            "album": normalizar(linha["album_nome"]),
            "duracao_s": linha["duracao_s"],
            "playlists": set(),
        }
    for vid, pid in con.execute(
        f"SELECT pf.video_id, pf.playlist_id FROM playlist_faixas pf JOIN playlists p USING (playlist_id) "
        f"WHERE p.propria = 1 AND p.ignorada = 0 AND pf.video_id IN ({','.join('?' * len(ids))})",
        ids,
    ):
        meta[vid]["playlists"].add(pid)
    return meta


def _indice_rotulos(con: sqlite3.Connection) -> dict[str, list[tuple[str, int]]]:
    """rótulo → [(tipo do vetor, índice)]; o mesmo nome pode existir em mais de um modelo."""
    out = defaultdict(list)
    for tipo, indice, rotulo in con.execute(
        "SELECT tipo, indice, rotulo FROM rotulos_modelo ORDER BY tipo, indice"
    ):
        out[rotulo].append((tipo, indice))
    return out


class _Probs:
    """Probabilidade de um rótulo por faixa (máximo entre os modelos que têm o rótulo) e afinidade de gênero-pai."""

    def __init__(self, con, ids: list[str]):
        self.con, self.ids = con, ids
        self.indice = _indice_rotulos(con)
        self.matrizes = {t: espacos.probabilidades(con, t) for t in espacos.TIPOS_PROBABILIDADE}
        self._afinidades: generos.Afinidades | None = None

    def de(self, rotulo: str) -> np.ndarray:
        out = np.zeros(len(self.ids), np.float32)
        for tipo, idx in self.indice.get(rotulo, []):
            m = self.matrizes[tipo]
            col = np.array([m.X[m.indice[v], idx] if v in m.indice else 0.0 for v in self.ids], np.float32)
            out = np.maximum(out, col)
        return out

    def do_pai(self, pai: str) -> np.ndarray:
        """Afinidade relativa (#83): 1,0 = tão típica do gênero quanto a faixa mediana do núcleo dele."""
        if self._afinidades is None:
            self._afinidades = generos.Afinidades(self.con)
        return self._afinidades.relativa(pai, self.ids).astype(np.float32)


def _menciona(texto_norm: str, nome_norm: str) -> bool:
    return bool(nome_norm) and re.search(rf"(^| ){re.escape(nome_norm)}( |$)", texto_norm) is not None


# ------------------------------------------------------------------ execução


def executar(con: sqlite3.Connection, r: Receita, universo: list[str] | None = None) -> Execucao:
    """`universo` substitui o da receita (recomendações do modo lento: as candidatas analisadas)."""
    validar(con, r)
    universo = _universo(con, r) if universo is None else sorted(set(universo))
    dislikes = consultas.com_dislike(con)
    ignoradas = consultas.ignoradas(con)
    if n := sum(v in ignoradas for v in universo):
        universo = [v for v in universo if v not in ignoradas]
    sem_dislike = [v for v in universo if v not in dislikes]
    analisadas = set(espacos.analisadas(con))
    cand = [v for v in sem_dislike if v in analisadas]
    ex = Execucao(r.hash(), espacos.hash_biblioteca(con), [], len(universo), len(sem_dislike) - len(cand), 0)
    if len(sem_dislike) < len(universo):
        ex.descartes["dislike"] = len(universo) - len(sem_dislike)
    if n:
        ex.descartes["playlist ignorada"] = n
    if not cand:
        return ex

    meta = _metadados(con, cand)
    esc_todas = espacos.escalares(con)
    # média/desvio sobre a biblioteca analisada (sem dislikes nem faixas de fora): candidatas não mexem na régua
    _, media, desvio = espacos.zscore(esc_todas.subconjunto(espacos.da_biblioteca(con)).X)
    esc = esc_todas.subconjunto(cand)
    col = {c: i for i, c in enumerate(esc.colunas)}
    probs = _Probs(con, cand)
    f = r.filtros

    # 2. filtros duros
    manter = np.ones(len(cand), bool)

    def aplicar(motivo: str, mascara: np.ndarray) -> None:
        nonlocal manter
        ex.descartes[motivo] += int(np.sum(manter & ~mascara))
        manter &= mascara

    if f.excluir_artistas:
        proibidos = [normalizar(a) for a in f.excluir_artistas]
        aplicar("artista excluído", np.array([
            not any(a in proibidos for a in meta[v]["artistas"]) and not any(_menciona(meta[v]["titulo"], p) for p in proibidos)
            for v in cand
        ]))  # fmt: skip
    if f.exigir_artistas:
        exigidos = {normalizar(a) for a in f.exigir_artistas}
        aplicar("artista não exigido", np.array([bool(exigidos & set(meta[v]["artistas"])) for v in cand]))
    if f.excluir_albuns_contendo:
        trechos = [normalizar(t) for t in f.excluir_albuns_contendo]
        aplicar(
            "álbum excluído", np.array([not any(t and t in meta[v]["album"] for t in trechos) for v in cand])
        )
    if f.excluir_playlists:
        fora = {resolver_playlist(con, p) for p in f.excluir_playlists}
        aplicar("playlist excluída", np.array([not (meta[v]["playlists"] & fora) for v in cand]))
    if f.somente_playlists:
        dentro = {resolver_playlist(con, p) for p in f.somente_playlists}
        aplicar("fora das playlists exigidas", np.array([bool(meta[v]["playlists"] & dentro) for v in cand]))
    if f.duracao_s:
        lo, hi = f.duracao_s
        aplicar(
            "duração",
            np.array([meta[v]["duracao_s"] is not None and lo <= meta[v]["duracao_s"] <= hi for v in cand]),
        )
    limites = dict(f.min)
    tetos = dict(f.max)
    if f.bpm:
        limites["bpm"], tetos["bpm"] = (
            max(limites.get("bpm", -np.inf), f.bpm[0]),
            min(tetos.get("bpm", np.inf), f.bpm[1]),
        )
    for nome, lo in limites.items():
        x = esc.X[:, col[nome]]
        aplicar(f"{nome} < {lo}", ~np.isnan(x) & (x >= lo))
    for nome, hi in tetos.items():
        x = esc.X[:, col[nome]]
        aplicar(f"{nome} > {hi}", ~np.isnan(x) & (x <= hi))
    if f.generos_min:
        ok = np.zeros(len(cand), bool)
        for rot, lo in f.generos_min.items():
            ok |= probs.de(rot) >= lo
        aplicar("gênero abaixo do mínimo", ok)
    if f.genero_pai_min:
        ok = np.zeros(len(cand), bool)
        for pai, lo in f.genero_pai_min.items():
            ok |= probs.do_pai(pai) >= lo
        aplicar("gênero-pai abaixo do mínimo", ok)

    idx = np.nonzero(manter)[0]
    ex.apos_filtros = len(idx)
    if not len(idx):
        return ex
    ids = [cand[i] for i in idx]

    # 3. componentes do score, cada um em 0..1
    comps: dict[str, np.ndarray] = {}
    pesos: dict[str, float] = {}
    if r.perfil_alvo:
        soma_w, d2 = 0.0, np.zeros(len(idx))
        for nome, alvo in r.perfil_alvo.items():
            j = col[nome]
            x = esc.X[idx, j]
            dz = np.where(
                np.isnan(x), 1.0, (x - alvo.alvo) / desvio[j]
            )  # feature ausente: 1 desvio de penalidade
            d2 += alvo.peso * dz**2
            soma_w += alvo.peso
        comps["perfil"], pesos["perfil"] = np.exp(-d2 / (2 * soma_w)), r.pesos.perfil
    emb = espacos.embeddings(con).subconjunto(ids)
    E = np.stack([emb.linha(v) for v in ids]) if len(emb.ids) == len(ids) else None
    if r.sementes and E is not None:
        todas = espacos.embeddings(con)
        centro = np.mean([todas.linha(s) for s in r.sementes], axis=0)
        centro /= np.linalg.norm(centro) or 1
        comps["sementes"], pesos["sementes"] = (E @ centro + 1) / 2, r.pesos.sementes
    rotulos = dict(r.generos_preferidos) or {rot: 1.0 for rot in f.generos_min}
    pais = {} if r.generos_preferidos else dict.fromkeys(f.genero_pai_min, 1.0)
    if rotulos or pais:
        soma_w, acc = 0.0, np.zeros(len(idx))
        for rot, w in rotulos.items():
            p = probs.de(rot)[idx]
            acc += w * p / (p.max() or 1)
            soma_w += w
        for pai, w in pais.items():
            p = probs.do_pai(pai)[idx]
            acc += w * p / (p.max() or 1)
            soma_w += w
        comps["generos"], pesos["generos"] = acc / soma_w, r.pesos.generos
    ativos = [k for k in comps if pesos[k] > 0]
    if ativos:
        score = sum(pesos[k] * comps[k] for k in ativos) / sum(pesos[k] for k in ativos)
    else:  # nada para pontuar: curtidas mais recentes primeiro, estável
        posicao = dict(con.execute("SELECT video_id, coalesce(posicao_curtida, 1e9) FROM faixas"))
        score = np.array([1 / (1 + posicao[v]) for v in ids])

    # 4. ordenação total
    ordem = sorted(range(len(ids)), key=lambda i: (-float(score[i]), ids[i]))

    # 5. MMR + limite por artista
    lam = r.diversidade.lambda_
    limite = r.diversidade.max_por_artista
    por_artista: Counter = Counter()
    escolhidos: list[int] = []
    sim_max = np.full(len(ids), -np.inf)
    restantes = list(ordem)
    while restantes and len(escolhidos) < r.tamanho:
        elegiveis = [
            i for i in restantes if limite is None or por_artista[meta[ids[i]]["artista_principal"]] < limite
        ]
        if not elegiveis:
            break
        if lam >= 1 or not escolhidos or E is None:
            pegar = elegiveis[0]
        else:
            # elegiveis está em ordem de relevância; max() fica com o primeiro em empate
            pegar = max(elegiveis, key=lambda i: lam * float(score[i]) - (1 - lam) * float(sim_max[i]))
        escolhidos.append(pegar)
        restantes.remove(pegar)
        por_artista[meta[ids[pegar]]["artista_principal"]] += 1
        if E is not None:
            sim_max = np.maximum(sim_max, (E @ E[pegar] + 1) / 2)

    if (
        restantes and len(escolhidos) < r.tamanho
    ):  # sobrou espaço: o que ficou de fora foi pelo limite por artista
        ex.descartes["limite por artista"] = len(restantes)
    ex.itens = [
        Item(ids[i], round(float(score[i]), 6), {k: round(float(v[i]), 6) for k, v in comps.items()})
        for i in escolhidos
    ]
    return ex
