"""Onde colocar as faixas não alocadas (ADR-06): o pertencimento atual é o rótulo supervisionado.

    s(t, p) = α · média(cos com os k vizinhos de t entre os membros de p) + (1 − α) · cos(t, centroide de p)

O limiar τₚ de cada playlist sai de leave-one-out sobre os próprios membros (percentil 10 dos scores que
eles recebem quando retirados), então playlists "fechadas" exigem mais semelhança que as "abertas".
Decisão de cima para baixo: a faixa só é comparada com as filhas das mães em que passou. Multi-rótulo:
entra em toda playlist em que s ≥ τ.

Só nas playlists que o usuário nomeia (#80): `em` (playlists), `mae` (as filhas dela) ou `nas_gerais` (as
criadas por `curator playlists sugerir`). Sem alvo, nada é sugerido: as playlists existentes não são mexidas sem
pedido.
"""

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

from curator.banco import consultas
from curator.motor import espacos

K, ALFA, PERCENTIL, MIN_MEMBROS = 10, 0.6, 10, 5
MARGEM_AMBIGUA = 0.02


class SemAlvo(ValueError):
    """Classificar exige dizer em quais playlists (#80)."""


@dataclass
class Perfil:
    playlist_id: str
    titulo: str
    membros: list[str]
    E: np.ndarray  # embeddings dos membros (normalizados)
    centroide: np.ndarray
    limiar: float
    filhas: list[str] = field(default_factory=list)


@dataclass
class Destino:
    playlist_id: str
    titulo: str
    score: float
    limiar: float


@dataclass
class Decisao:
    video_id: str
    destinos: list[Destino]
    proximas: list[Destino]  # quando não há destino: as 3 mais próximas, para sugerir playlist nova
    ambigua: bool  # algum score a menos de MARGEM_AMBIGUA do limiar: caso para a IA explicar


@dataclass
class Classificacao:
    decisoes: list[Decisao]
    frias: list[tuple[str, int]]  # (título, membros analisados) das playlists com poucos membros
    sem_analise: int


def _score(e: np.ndarray, E: np.ndarray, centroide: np.ndarray, k: int, alfa: float) -> float:
    if not len(E):
        return -1.0
    sims = E @ e
    kk = min(k, len(sims))
    topk = np.partition(sims, -kk)[-kk:]
    return float(alfa * topk.mean() + (1 - alfa) * float(centroide @ e))


def _centroide(E: np.ndarray) -> np.ndarray:
    c = E.mean(axis=0)
    return c / (np.linalg.norm(c) or 1)


def limiar_loo(E: np.ndarray, k: int = K, alfa: float = ALFA, percentil: float = PERCENTIL) -> float:
    scores = []
    for i in range(len(E)):
        resto = np.delete(E, i, axis=0)
        scores.append(_score(E[i], resto, _centroide(resto), k, alfa))
    return float(np.percentile(scores, percentil))


def perfis(con: sqlite3.Connection, k: int = K, alfa: float = ALFA, percentil: float = PERCENTIL,
           sem: set[str] | None = None) -> tuple[dict[str, Perfil], list[tuple[str, int]]]:  # fmt: skip
    """Perfil de cada playlist própria com membros analisados suficientes. `sem` tira faixas (avaliação)."""
    emb = espacos.embeddings(con)
    sem = (sem or set()) | consultas.bloqueadas(con)  # dislikes e ignoradas não puxam o centroide
    pl = con.execute(
        "SELECT playlist_id, titulo, mae_id FROM playlists WHERE propria = 1 AND descobertas = 0 AND ignorada = 0 ORDER BY playlist_id"
    ).fetchall()
    filhas = defaultdict(list)
    for p in pl:
        if p["mae_id"]:
            filhas[p["mae_id"]].append(p["playlist_id"])

    def descendentes(pid: str) -> list[str]:
        out = [pid]
        for f in filhas.get(pid, []):
            out += descendentes(f)
        return out

    membros_de = defaultdict(set)
    for pid, vid in con.execute("SELECT playlist_id, video_id FROM playlist_faixas"):
        membros_de[pid].add(vid)
    out, frias = {}, []
    for p in pl:
        pid = p["playlist_id"]
        # mãe: os próprios membros + os das descendentes (para agregadoras, dá no mesmo)
        membros = sorted(
            {v for d in descendentes(pid) for v in membros_de[d] if v in emb.indice and v not in sem}
        )
        if len(membros) < MIN_MEMBROS:
            frias.append((p["titulo"], len(membros)))
            continue
        E = np.stack([emb.linha(v) for v in membros])
        out[pid] = Perfil(
            pid,
            p["titulo"],
            membros,
            E,
            _centroide(E),
            limiar_loo(E, k, alfa, percentil),
            filhas.get(pid, []),
        )
    return out, frias


def _raizes(con: sqlite3.Connection, mae: str | None) -> list[str]:
    if mae:
        return [
            r[0]
            for r in con.execute(
                "SELECT playlist_id FROM playlists WHERE mae_id = ? ORDER BY playlist_id", (mae,)
            )
        ]
    return [
        r[0]
        for r in con.execute(
            "SELECT playlist_id FROM playlists WHERE propria = 1 AND descobertas = 0 AND ignorada = 0 AND mae_id IS NULL ORDER BY playlist_id"
        )
    ]


def decidir(
    e: np.ndarray, video_id: str, ps: dict[str, Perfil], raizes: list[str], k: int, alfa: float
) -> Decisao:
    destinos, avaliados = [], []

    def visitar(pid: str) -> None:
        p = ps.get(pid)
        if p is None:
            return
        s = _score(e, p.E, p.centroide, k, alfa)
        d = Destino(pid, p.titulo, round(s, 4), round(p.limiar, 4))
        avaliados.append(d)
        if s < p.limiar:
            return
        aceitas_antes = len(destinos)
        for f in p.filhas:
            visitar(f)
        if len(destinos) == aceitas_antes:  # nenhuma filha aceitou (ou é folha): fica nesta
            destinos.append(d)

    for r in raizes:
        visitar(r)
    proximas = [] if destinos else sorted(avaliados, key=lambda d: (-(d.score - d.limiar), d.playlist_id))[:3]
    ambigua = any(abs(d.score - d.limiar) < MARGEM_AMBIGUA for d in avaliados)
    return Decisao(video_id, sorted(destinos, key=lambda d: (-d.score, d.playlist_id)), proximas, ambigua)


def alvos(con: sqlite3.Connection, mae: str | None = None, em: list[str] | None = None,
          nas_gerais: bool = False) -> list[str]:  # fmt: skip
    """As playlists que a classificação pode sugerir, na ordem do pedido e sem repetir. Levanta `SemAlvo`."""
    from curator.motor import gerais

    out = list(em or [])
    if mae:
        out += _raizes(con, mae)
    if nas_gerais:
        if not (g := gerais.playlists(con)):
            raise SemAlvo(
                "nenhuma playlist geral ainda: rode `curator playlists sugerir --plano` e aplique o plano"
            )
        out += g
    if not out:
        raise SemAlvo("diga em quais playlists: --em (uma ou mais), --mae (as filhas dela) ou --nas-gerais")
    return list(dict.fromkeys(out))


def classificar(con: sqlite3.Connection, ids: list[str] | None = None, mae: str | None = None,
                k: int = K, alfa: float = ALFA, percentil: float = PERCENTIL, em: list[str] | None = None,
                nas_gerais: bool = False) -> Classificacao:  # fmt: skip
    """Entre os `alvos` pedidos. Por padrão, as curtidas não alocadas; só com `nas_gerais`, as curtidas que ainda
    não estão em nenhuma geral (as outras playlists não contam). Faixas sem análise ficam de fora (e são contadas)."""
    raizes = alvos(con, mae, em, nas_gerais)
    if ids is None:
        ids = (
            consultas.fora_das_gerais(con) if nas_gerais and not (em or mae) else consultas.nao_alocadas(con)
        )
    bloqueadas = consultas.bloqueadas(con)
    ids = [v for v in ids if v not in bloqueadas]
    emb = espacos.embeddings(con)
    com = [v for v in ids if v in emb.indice]
    ps, frias = perfis(con, k, alfa, percentil)
    decisoes = [decidir(emb.linha(v), v, ps, raizes, k, alfa) for v in com]
    titulos = {r[0] for r in con.execute(
        f"SELECT titulo FROM playlists WHERE playlist_id IN ({','.join('?' * len(raizes))})", raizes
    )}  # fmt: skip
    return Classificacao(decisoes, [f for f in frias if f[0] in titulos], len(ids) - len(com))


# ------------------------------------------------------------------ avaliação (#28)


@dataclass
class Metrica:
    titulo: str
    membros: int
    precisao: float
    revocacao: float


def avaliar(con: sqlite3.Connection, mae: str | None = None, k: int = K, alfa: float = ALFA,
            percentil: float = PERCENTIL) -> list[Metrica]:  # fmt: skip
    """Leave-one-out: cada membro é tirado de todas as playlists e classificado; compara com onde ele estava."""
    emb = espacos.embeddings(con)
    raizes = _raizes(con, mae)
    base, _ = perfis(con, k, alfa, percentil)
    folhas = {pid for pid, p in base.items() if not p.filhas}
    escopo = folhas if mae is None else {f for f in folhas if f in set(_descendentes_de(base, mae))}
    verdade = defaultdict(set)
    for pid, vid in con.execute("SELECT playlist_id, video_id FROM playlist_faixas"):
        if pid in escopo:
            verdade[vid].add(pid)
    acertos, previstos, reais = defaultdict(int), defaultdict(int), defaultdict(int)
    for vid in sorted(verdade):
        if vid not in emb.indice:
            continue
        ps, _ = perfis(con, k, alfa, percentil, sem={vid})
        d = decidir(emb.linha(vid), vid, ps, raizes, k, alfa)
        prev = {x.playlist_id for x in d.destinos} & escopo
        for pid in prev:
            previstos[pid] += 1
            acertos[pid] += pid in verdade[vid]
        for pid in verdade[vid]:
            reais[pid] += 1
    return [
        Metrica(
            base[pid].titulo,
            reais[pid],
            round(acertos[pid] / previstos[pid], 3) if previstos[pid] else 0.0,
            round(acertos[pid] / reais[pid], 3) if reais[pid] else 0.0,
        )  # fmt: skip
        for pid in sorted(escopo, key=lambda p: base[p].titulo)
    ]


def _descendentes_de(ps: dict[str, Perfil], pid: str) -> list[str]:
    out = [pid]
    for f in ps[pid].filhas if pid in ps else []:
        out += _descendentes_de(ps, f)
    return out
