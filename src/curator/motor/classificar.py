"""Onde colocar as faixas não alocadas (ADR-06): o pertencimento atual é o rótulo supervisionado.

    s(t, p) = α · média(cos com os k vizinhos de t entre os membros de p) + (1 − α) · cos(t, centroide de p)

O limiar τₚ de cada playlist sai de leave-one-out sobre os próprios membros (percentil 10 dos scores que
eles recebem quando retirados), então playlists "fechadas" exigem mais semelhança que as "abertas".
Decisão de cima para baixo: a faixa só é comparada com as filhas das mães em que passou. Multi-rótulo:
entra em toda playlist em que s ≥ τ.

Só nas playlists que o usuário nomeia (#80): `em` (playlists), `mae` (as filhas dela) ou `nas_gerais` (as
criadas por `curator playlists sugerir`). Sem alvo, nada é sugerido: as playlists existentes não são mexidas sem
pedido.

Regras das marcações (#107, `motor/marcacoes.py`): quando vêm em `regras`, valem DEPOIS do áudio, só nas playlists
pedidas. A etiqueta da faixa acrescenta destinos (`entrar_em`, com `Destino.regra`) e barra outros (`sair_de`, em
`Decisao.barradas`). O score e os limiares não mudam.
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
    regra: str | None = None  # entrou pela marcação (#107), não pelo score


@dataclass
class Decisao:
    video_id: str
    destinos: list[Destino]
    proximas: list[Destino]  # quando não há destino: as 3 mais próximas, para sugerir playlist nova
    ambigua: bool  # algum score a menos de MARGEM_AMBIGUA do limiar: caso para a IA explicar
    # destinos do áudio que uma marcação tirou (#107): (destino, etiqueta)
    barradas: list[tuple[Destino, str]] = field(default_factory=list)


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
    """O percentil dos scores que os membros recebem quando retirados, um de cada vez. Todos de uma vez: uma
    matriz de cossenos sem a diagonal dá os vizinhos, e a soma menos a própria linha dá o centroide dos outros."""
    n = len(E)
    if n < 2:
        return -1.0  # sem os outros não há score (como `_score` com a playlist vazia)
    sims = E @ E.T
    np.fill_diagonal(sims, -np.inf)
    kk = min(k, n - 1)
    vizinhos = np.partition(sims, n - kk, axis=1)[:, n - kk :].mean(axis=1)
    outros = E.sum(axis=0) - E
    outros /= np.where((norma := np.linalg.norm(outros, axis=1, keepdims=True)) == 0, 1, norma)
    return float(np.percentile(alfa * vizinhos + (1 - alfa) * (E * outros).sum(axis=1), percentil))


def perfis(con: sqlite3.Connection, k: int = K, alfa: float = ALFA, percentil: float = PERCENTIL,
           sem: set[str] | None = None, so: list[str] | None = None) -> tuple[dict[str, Perfil], list[tuple[str, int]]]:  # fmt: skip
    """Perfil de cada playlist própria com membros analisados suficientes. `sem` tira faixas (avaliação); `so`
    restringe às playlists dadas e às descendentes delas."""
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
    if so is not None:
        pedidas = {d for raiz in so for d in descendentes(raiz)}
        pl = [p for p in pl if p["playlist_id"] in pedidas]
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
                nas_gerais: bool = False, regras: list | None = None) -> Classificacao:  # fmt: skip
    """Entre os `alvos` pedidos. Por padrão, as curtidas não alocadas; só com `nas_gerais`, as curtidas que ainda
    não estão em nenhuma geral (as outras playlists não contam). Faixas sem análise ficam de fora (e são contadas).
    `regras` (#107): as das marcações, aplicadas depois do áudio (ver `aplicar_regras`)."""
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
    if regras:
        decisoes = aplicar_regras(con, decisoes, regras, raizes, ps, emb, k, alfa)
    titulos = {r[0] for r in con.execute(
        f"SELECT titulo FROM playlists WHERE playlist_id IN ({','.join('?' * len(raizes))})", raizes
    )}  # fmt: skip
    return Classificacao(decisoes, [f for f in frias if f[0] in titulos], len(ids) - len(com))


def aplicar_regras(con: sqlite3.Connection, decisoes: list[Decisao], regras: list, raizes: list[str],
                   ps: dict[str, Perfil], emb, k: int = K, alfa: float = ALFA) -> list[Decisao]:  # fmt: skip
    """As regras das marcações (#107) sobre as decisões do áudio, só entre as playlists pedidas (`raizes`):
    `entrar_em` acrescenta o destino (com o score, para informação), `sair_de` tira. Entrar vence sair."""
    from curator.motor import marcacoes

    pedidas = set(raizes)
    etiquetas = marcacoes.da_faixa(con, [d.video_id for d in decisoes])
    titulos = dict(con.execute("SELECT playlist_id, titulo FROM playlists"))
    for d in decisoes:
        entrar, sair = marcacoes.destinos_por_regra(etiquetas.get(d.video_id, set()), regras)
        fica = []
        for x in d.destinos:
            if x.playlist_id in sair and x.playlist_id in pedidas:
                d.barradas.append((x, sair[x.playlist_id]))
            else:
                fica.append(x)
        ja = {x.playlist_id for x in fica}
        for pid, e in entrar.items():
            if pid in pedidas and pid not in ja:
                p = ps.get(pid)
                s = _score(emb.linha(d.video_id), p.E, p.centroide, k, alfa) if p else float("nan")
                fica.append(Destino(pid, titulos.get(pid, pid), round(s, 4),
                                    round(p.limiar, 4) if p else float("nan"), regra=e))  # fmt: skip
        d.destinos = sorted(
            fica, key=lambda x: (x.regra is not None, -x.score if x.score == x.score else 0, x.playlist_id)
        )
        if d.destinos:
            d.proximas = []
    return decisoes


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
    so = [mae] if mae else None  # com mãe, só ela e as descendentes entram na decisão
    base, _ = perfis(con, k, alfa, percentil, so=so)
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
        ps, _ = perfis(con, k, alfa, percentil, sem={vid}, so=so)
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
