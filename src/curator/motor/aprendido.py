"""Gênero-pai aprendido com os rótulos do usuário (#93, ADR-17).

A régua do ADR-14 é zero-shot: usa a taxonomia do Discogs e não sabe o que VOCÊ chama de cada gênero. No Discogs,
"J-pop" é etiqueta de mercado: kessoku band e BABYMETAL caem no Pop. Aqui, um modelo por gênero-pai aprende com os
seus rótulos (`rotulos_genero`, migração 020), que nunca vêm das playlists existentes.

- **Rótulos:**
  - os explícitos ("é Rock", "não é Rock"), da faixa ou do artista do gabarito;
  - os negativos implícitos: uma faixa marcada como de outro pai, e sem rótulo neste, conta como "não" (o
    gabarito por artista é exclusivo).
- **Atributos:** o embedding do EffNet (média, L2) e as 519 saídas do MAEST, cada bloco em z-score sobre a
  biblioteca (sem olhar os rótulos). Faixa sem MAEST fica com a média (zero).
- **Modelo:** regressão logística L2 (λ = `LAMBDA`), com as classes balanceadas e resolvida por IRLS na forma dual
  (há menos faixas rotuladas que atributos). É determinística: mesmos rótulos + mesma biblioteca → mesma afinidade.
  A versão é o hash dos rótulos.
- **Mínimo:** só tem modelo o pai com `MIN_POSITIVOS` positivos de `MIN_ARTISTAS` artistas e `MIN_NEGATIVOS`
  negativos. Os outros (trilha sonora, clássica…) ficam com a régua do ADR-14.
- **Escala:** a mesma ideia da régua: 1,0 = tão típica quanto a mediana do NÚCLEO aprendido, que são as faixas da
  biblioteca que o modelo põe no pai (p ≥ 0,5). Assim, os níveis 0,7 / 0,8 / 1,0 valem igual.
- **Portão (fora do que aprendeu):** o modelo só conhece os pais rotulados e, sem portão, decide entre eles até para o
  que não é nenhum (O Lago dos Cisnes ia para o Hip Hop). Por isso ele só julga a faixa que a régua do ADR-14 põe em
  primeiro lugar num pai que tem modelo. As outras (trilha sonora, clássica…) ficam com a régua do ADR-14 em todos
  os pais. Negativos de fundo tirados do núcleo dos pais sem rótulo foram testados e pioraram o Electronic (AUC
  0,966 → 0,908): trilha de jogo e eletrônica se parecem.
- **Guarda (longe do que aprendeu, #101):** o portão não basta: quase metade da biblioteca é trilha de jogo, que a v2
  põe no Electronic (um pai com modelo), e o modelo nunca viu nada parecido. Então ele só julga a faixa que se
  parece com faixas rotuladas: a média do cosseno com as `K_GUARDA` rotuladas mais próximas, no espaço do modelo,
  tem de chegar ao percentil `PERCENTIL_GUARDA` das próprias rotuladas (cada uma sem contar a si mesma). Fora
  disso, vale a régua do ADR-14. É a detecção fora da distribuição por vizinhos de Sun et al. (2022), usada como
  abstenção (classificação seletiva, Geifman & El-Yaniv, 2017).
- **Os rótulos vencem:** uma faixa marcada "sim" tem pelo menos 1,0, e uma marcada "não" fica com 0, com ou sem
  portão ou guarda.

Avaliação com a guarda (deixando o artista inteiro de fora; docs/avaliacao.md, #101): rap faixa a faixa com AUC 0,990
(régua v2: 0,946), Pop por artista 0,977 (v2: 0,890) e 80% do rock japonês ≥ 0,8 no Rock. Nas receitas, a régua
aprendida vale no Hip Hop e no Pop (`generos.REGUA_APRENDIDA`). Testados e deixados de fora:
- YAMNet e as tags do MSD como atributos a mais (dentro do ruído);
- a média com uma das réguas (devolve o rock japonês ao Pop).
"""

import hashlib
import json
import sqlite3
from dataclasses import dataclass

import numpy as np

from curator.banco import consultas
from curator.banco.conexao import agora
from curator.motor import espacos

LAMBDA = 20.0
C0 = 10.0  # coluna constante do intercepto: com ela grande, a penalidade quase não pesa no intercepto
MIN_POSITIVOS, MIN_NEGATIVOS, MIN_ARTISTAS = 20, 20, 3
MIN_NUCLEO = 10
DOBRAS = 10
K_GUARDA = 5
PERCENTIL_GUARDA = 5.0
ORIGENS = ("gabarito_artista", "gabarito_faixa", "cli", "mcp")


# ------------------------------------------------------------------ regressão logística


def ajustar(X: np.ndarray, y: np.ndarray, lam: float = LAMBDA, iters: int = 50) -> np.ndarray:
    """Logística L2 com classes balanceadas, por IRLS na forma dual (vale para n < d). Devolve beta (d + 1)."""
    n = len(y)
    w = np.where(y == 1, n / (2 * max(y.sum(), 1)), n / (2 * max((1 - y).sum(), 1)))
    Xa = np.hstack([X, np.full((n, 1), C0)])
    K = Xa @ Xa.T
    f = np.zeros(n)
    a = np.zeros(n)
    for _ in range(iters):
        p = 1 / (1 + np.exp(-f))
        s = np.maximum(w * p * (1 - p), 1e-10)
        z = f + w * (y - p) / s
        a = np.linalg.solve(s[:, None] * K + lam * np.eye(n), s * z)
        novo = K @ a
        parar = np.max(np.abs(novo - f)) < 1e-7
        f = novo
        if parar:
            break
    return Xa.T @ a


def prever(beta: np.ndarray, X: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-(np.hstack([X, np.full((len(X), 1), C0)]) @ beta)))


# ------------------------------------------------------------------ guarda (#101)


def _l2(X: np.ndarray) -> np.ndarray:
    return X / np.maximum(np.linalg.norm(X, axis=1, keepdims=True), 1e-12)


def vizinhanca(
    XL: np.ndarray, rotuladas: list[str], X: np.ndarray, ids: list[str], k: int = K_GUARDA
) -> np.ndarray:
    """Média do cosseno de cada linha de `X` com as `k` linhas mais próximas de `XL` (as duas já em L2), sem contar a
    própria faixa quando ela é rotulada. NaN se não há rotuladas suficientes."""
    out = np.full(len(ids), np.nan)
    if len(rotuladas) <= k or not len(ids):
        return out
    pos = {v: j for j, v in enumerate(rotuladas)}
    for i0 in range(0, len(ids), 2048):
        S = X[i0 : i0 + 2048] @ XL.T
        for i, v in enumerate(ids[i0 : i0 + 2048]):
            if v in pos:
                S[i, pos[v]] = -np.inf
        out[i0 : i0 + len(S)] = np.sort(S, axis=1)[:, -k:].mean(axis=1)
    return out


def corte_guarda(s_rotuladas: np.ndarray, percentil: float = PERCENTIL_GUARDA) -> float:
    """O limite da guarda: o percentil das próprias rotuladas (cada uma medida sem contar a si mesma)."""
    s = s_rotuladas[~np.isnan(s_rotuladas)]
    return float(np.percentile(s, percentil)) if len(s) else np.inf


# ------------------------------------------------------------------ rótulos


# Os rótulos de faixas na lista negra (#100) ficam guardados, mas não ensinam nada enquanto ela estiver ignorada.
_ROTULOS_VALIDOS = f"""SELECT video_id, pai, sim FROM rotulos_genero
                       WHERE video_id NOT IN ({consultas.IGNORADAS}) ORDER BY pai, video_id"""


def rotulos(con: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """pai → {video_id: 1/0}, com os negativos implícitos (marcada como de outro pai e sem rótulo neste)."""
    explicitos: dict[str, dict[str, int]] = {}
    for vid, pai, sim in con.execute(_ROTULOS_VALIDOS):
        explicitos.setdefault(pai, {})[vid] = int(sim)
    positivos = {p: {v for v, s in d.items() if s} for p, d in explicitos.items()}
    out = {}
    for pai, d in explicitos.items():
        r = dict(d)
        for outro, vs in positivos.items():
            if outro != pai:
                for v in vs:
                    r.setdefault(v, 0)
        out[pai] = r
    return out


def hash_rotulos(con: sqlite3.Connection) -> str:
    h = hashlib.sha256()
    for vid, pai, sim in con.execute(_ROTULOS_VALIDOS):
        h.update(f"{pai}\t{vid}\t{sim}\n".encode())
    return h.hexdigest()[:16]


def rotular(con: sqlite3.Connection, video_id: str, pai: str, sim: bool | None, origem: str) -> None:
    """Grava (ou apaga, com `sim=None`) o rótulo da faixa. Um rótulo da faixa substitui o que veio do artista."""
    assert origem in ORIGENS
    if sim is None:
        con.execute("DELETE FROM rotulos_genero WHERE video_id = ? AND pai = ?", (video_id, pai))
        return
    con.execute(
        "INSERT INTO rotulos_genero (video_id, pai, sim, origem, criado_em) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (video_id, pai) DO UPDATE SET sim = excluded.sim, origem = excluded.origem, "
        "criado_em = excluded.criado_em",
        (video_id, pai, int(sim), origem, agora()),
    )


def importar_gabarito(con: sqlite3.Connection, gabarito: dict) -> dict[str, int]:
    """O gabarito pessoal (TOML da #83): `[artistas] "Rock" = [...]` vira um rótulo "sim" em cada faixa do artista
    principal; `[faixas."Hip Hop"] sim/nao` vira rótulo da faixa. Um rótulo da faixa nunca é trocado pelo do artista."""
    principal: dict[str, list[str]] = {}
    for vid, artistas in con.execute("SELECT video_id, artistas FROM faixas ORDER BY video_id"):
        nome = (json.loads(artistas) or [{"nome": ""}])[0]["nome"]
        principal.setdefault(nome, []).append(vid)
    cont = {"artista": 0, "faixa": 0, "faixa_desconhecida": 0}
    for pai, nomes in (gabarito.get("artistas") or {}).items():
        for nome in nomes:
            for vid in principal.get(nome, []):
                cur = con.execute(
                    "INSERT INTO rotulos_genero (video_id, pai, sim, origem, criado_em) "
                    "VALUES (?, ?, 1, 'gabarito_artista', ?) ON CONFLICT (video_id, pai) DO UPDATE SET sim = 1 "
                    "WHERE rotulos_genero.origem = 'gabarito_artista'",
                    (vid, pai, agora()),
                )
                cont["artista"] += cur.rowcount
    existentes = {r[0] for r in con.execute("SELECT video_id FROM faixas")}
    for pai, grupos in (gabarito.get("faixas") or {}).items():
        for chave, sim in (("sim", True), ("nao", False)):
            for vid in grupos.get(chave, []):
                if vid not in existentes:
                    cont["faixa_desconhecida"] += 1
                    continue
                rotular(con, vid, pai, sim, "gabarito_faixa")
                cont["faixa"] += 1
    return cont


# ------------------------------------------------------------------ modelos


@dataclass(frozen=True)
class Modelo:
    pai: str
    beta: np.ndarray
    referencia: float  # o p que vale 1,0: a mediana do núcleo aprendido
    positivos: int
    negativos: int
    artistas: int
    nucleo: int


class Aprendido:
    """Um modelo por gênero-pai com rótulos suficientes, sobre a biblioteca analisada deste banco."""

    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self.versao = hash_rotulos(con)
        base = espacos.da_biblioteca(con)
        self._e = espacos.embeddings(con, "effnet_emb")
        self._m = espacos.probabilidades(con, "maest519")
        self._norma = {}
        for nome, mat in (("e", self._e), ("m", self._m)):
            linhas = [mat.indice[v] for v in base if v in mat.indice]
            X = (
                mat.X[linhas].astype(np.float64)
                if linhas
                else np.zeros((0, mat.X.shape[1] if mat.X.ndim == 2 else 0))
            )
            self._norma[nome] = (X.mean(0), X.std(0) + 1e-6) if len(X) else None
        self._rotulos = rotulos(con)
        artista = self._artistas(con)
        self.modelos: dict[str, Modelo] = {}
        self.insuficientes: dict[str, dict[str, int]] = {}
        X_base, tem_base = self.atributos(base)
        for pai, rot in sorted(self._rotulos.items()):
            vids = [v for v in sorted(rot) if v in self._e.indice]
            y = np.array([rot[v] for v in vids], np.float64)
            n_art = len({artista.get(v) for v, s in zip(vids, y, strict=True) if s == 1})
            pos, neg = int(y.sum()), int(len(y) - y.sum())
            if pos < MIN_POSITIVOS or neg < MIN_NEGATIVOS or n_art < MIN_ARTISTAS:
                self.insuficientes[pai] = {"positivos": pos, "negativos": neg, "artistas": n_art}
                continue
            X, _ = self.atributos(vids)
            beta = ajustar(X, y)
            p_base = prever(beta, X_base[tem_base]) if tem_base.any() else np.zeros(0)
            nucleo = p_base[p_base >= 0.5]
            ref = float(np.median(nucleo)) if len(nucleo) >= MIN_NUCLEO else 0.5
            self.modelos[pai] = Modelo(pai, beta, max(ref, 0.5), pos, neg, n_art, len(nucleo))
        # a guarda: todas as faixas rotuladas, de qualquer pai, são o "conhecido"
        self._rotuladas = sorted({v for d in self._rotulos.values() for v in d if v in self._e.indice})
        self._XL = _l2(self.atributos(self._rotuladas)[0])
        self.corte_guarda = corte_guarda(vizinhanca(self._XL, self._rotuladas, self._XL, self._rotuladas))

    @staticmethod
    def _artistas(con: sqlite3.Connection) -> dict[str, str]:
        out = {}
        for vid, artistas in con.execute("SELECT video_id, artistas FROM faixas"):
            a = json.loads(artistas)
            out[vid] = (a[0].get("id") or a[0].get("nome")) if a else vid
        return out

    def atributos(self, ids: list[str]) -> tuple[np.ndarray, np.ndarray]:
        """(X em z-score, tem EffNet) alinhados a `ids`. Sem MAEST, o bloco dele fica na média (zero)."""
        partes = []
        tem = np.array([v in self._e.indice for v in ids], bool)
        for nome, mat in (("e", self._e), ("m", self._m)):
            d = mat.X.shape[1] if len(mat.ids) else 0
            X = np.zeros((len(ids), d))
            if d and self._norma[nome] is not None:
                mu, sd = self._norma[nome]
                for i, v in enumerate(ids):
                    if v in mat.indice:
                        X[i] = (mat.X[mat.indice[v]] - mu) / sd
            partes.append(X)
        return np.hstack(partes), tem

    def proximidade(self, ids: list[str]) -> np.ndarray:
        """A nota da guarda: média do cosseno com as `K_GUARDA` faixas rotuladas mais próximas. NaN sem EffNet."""
        out = np.full(len(ids), np.nan)
        X, tem = self.atributos(ids)
        if tem.any():
            out[tem] = vizinhanca(
                self._XL, self._rotuladas, _l2(X[tem]), [v for v, t in zip(ids, tem, strict=True) if t]
            )
        return out

    def dentro_da_guarda(self, ids: list[str]) -> np.ndarray:
        """Faixas parecidas o bastante com as rotuladas para o modelo julgar (NaN conta como fora)."""
        s = self.proximidade(ids)
        return np.where(np.isnan(s), False, s >= self.corte_guarda)

    def prob(self, pai: str, ids: list[str]) -> np.ndarray:
        """Probabilidade do modelo do pai; NaN sem modelo ou sem EffNet."""
        out = np.full(len(ids), np.nan)
        m = self.modelos.get(pai)
        if m is None or not ids:
            return out
        X, tem = self.atributos(ids)
        if tem.any():
            out[tem] = prever(m.beta, X[tem])
        return out

    def relativa(self, pai: str, ids: list[str]) -> np.ndarray:
        """p ÷ referência do núcleo aprendido (sem portão nem rótulos; ver `generos.relativa_receita`). NaN sem
        modelo ou sem EffNet."""
        m = self.modelos.get(pai)
        return self.prob(pai, ids) / (m.referencia if m else 1.0)

    def sobrepor_rotulos(self, pai: str, ids: list[str], rel: np.ndarray) -> np.ndarray:
        """Os rótulos por cima de qualquer régua: "sim" ≥ 1,0, "não" = 0."""
        rot = self._rotulos.get(pai, {})
        out = rel.astype(np.float64).copy()
        for i, v in enumerate(ids):
            if v in rot:
                out[i] = max(0.0 if np.isnan(out[i]) else out[i], 1.0) if rot[v] else 0.0
        return out

    def rotulo(self, pai: str, video_id: str) -> int | None:
        return self._rotulos.get(pai, {}).get(video_id)


_cache: dict[tuple, Aprendido] = {}


def obter(con: sqlite3.Connection) -> Aprendido:
    """O Aprendido deste banco, refeito quando mudam os rótulos, as análises, o MAEST ou as faixas fora da base (um
    dislike ou uma faixa ignorada muda o z-score e o núcleo)."""
    chave = (str(con.execute("PRAGMA database_list").fetchone()[2]), espacos.hash_biblioteca(con),
             espacos._versao_extra(con, "maest519"), hash_rotulos(con), consultas.hash_bloqueadas(con))  # fmt: skip
    if chave not in _cache:
        _cache.clear()
        _cache[chave] = Aprendido(con)
    return _cache[chave]


# ------------------------------------------------------------------ validação


def fora_da_amostra(con: sqlite3.Connection, pai: str, dobras: int = DOBRAS) -> dict | None:
    """As notas de cada faixa rotulada do pai deixando o artista INTEIRO de fora (dobras por artista): o modelo da
    dobra nunca viu uma faixa do artista que está prevendo, e a guarda da dobra só conhece as rotuladas dos outros
    artistas. None se o pai não tem modelo.

    - `aprendida`: só o modelo (p ÷ referência do núcleo);
    - `com_guarda`: o que a receita veria com a régua aprendida: o modelo nas faixas que passam pelo portão e pela
      guarda, a v2 nas outras (sem o rótulo da própria faixa, que é o que se está prevendo);
    - `dentro`: a faixa passou pela guarda da dobra."""
    from curator.motor import generos

    afin = generos.Afinidades(con)
    ap = afin.aprendido
    if pai not in ap.modelos:
        return None
    rot = ap._rotulos[pai]
    artista = Aprendido._artistas(con)
    vids = [v for v in sorted(rot) if v in ap._e.indice]
    y = np.array([rot[v] for v in vids], np.float64)
    grupos = sorted({artista.get(v, v) for v in vids})
    dobra = {g: i % dobras for i, g in enumerate(grupos)}
    X, _ = ap.atributos(vids)
    Xn = _l2(X)
    X_base, tem_base = ap.atributos(espacos.da_biblioteca(con))
    art_l = np.array([artista.get(v, v) for v in ap._rotuladas], object)
    s = np.zeros(len(vids))
    dentro = np.zeros(len(vids), bool)
    for k in range(dobras):
        te = np.array([dobra[artista.get(v, v)] == k for v in vids])
        if not te.any() or len(set(y[~te])) < 2:
            continue
        beta = ajustar(X[~te], y[~te])
        p_base = prever(beta, X_base[tem_base])
        nucleo = p_base[p_base >= 0.5]
        ref = max(float(np.median(nucleo)) if len(nucleo) >= MIN_NUCLEO else 0.5, 0.5)
        s[te] = prever(beta, X[te]) / ref
        fora = {g for g in grupos if dobra[g] == k}
        conhecidas = np.array([a not in fora for a in art_l], bool)
        XL, L = ap._XL[conhecidas], [v for v, c in zip(ap._rotuladas, conhecidas, strict=True) if c]
        corte = corte_guarda(vizinhanca(XL, L, XL, L))
        prox = vizinhanca(XL, L, Xn[te], [v for v, t in zip(vids, te, strict=True) if t])
        dentro[te] = np.where(np.isnan(prox), False, prox >= corte)
    v2 = afin.relativa(pai, vids)
    return {"vids": vids, "y": y, "artistas": len(grupos), "aprendida": s, "v2": v2, "dentro": dentro,
            "com_guarda": np.where(dentro & ~afin.fora_do_aprendido(vids), s, v2)}  # fmt: skip


def _medidas(s: np.ndarray, y: np.ndarray) -> dict:
    pos, neg = s[y == 1], s[y == 0]
    return {
        "auc": _auc(pos, neg),
        "revocacao_0_8": float(np.mean(pos >= 0.8)) if len(pos) else None,
        "falsos_positivos_0_8": float(np.mean(neg >= 0.8)) if len(neg) else None,
    }


def validar(con: sqlite3.Connection, pai: str, dobras: int = DOBRAS) -> dict | None:
    """AUC e revocação/falsos positivos no 0,8, deixando o artista inteiro de fora: o modelo sozinho (chaves de cima),
    `com_guarda` (o que a receita veria) e a `v2`. None se o pai não tem rótulos suficientes."""
    r = fora_da_amostra(con, pai, dobras)
    if r is None:
        return None
    y = r["y"]
    return {
        "pai": pai, "positivos": int(y.sum()), "negativos": int(len(y) - y.sum()), "artistas": r["artistas"],
        **_medidas(r["aprendida"], y), "com_guarda": _medidas(r["com_guarda"], y), "v2": _medidas(r["v2"], y),
        "julgadas_pela_guarda": float(r["dentro"].mean()),
    }  # fmt: skip


def _auc(pos: np.ndarray, neg: np.ndarray) -> float | None:
    """Chance de um positivo ficar acima de um negativo (empate conta meio); sem scipy."""
    if not len(pos) or not len(neg):
        return None
    maior = (pos[:, None] > neg[None, :]).sum() + 0.5 * (pos[:, None] == neg[None, :]).sum()
    return float(maior / (len(pos) * len(neg)))


# ------------------------------------------------------------------ aprendizado ativo


CORTE = 0.8  # o nível "normal" do genero_pai_min


def duvidosas(con: sqlite3.Connection, pai: str, n: int = 10, max_por_artista: int = 2) -> list[dict]:
    """As faixas sem rótulo em que um rótulo mais ensina (aprendizado ativo por incerteza; Settles, 2009).

    Primeiro as que as réguas (v2, com o MAEST, aprendida) põem de lados opostos do corte 0,8, da maior discordância
    para a menor. Depois as mais perto do corte na régua da receita. No máximo `max_por_artista` faixas por artista,
    para um rótulo não ensinar só o som de um artista, e sem repetir a mesma música (outro upload, mesmo título).
    Desempate por video_id: a lista é reprodutível."""
    from curator.motor import generos

    afin = generos.Afinidades(con)
    if pai not in afin.pais:
        raise KeyError(pai)
    ap = afin.aprendido
    ja = ap._rotulos.get(pai, {})
    ids = [v for v in espacos.da_biblioteca(con) if v in afin.d.indice and v not in ja]
    v2, cm = afin.relativa(pai, ids), afin.relativa_com_maest(pai, ids)
    apr = afin.relativa_aprendida(pai, ids)
    chaves = []
    for i, v in enumerate(ids):
        vals = [v2[i], cm[i]] + ([apr[i]] if not np.isnan(apr[i]) else [])
        alto, baixo = max(vals), min(vals)
        receita = apr[i] if not np.isnan(apr[i]) else v2[i]
        if alto >= CORTE > baixo:
            chaves.append(((0, -(alto - baixo), v), i, "as réguas discordam"))
        else:
            chaves.append(((1, abs(receita - CORTE), v), i, "perto do corte"))
    from curator.texto import normalizar

    artista = Aprendido._artistas(con)
    titulo = dict(con.execute("SELECT video_id, titulo FROM faixas").fetchall())
    por_artista: dict[str, int] = {}
    vistos: set[str] = set()
    out = []
    for _, i, motivo in sorted(chaves):
        a, t = artista.get(ids[i], ids[i]), normalizar(titulo.get(ids[i]) or "").split(" - ")[0]
        if por_artista.get(a, 0) >= max_por_artista or (t and t in vistos):
            continue
        por_artista[a] = por_artista.get(a, 0) + 1
        vistos.add(t)
        out.append({
            "video_id": ids[i], "motivo": motivo, "afinidade_v2": round(float(v2[i]), 3),
            "afinidade_com_maest": round(float(cm[i]), 3),
            **({"afinidade_aprendida": round(float(apr[i]), 3)} if not np.isnan(apr[i]) else {}),
        })  # fmt: skip
        if len(out) >= n:
            break
    return out


def resumo(con: sqlite3.Connection) -> list[dict]:
    """Por pai rotulado: quantos rótulos, se já tem modelo e o tamanho do núcleo aprendido."""
    ap = obter(con)
    out = []
    for pai in sorted(set(ap.modelos) | set(ap.insuficientes)):
        explicitos = dict(con.execute(
            "SELECT sim, count(*) FROM rotulos_genero WHERE pai = ? GROUP BY sim", (pai,)
        ).fetchall())  # fmt: skip
        m = ap.modelos.get(pai)
        out.append({
            "pai": pai, "sim": explicitos.get(1, 0), "nao": explicitos.get(0, 0),
            **({"modelo": True, "positivos": m.positivos, "negativos": m.negativos, "artistas": m.artistas,
                "nucleo_aprendido": m.nucleo} if m else {"modelo": False, **ap.insuficientes[pai]}),
        })  # fmt: skip
    return out
