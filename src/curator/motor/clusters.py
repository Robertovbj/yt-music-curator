"""Reorganização (ADR-07): UMAP → HDBSCAN → pertencimento sobreposto → descrição → nome.

Determinístico: UMAP com `random_state` fixo (o umap-learn força 1 thread nesse caso), HDBSCAN não é
aleatório, e o resto é álgebra com desempate por id. Mesmos dados + mesmos parâmetros = mesmos clusters.

Sobreposição: o HDBSCAN dá uma partição só como ponto de partida. Cada cluster tem um núcleo (os membros
com probabilidade acima da mediana do cluster) e um centroide calculado pelo núcleo. Os membros são a partição
do HDBSCAN MAIS toda faixa com cos(t, centroide) ≥ τ_c, onde τ_c é o percentil 10 da similaridade dos membros
do HDBSCAN ao centroide. Ninguém sai do próprio cluster; a sobreposição só acrescenta. (O ADR-07 previa o
percentil 25 só do núcleo, o que deixava de fora um quarto do próprio núcleo e metade do estilo.)

Estabilidade: um agrupamento fica salvo. `atualizar` encaixa faixas novas nos clusters existentes pelo
centroide; só `agrupar` (sob pedido) recalcula tudo.

Playlists master (#105, `Parametros.quantas`): N grupos exatos, amplos, cobrindo todas as faixas. O HDBSCAN não
deixa fixar N, então:
1. Partição por consenso: Ward com N grupos sobre o UMAP de cada uma das `sementes`, uma matriz de
   coassociação (fração das sementes em que duas faixas ficaram juntas) e ligação média sobre 1 − coassociação
   (Monti et al., 2003; Strehl & Ghosh, 2002). Um UMAP só muda a fronteira quando a biblioteca muda pouco; o
   consenso não (na biblioteca real, tirando 1% das faixas: ARI 0,97–0,99 contra 0,76–0,96).
2. Fronteira: toda faixa fica na sua master e entra também na master mais próxima depois dela, se estiver quase
   tão perto (cos ≥ cos da sua − `fronteira`) e passar do limiar τ dela (percentil 10 dos membros). No máximo duas.
"""

import hashlib
import json
import sqlite3
import warnings
from collections import Counter
from dataclasses import asdict, dataclass, field
from itertools import combinations

import numpy as np

from curator.banco import vetores
from curator.banco.conexao import agora, transacao
from curator.motor import escrita, espacos


@dataclass(frozen=True)
class Parametros:
    dims: int = 10
    n_neighbors: int = 15
    min_dist: float = 0.0
    random_state: int = 42
    min_cluster_size: int = 12
    min_samples: int | None = None
    percentil_limiar: float = 10
    jaccard_fusao: float = 0.6
    selecao: str = "eom"  # HDBSCAN: "eom" (o padrão) ou "leaf" (folhas da hierarquia: grupos mais parelhos)
    # playlists master (#105): N grupos exatos por consenso, no lugar do HDBSCAN
    quantas: int | None = None
    sementes: tuple[int, ...] = (42, 7, 13, 21, 99)
    fronteira: float = 0.02


@dataclass
class Cluster:
    cluster_id: int
    nucleo: list[str]
    centroide: np.ndarray
    limiar: float
    membros: dict[str, float] = field(default_factory=dict)  # video_id → similaridade ao centroide
    nome: str = ""
    estatisticas: dict = field(default_factory=dict)

    @property
    def hash_nucleo(self) -> str:
        return hashlib.sha256("\n".join(sorted(self.nucleo)).encode()).hexdigest()[:16]


CURTIDAS = "curtidas"  # escopo das playlists gerais (#80): todas as curtidas, ignorando as playlists


class PoucasFaixas(ValueError):
    pass


# ------------------------------------------------------------------ cálculo


def reduzir(X: np.ndarray, p: Parametros, dims: int | None = None) -> np.ndarray:
    import umap

    dims = dims or p.dims
    with warnings.catch_warnings():  # "n_jobs overridden to 1 by setting random_state": é o que queremos
        warnings.simplefilter("ignore")
        modelo = umap.UMAP(
            n_components=dims, n_neighbors=min(p.n_neighbors, len(X) - 1), min_dist=p.min_dist,
            metric="cosine", random_state=p.random_state,
        )  # fmt: skip
        return modelo.fit_transform(X).astype(np.float32)


def particionar(Y: np.ndarray, p: Parametros) -> tuple[np.ndarray, np.ndarray]:
    from sklearn.cluster import HDBSCAN

    h = HDBSCAN(
        min_cluster_size=p.min_cluster_size,
        min_samples=p.min_samples,
        cluster_selection_method=p.selecao,
        copy=True,
    ).fit(Y)
    return h.labels_, h.probabilities_


def particionar_consenso(X: np.ndarray, p: Parametros) -> np.ndarray:
    """Partição em exatamente `p.quantas` grupos, por consenso de Ward sobre UMAPs com várias sementes.

    Rótulos em ordem canônica (maior grupo primeiro; empate pelo menor índice), para não depender da numeração
    interna do scikit-learn."""
    from dataclasses import replace

    from sklearn.cluster import AgglomerativeClustering

    co = np.zeros((len(X), len(X)), np.float32)
    for s in p.sementes:
        Y = reduzir(X, replace(p, random_state=s))
        lab = AgglomerativeClustering(n_clusters=p.quantas, linkage="ward").fit_predict(Y)
        co += lab[:, None] == lab[None, :]
    co /= len(p.sementes)
    lab = AgglomerativeClustering(n_clusters=p.quantas, metric="precomputed", linkage="average").fit_predict(
        1 - co
    )
    ordem = sorted(set(lab.tolist()), key=lambda k: (-(lab == k).sum(), int(np.flatnonzero(lab == k)[0])))
    return np.array([ordem.index(k) for k in lab])


def segunda_master(sims: np.ndarray, propria: int, limiares: np.ndarray, fronteira: float) -> int | None:
    """A master mais próxima depois da `propria`, se a faixa está na fronteira (quase tão perto e acima do τ dela)."""
    outras = sims.copy()
    outras[propria] = -np.inf
    k2 = int(outras.argmax())
    if outras[k2] >= sims[propria] - fronteira and outras[k2] >= limiares[k2]:
        return k2
    return None


def masters(m: espacos.Matriz, rotulos: np.ndarray, p: Parametros) -> list[Cluster]:
    """Cada faixa na sua master (núcleo) e, na fronteira, também na segunda."""
    ks = range(p.quantas)
    C = np.vstack([_centroide(m.X[rotulos == k]) for k in ks])
    S = m.X @ C.T
    limiares = np.array([float(np.percentile(S[rotulos == k, k], p.percentil_limiar)) for k in ks])
    membros: dict[int, dict[str, float]] = {k: {} for k in ks}
    for i, k in enumerate(rotulos):
        membros[k][m.ids[i]] = round(float(S[i, k]), 6)
        if (k2 := segunda_master(S[i], k, limiares, p.fronteira)) is not None:
            membros[k2][m.ids[i]] = round(float(S[i, k2]), 6)
    return [
        Cluster(k, sorted(m.ids[i] for i in np.flatnonzero(rotulos == k)), C[k], float(limiares[k]),
                dict(sorted(membros[k].items())))
        for k in ks
    ]  # fmt: skip


def _centroide(E: np.ndarray) -> np.ndarray:
    c = E.mean(axis=0)
    return c / (np.linalg.norm(c) or 1)


def sobrepor(m: espacos.Matriz, rotulos: np.ndarray, probs: np.ndarray, p: Parametros) -> list[Cluster]:
    """Núcleo + centroide por cluster do HDBSCAN e atribuição sobreposta de toda faixa (inclusive ruído)."""
    out = []
    for novo_id, rot in enumerate(sorted(set(rotulos) - {-1})):
        idx = np.nonzero(rotulos == rot)[0]
        mediana = np.median(probs[idx])
        nuc = [i for i in idx if probs[i] >= mediana] or list(idx)
        c = _centroide(m.X[nuc])
        sims = m.X @ c
        limiar = float(np.percentile(sims[idx], p.percentil_limiar))
        dentro = set(idx.tolist()) | set(np.nonzero(sims >= limiar)[0].tolist())
        membros = {m.ids[i]: round(float(sims[i]), 6) for i in sorted(dentro)}
        out.append(Cluster(novo_id, sorted(m.ids[i] for i in nuc), c, limiar, membros))
    return out


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def sobreposicoes(cs: list[Cluster], minimo: float = 0.0) -> list[tuple[int, int, float]]:
    pares = [
        (a.cluster_id, b.cluster_id, round(jaccard(set(a.membros), set(b.membros)), 4))
        for a, b in combinations(cs, 2)
    ]
    return sorted((x for x in pares if x[2] >= minimo), key=lambda x: (-x[2], x[0], x[1]))


# ------------------------------------------------------------------ descrição e nome (#37)


def _rotulo_curto(r: str) -> str:
    return r.split("---", 1)[1] if "---" in r else r


def _faixa(v: float | None, cortes: tuple[float, float], nomes: tuple[str, str, str]) -> str | None:
    if v is None or np.isnan(v):
        return None
    return nomes[0] if v < cortes[0] else nomes[2] if v > cortes[1] else nomes[1]


def descrever(con: sqlite3.Connection, ids: list[str]) -> dict:
    """Estatísticas agregadas: o que a IA vai ler para nomear o cluster (e o que o nome automático usa)."""
    esc = espacos.escalares(con).subconjunto(ids)
    col = {c: i for i, c in enumerate(esc.colunas)}

    def med(nome: str) -> float:
        x = esc.X[:, col[nome]]
        return float(np.nanmedian(x)) if len(x) and not np.isnan(x).all() else float("nan")

    est = {"faixas": len(ids)}
    for nome in (
        "bpm",
        "energy",
        "valence",
        "arousal",
        "danceability",
        "acousticness",
        "aggressive",
        "relaxed",
        "instrumental",
    ):
        v = med(nome)
        est[nome] = None if np.isnan(v) else round(v, 3)
    for tipo, chave, n in (
        ("discogs400", "estilos", 5),
        ("jamendo_mood", "humores", 3),
        ("jamendo_instr", "instrumentos", 3),
    ):
        pm = espacos.probabilidades(con, tipo).subconjunto(ids)
        if len(pm.ids):
            media = pm.X.mean(axis=0)
            top = sorted(range(len(media)), key=lambda i: (-float(media[i]), pm.colunas[i]))[:n]
            est[chave] = [[pm.colunas[i], round(float(media[i]), 3)] for i in top]
    artistas = Counter()
    for (lista,) in con.execute(
        f"SELECT artistas FROM faixas WHERE video_id IN ({','.join('?' * len(ids))})", ids
    ):
        artistas.update(a["nome"] for a in json.loads(lista)[:1])
    est["artistas"] = [[a, n] for a, n in sorted(artistas.items(), key=lambda x: (-x[1], x[0]))[:5]]
    # #111: fração das faixas por escrita do título/álbum/artistas, o que o áudio não diz (idioma e mercado)
    est["escritas"] = escrita.proporcoes(escrita.calcular(con, ids), ids)
    return est


def nome_automatico(est: dict) -> str:
    """Nome provisório, determinístico, a partir das estatísticas. A IA (M4) pode substituí-lo."""
    partes = []
    # estilo de mercado ("J-pop", "K-pop") só com a escrita das faixas sustentando (#111): o som não diz o idioma.
    # Estatísticas antigas, sem "escritas", ficam como estavam
    escritas = est.get("escritas")
    estilos = [
        (r, p)
        for r, p in est.get("estilos") or []
        if escritas is None
        or r not in escrita.MERCADOS
        or escritas.get(escrita.MERCADOS[r], 0) >= escrita.SUSTENTA_MERCADO
    ]
    if estilos:
        (r1, p1), *resto = estilos
        # o segundo estilo só entra se for relevante (≥ metade do primeiro); senão é ruído
        segundo = [_rotulo_curto(r) for r, p in resto[:1] if p >= 0.5 * p1]
        partes.append(" / ".join([_rotulo_curto(r1), *segundo]))
    energia = _faixa(est.get("energy"), (0.33, 0.66), ("calmo", "moderado", "energia alta"))
    humor = _faixa(est.get("valence"), (0.4, 0.6), ("melancólico", None, "alegre"))
    partes += [x for x in (energia, humor) if x]
    return " · ".join(partes) or "sem nome"


# ------------------------------------------------------------------ orquestração e persistência


def _universo(con: sqlite3.Connection, escopo: str) -> list[str]:
    """Faixas do escopo, sem as que têm dislike (#61) ou estão na lista negra (#77, #100)."""
    from curator.banco import consultas

    if escopo == "biblioteca":
        return consultas.utilizaveis(con)
    dislikes = consultas.bloqueadas(con)
    if escopo == CURTIDAS:  # playlists gerais: só as curtidas, sem olhar playlist nenhuma
        return [
            r[0]
            for r in con.execute("SELECT video_id FROM faixas WHERE curtida = 1 ORDER BY video_id")
            if r[0] not in dislikes
        ]
    return [
        r[0]
        for r in con.execute(
            """WITH RECURSIVE d(id) AS (SELECT ? UNION SELECT p.playlist_id FROM playlists p JOIN d ON p.mae_id = d.id
                                         WHERE p.ignorada = 0)
               SELECT DISTINCT video_id FROM playlist_faixas WHERE playlist_id IN (SELECT id FROM d) ORDER BY video_id""",
            (escopo,),
        )
        if r[0] not in dislikes
    ]


def calcular(
    con: sqlite3.Connection, escopo: str = "biblioteca", p: Parametros = Parametros()
) -> list[Cluster]:
    emb = espacos.embeddings(con)
    m = emb.subconjunto(_universo(con, escopo))
    minimo = max(2 * p.quantas if p.quantas else 2 * p.min_cluster_size, p.n_neighbors + 2)
    if len(m.ids) < minimo:
        raise PoucasFaixas(f"só {len(m.ids)} faixa(s) analisada(s) no escopo; o mínimo é {minimo}")
    if p.quantas:
        cs = masters(m, particionar_consenso(m.X, p), p)
    else:
        rotulos, probs = particionar(reduzir(m.X, p), p)
        cs = sobrepor(m, rotulos, probs, p)
    for c in cs:
        c.estatisticas = descrever(con, sorted(c.membros))
        c.nome = _nome_em_cache(con, c.hash_nucleo) or nome_automatico(c.estatisticas)
    return cs


def _nome_em_cache(con: sqlite3.Connection, hash_nucleo: str) -> str | None:
    """Mesmo núcleo = mesmo cluster: reaproveita o nome (inclusive um dado pela IA ou por você)."""
    linha = con.execute(
        "SELECT nome FROM clusters WHERE hash_nucleo = ? ORDER BY nome_origem = 'automatico', agrupamento_id DESC LIMIT 1",
        (hash_nucleo,),
    ).fetchone()
    return linha[0] if linha else None


def salvar(con: sqlite3.Connection, cs: list[Cluster], escopo: str, p: Parametros) -> int:
    with transacao(con):
        aid = con.execute(
            "INSERT INTO agrupamentos (escopo, parametros, hash_biblioteca, criado_em) VALUES (?, ?, ?, ?)",
            (escopo, json.dumps(asdict(p), sort_keys=True), espacos.hash_biblioteca(con), agora()),
        ).lastrowid
        for c in cs:
            con.execute(
                "INSERT INTO clusters (agrupamento_id, cluster_id, nome, hash_nucleo, centroide, limiar, estatisticas) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (aid, c.cluster_id, c.nome, c.hash_nucleo, vetores.para_blob(c.centroide), c.limiar,
                 json.dumps(c.estatisticas, ensure_ascii=False)),
            )  # fmt: skip
            nucleo = set(c.nucleo)
            con.executemany(
                "INSERT INTO cluster_membros (agrupamento_id, cluster_id, video_id, nucleo, similaridade) VALUES (?, ?, ?, ?, ?)",
                [(aid, c.cluster_id, v, int(v in nucleo), s) for v, s in sorted(c.membros.items())],
            )
    return aid


def ultimo(con: sqlite3.Connection, escopo: str = "biblioteca") -> int | None:
    linha = con.execute(
        "SELECT agrupamento_id FROM agrupamentos WHERE escopo = ? ORDER BY agrupamento_id DESC LIMIT 1",
        (escopo,),
    ).fetchone()
    return linha[0] if linha else None


def ultimo_geral(con: sqlite3.Connection) -> int | None:
    """O agrupamento que representa a biblioteca inteira: o das playlists gerais (#80), senão o antigo."""
    return ultimo(con, CURTIDAS) or ultimo(con, "biblioteca")


def carregar(con: sqlite3.Connection, agrupamento_id: int) -> list[Cluster]:
    cs = []
    for r in con.execute(
        "SELECT * FROM clusters WHERE agrupamento_id = ? ORDER BY cluster_id", (agrupamento_id,)
    ):
        membros = con.execute(
            "SELECT video_id, nucleo, similaridade FROM cluster_membros WHERE agrupamento_id = ? AND cluster_id = ? ORDER BY video_id",
            (agrupamento_id, r["cluster_id"]),
        ).fetchall()
        c = Cluster(
            r["cluster_id"], [m["video_id"] for m in membros if m["nucleo"]],
            np.frombuffer(r["centroide"], dtype="<f4").copy(), r["limiar"],
            {m["video_id"]: m["similaridade"] for m in membros}, r["nome"], json.loads(r["estatisticas"]),
        )  # fmt: skip
        cs.append(c)
    return cs


def atualizar(con: sqlite3.Connection, agrupamento_id: int) -> int:
    """Âncoras: encaixa nos clusters salvos as faixas analisadas depois, sem mexer nos membros antigos."""
    escopo = con.execute(
        "SELECT escopo FROM agrupamentos WHERE agrupamento_id = ?", (agrupamento_id,)
    ).fetchone()[0]
    emb = espacos.embeddings(con).subconjunto(_universo(con, escopo))
    # Faixas que ficaram sem cluster também são reavaliadas: com τ e centroides fixos, o resultado não muda.
    vistos = {
        r[0]
        for r in con.execute(
            "SELECT DISTINCT video_id FROM cluster_membros WHERE agrupamento_id = ?", (agrupamento_id,)
        )
    }
    novos = [v for v in emb.ids if v not in vistos]
    p = _parametros_salvos(con, agrupamento_id)
    if p.quantas:
        return _encaixar_masters(con, agrupamento_id, emb, novos, p)
    inseridos = 0
    with transacao(con):
        for c in carregar(con, agrupamento_id):
            for v in novos:
                s = float(emb.linha(v) @ c.centroide)
                if s >= c.limiar:
                    con.execute(
                        "INSERT OR IGNORE INTO cluster_membros (agrupamento_id, cluster_id, video_id, nucleo, similaridade) VALUES (?, ?, ?, 0, ?)",
                        (agrupamento_id, c.cluster_id, v, round(s, 6)),
                    )
                    inseridos += 1
    return inseridos


def _parametros_salvos(con: sqlite3.Connection, agrupamento_id: int) -> Parametros:
    """Os parâmetros gravados, com os padrões para os campos que não existiam quando o agrupamento foi salvo."""
    salvos = json.loads(
        con.execute(
            "SELECT parametros FROM agrupamentos WHERE agrupamento_id = ?", (agrupamento_id,)
        ).fetchone()[0]
    )
    campos = {k: v for k, v in salvos.items() if k in Parametros.__dataclass_fields__}
    if isinstance(campos.get("sementes"), list):
        campos["sementes"] = tuple(campos["sementes"])
    return Parametros(**campos)


def _encaixar_masters(con, agrupamento_id: int, emb: espacos.Matriz, novos: list[str], p: Parametros) -> int:
    """Faixa nova nas masters salvas: a mais próxima e, na fronteira, a segunda (a mesma regra da criação)."""
    cs = carregar(con, agrupamento_id)
    C = np.vstack([c.centroide for c in cs])
    limiares = np.array([c.limiar for c in cs])
    inseridos = 0
    with transacao(con):
        for v in novos:
            sims = C @ emb.linha(v)
            k = int(sims.argmax())
            for j in (k, segunda_master(sims, k, limiares, p.fronteira)):
                if j is not None:
                    con.execute(
                        "INSERT OR IGNORE INTO cluster_membros (agrupamento_id, cluster_id, video_id, nucleo, similaridade) VALUES (?, ?, ?, 0, ?)",
                        (agrupamento_id, cs[j].cluster_id, v, round(float(sims[j]), 6)),
                    )
                    inseridos += 1
    return inseridos


def nomes_unicos(cs: list[Cluster]) -> dict[int, str]:
    """Dois clusters com o mesmo nome virariam a mesma playlist num plano: numera os repetidos (o maior fica sem
    número)."""
    vistos: dict[str, int] = {}
    out = {}
    for c in sorted(cs, key=lambda c: (-len(c.membros), c.cluster_id)):
        n = vistos[c.nome] = vistos.get(c.nome, 0) + 1
        out[c.cluster_id] = c.nome if n == 1 else f"{c.nome} ({n})"
    return out


def renomear(
    con: sqlite3.Connection, agrupamento_id: int, cluster_id: int, nome: str, origem: str = "usuario"
) -> None:
    con.execute(
        "UPDATE clusters SET nome = ?, nome_origem = ? WHERE agrupamento_id = ? AND cluster_id = ?",
        (nome, origem, agrupamento_id, cluster_id),
    )


def obter(con: sqlite3.Connection, escopo: str = "biblioteca", p: Parametros = Parametros(),
          reagrupar: bool = False) -> tuple[int, list[Cluster], int]:  # fmt: skip
    """(agrupamento_id, clusters, faixas novas encaixadas): reaproveita o último agrupamento do escopo, salvo
    `reagrupar` ou parâmetros diferentes dos que ele usou."""
    aid = ultimo(con, escopo)
    if aid is not None and not reagrupar:
        reagrupar = _parametros_salvos(con, aid) != p
    if aid is None or reagrupar:
        cs = calcular(con, escopo, p)
        return salvar(con, cs, escopo, p), cs, 0
    novos = atualizar(con, aid)
    return aid, carregar(con, aid), novos
