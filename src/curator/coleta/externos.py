"""Metadados externos (#88, item 5): gêneros, ISRC, BPM, popularidade e datas do Deezer e do MusicBrainz.

Por quê: o áudio não separa bem o rap "com cara de pop" nem o Pop do Electronic (ADR-14). Para propagar gênero,
Kim, Tomasik & Turnbull (ISMIR 2009) acharam as tags sociais e a filtragem colaborativa melhores que o áudio. Os
gêneros do MusicBrainz são votos de pessoas, por faixa, álbum e artista. Se essas fontes valem o que custam está
em aberto na #91: elas ficam enquanto isso não for medido.

Por faixa da biblioteca (título, artistas e duração do YT Music):
1. **Deezer** (sem chave): busca "artista título" e casa pelas regras do ADR-08 (título, artista, duração ±3 s,
   sem variantes como remix/cover). Da faixa vêm ISRC, BPM, ganho, rank de popularidade, data e explícita; do
   álbum, gêneros, gravadora e tipo.
2. **MusicBrainz** (sem chave, 1 pedido/s, User-Agent com contato): pelo ISRC do Deezer ou, sem ele, por busca
   com o mesmo casamento. Traz gêneros e tags da gravação, do artista (mais tipo, país e início) e do release
   group (mais a primeira data).

O Last.fm ficou de fora de propósito: pede chave de API (cadastro e login) para pouco além das tags que o
MusicBrainz já traz.

Cache: casamento e dados ficam em `externos_faixas`. `ok` e `sem-casamento` são refeitos depois de `ttl_dias`.
Erro de rede vira `erro`, com nova tentativa espaçada (1 h, 2 h, 4 h… até 7 dias), e nunca vira cache. Artistas
e álbuns, que várias faixas dividem, ficam em `externos_entidades`. `externos_generos` junta os rótulos com peso,
por fonte e nível (faixa, álbum, artista).
"""

import json
import logging
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from curator.audio.casamento import score as score_casamento
from curator.audio.casamento import variantes
from curator.audio.provedores.base import FaixaAlvo
from curator.banco.conexao import agora, transacao
from curator.banco.consultas import BIBLIOTECA

log = logging.getLogger(__name__)

FONTES = ("deezer", "musicbrainz")
MAX_ESPERA = timedelta(days=7)


class ErroExterno(RuntimeError):
    """Rede, limite de pedidos ou resposta inválida: tentar a faixa mais tarde."""


class ClienteHTTP:
    """GET JSON com pausa mínima entre pedidos e novas tentativas em 429/503 (limite de taxa)."""

    def __init__(self, base: str, intervalo_s: float, user_agent: str, abrir=None, dormir=time.sleep):
        self.base, self.intervalo_s, self.user_agent = base, intervalo_s, user_agent
        self._abrir, self._dormir = abrir or urllib.request.urlopen, dormir
        self._ultima = 0.0

    def get(self, caminho: str, tentativas: int = 4, **params) -> dict | None:
        """None quando o recurso não existe (404)."""
        url = f"{self.base}{caminho}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        req = urllib.request.Request(
            url, headers={"User-Agent": self.user_agent, "Accept": "application/json"}
        )
        for n in range(tentativas):
            espera = self.intervalo_s - (time.monotonic() - self._ultima)
            if espera > 0:
                self._dormir(espera)
            try:
                with self._abrir(req, timeout=20) as r:
                    dados = json.load(r)
                return dados
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code not in (429, 500, 502, 503, 504) or n == tentativas - 1:
                    raise ErroExterno(f"{caminho}: HTTP {e.code}") from e
                self._dormir(2 ** (n + 1))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                if n == tentativas - 1:
                    raise ErroExterno(f"{caminho}: {e}") from e
                self._dormir(2 ** (n + 1))
            finally:
                self._ultima = time.monotonic()
        return None


@dataclass
class Busca:
    """O que uma fonte achou para uma faixa."""

    status: str  # ok | sem-casamento
    ref: str | None = None
    score: float | None = None
    dados: dict = field(default_factory=dict)
    generos: list[tuple[str, str, float]] = field(default_factory=list)  # (nível, rótulo, peso)


# ------------------------------------------------------------------ cache de artistas e álbuns


def entidade(con, fonte: str, tipo: str, ref, ttl_dias: float, buscar: Callable[[], dict | None]) -> dict:
    linha = con.execute(
        "SELECT dados, buscado_em FROM externos_entidades WHERE fonte = ? AND tipo = ? AND ref = ?",
        (fonte, tipo, str(ref)),
    ).fetchone()
    if linha and datetime.fromisoformat(linha[1]) > datetime.now(UTC) - timedelta(days=ttl_dias):
        return json.loads(linha[0])
    dados = buscar() or {}
    con.execute(
        "INSERT OR REPLACE INTO externos_entidades (fonte, tipo, ref, nome, dados, buscado_em) VALUES (?, ?, ?, ?, ?, ?)",
        (
            fonte,
            tipo,
            str(ref),
            dados.get("name") or dados.get("title"),
            json.dumps(dados, ensure_ascii=False),
            agora(),
        ),
    )
    return dados


def _contagens(itens) -> list[dict]:
    """[{nome, n}] dos gêneros/tags do MusicBrainz, do mais votado para o menos."""
    out = [{"nome": i["name"], "n": int(i.get("count") or 0)} for i in itens or [] if i.get("name")]
    return sorted(out, key=lambda x: (-x["n"], x["nome"]))


# ------------------------------------------------------------------ casamento

_PARENTESES = re.compile(r"\s*[(\[【].*?[)\]】]")


def titulos(t: str) -> list[str]:
    """O título e as formas que os catálogos costumam usar: sem parênteses (feat., "Original Soundtrack") e cada lado
    de "original - tradução" (o YT Music traz "秒針少女 - Chronostasis"; o Deezer, só "秒針少女")."""
    out = [t, _PARENTESES.sub("", t).strip()]
    if " - " in t:
        out += [_PARENTESES.sub("", x).strip() for x in t.split(" - ")]
    return list(dict.fromkeys(x for x in out if x))


def casar(
    f: FaixaAlvo, titulo: str, artista: str | None, duracao: float | None, tolerancia_s: float = 3
) -> float:
    """Casamento para METADADOS: o do ADR-08 (variantes como remix, cover e ao vivo continuam recusadas), mais tolerante.

    Compara também as formas de `titulos`. Uma duração diferente (outra versão da mesma música) não zera: vale o
    título e o artista, com desconto. Serve para gênero; o BPM e o ISRC dessa versão podem não ser os da faixa."""
    if variantes(titulo) - variantes(f.titulo):
        return 0.0
    melhor = 0.0
    for alvo in titulos(f.titulo):
        for cand in titulos(titulo):
            s = score_casamento(
                FaixaAlvo(f.video_id, alvo, f.artistas, f.duracao_s), cand, artista, duracao, tolerancia_s
            )
            if s == 0.0 and duracao and f.duracao_s:
                s = 0.95 * score_casamento(FaixaAlvo(f.video_id, alvo, f.artistas, None), cand, artista, None)
            melhor = max(melhor, s)
    return round(melhor, 4)


def _duracao_confere(f: FaixaAlvo, duracao: float | None, tolerancia_s: float) -> bool | None:
    return None if not (duracao and f.duracao_s) else abs(duracao - f.duracao_s) <= 3 * tolerancia_s


# ------------------------------------------------------------------ Deezer


def deezer(con, dz, f: FaixaAlvo, tolerancia_s: float, limiar: float, ttl_dias: float) -> Busca:
    """Uma busca por forma do título (no máximo 3), parando na primeira que casa."""
    from curator.texto import normalizar_titulo

    melhor, melhor_s = None, 0.0
    for t in titulos(f.titulo)[:3]:
        consulta = f"{' '.join(f.artistas[:1])} {normalizar_titulo(t) or t}".strip()
        for c in (dz.get("/search/track", q=consulta, limit=15) or {}).get("data") or []:
            s = casar(
                f, c.get("title") or "", (c.get("artist") or {}).get("name"), c.get("duration"), tolerancia_s
            )
            if s > melhor_s:
                melhor, melhor_s = c, s
        if melhor_s >= limiar:
            break
    if melhor is None or melhor_s < limiar:
        return Busca("sem-casamento", score=round(melhor_s, 4))
    t = dz.get(f"/track/{melhor['id']}") or melhor
    album_id = (t.get("album") or {}).get("id")
    album = (
        entidade(con, "deezer", "album", album_id, ttl_dias, lambda: dz.get(f"/album/{album_id}"))
        if album_id
        else {}
    )
    generos_album = [g["name"] for g in (album.get("genres") or {}).get("data") or [] if g.get("name")]
    dados = {
        "id": t.get("id"), "titulo": t.get("title"), "isrc": t.get("isrc"), "bpm": t.get("bpm") or None,
        "ganho": t.get("gain"), "rank": t.get("rank"), "data": t.get("release_date"),
        "explicita": t.get("explicit_lyrics"), "duracao": t.get("duration"),
        "duracao_confere": _duracao_confere(f, t.get("duration"), tolerancia_s),
        "artistas": [{"id": a.get("id"), "nome": a.get("name"), "papel": a.get("role")} for a in t.get("contributors") or []],
        "album": {"id": album_id, "titulo": album.get("title"), "generos": generos_album, "gravadora": album.get("label"),
                  "tipo": album.get("record_type"), "data": album.get("release_date"), "fas": album.get("fans")},
    }  # fmt: skip
    return Busca(
        "ok", str(t.get("id")), round(melhor_s, 4), dados, [("album", g, 1.0) for g in generos_album]
    )


# ------------------------------------------------------------------ MusicBrainz


def _mb_creditos(r: dict) -> list[dict]:
    return [
        {
            "id": (c.get("artist") or {}).get("id"),
            "nome": (c.get("artist") or {}).get("name") or c.get("name"),
        }
        for c in r.get("artist-credit") or []
        if isinstance(c, dict)
    ]


def _mb_score(f: FaixaAlvo, c: dict) -> float:
    nomes = " ".join(a["nome"] or "" for a in _mb_creditos(c))
    return casar(f, c.get("title") or "", nomes, (c.get("length") or 0) / 1000 or None, 3)


def _mb_candidatos(mb, f: FaixaAlvo, isrc: str | None) -> list[tuple[dict, float]]:
    """(gravação, score) pelo ISRC (score 1,0 se o casamento confere) ou pela busca."""
    if isrc and (r := mb.get(f"/isrc/{isrc}", inc="artist-credits", fmt="json")):
        cands = r.get("recordings") or []
        por_isrc = True
    else:
        artista = (f.artistas[0] if f.artistas else "").replace('"', "")
        cands, por_isrc = [], False
        for t in titulos(f.titulo)[:2]:
            q = f'recording:"{t.replace(chr(34), "")}"' + (f' AND artist:"{artista}"' if artista else "")
            achados = (mb.get("/recording", query=q, fmt="json", limit=10) or {}).get("recordings") or []
            cands += achados
            if any(_mb_score(f, c) >= 0.82 for c in achados):
                break
    out = []
    for c in cands:
        s = _mb_score(f, c)
        # o ISRC identifica a gravação: basta o título/artista não contradizer (a duração do MB às vezes falta)
        out.append((c, 1.0 if por_isrc and s >= 0.6 else s))
    return sorted(out, key=lambda x: -x[1])


def musicbrainz(con, mb, f: FaixaAlvo, isrc: str | None, limiar: float, ttl_dias: float) -> Busca:
    cands = _mb_candidatos(mb, f, isrc)
    if not cands or cands[0][1] < limiar:
        return Busca("sem-casamento", score=round(cands[0][1], 4) if cands else 0.0)
    rec_id, s = cands[0][0]["id"], cands[0][1]
    r = (
        mb.get(
            f"/recording/{rec_id}", inc="genres+tags+artist-credits+releases+release-groups+isrcs", fmt="json"
        )
        or {}
    )
    artistas = _mb_creditos(r)
    lancamentos = [x for x in r.get("releases") or [] if x.get("release-group")]
    oficiais = [x for x in lancamentos if x.get("status") == "Official"] or lancamentos
    # o álbum (release group) mais antigo entre os oficiais; álbum de estúdio antes de single/coletânea
    ordem = {"Album": 0, "EP": 1, "Single": 2}
    oficiais.sort(
        key=lambda x: (ordem.get(x["release-group"].get("primary-type"), 3), x.get("date") or "9999")
    )
    rg_id = oficiais[0]["release-group"]["id"] if oficiais else None
    inc = {"inc": "genres+tags", "fmt": "json"}
    rg = (
        entidade(
            con,
            "musicbrainz",
            "release-group",
            rg_id,
            ttl_dias,
            lambda: mb.get(f"/release-group/{rg_id}", **inc),
        )
        if rg_id
        else {}
    )
    art_id = artistas[0]["id"] if artistas else None
    art = (
        entidade(con, "musicbrainz", "artista", art_id, ttl_dias, lambda: mb.get(f"/artist/{art_id}", **inc))
        if art_id
        else {}
    )
    dados = {
        "id": rec_id, "titulo": r.get("title"), "duracao": (r.get("length") or 0) / 1000 or None,
        "primeira_data": r.get("first-release-date"), "isrcs": r.get("isrcs") or [], "artistas": artistas,
        "generos": _contagens(r.get("genres")), "tags": _contagens(r.get("tags")),
        "release_group": {"id": rg_id, "titulo": rg.get("title"), "tipo": rg.get("primary-type"),
                          "tipos_secundarios": rg.get("secondary-types") or [], "data": rg.get("first-release-date"),
                          "generos": _contagens(rg.get("genres")), "tags": _contagens(rg.get("tags"))},
        "artista": {"id": art_id, "nome": art.get("name"), "tipo": art.get("type"), "pais": art.get("country"),
                    "inicio": (art.get("life-span") or {}).get("begin"), "generos": _contagens(art.get("genres")),
                    "tags": _contagens(art.get("tags"))},
    }  # fmt: skip
    generos = (
        [("faixa", g["nome"], g["n"]) for g in dados["generos"]]
        + [("album", g["nome"], g["n"]) for g in dados["release_group"]["generos"]]
        + [("artista", g["nome"], g["n"]) for g in dados["artista"]["generos"]]
    )
    return Busca("ok", rec_id, round(s, 4), dados, generos)


# ------------------------------------------------------------------ banco e fila


def _faixa(con, vid: str) -> FaixaAlvo:
    r = con.execute(
        "SELECT titulo, artistas, duracao_s, album_nome FROM faixas WHERE video_id = ?", (vid,)
    ).fetchone()
    return FaixaAlvo(vid, r[0], [a["nome"] for a in json.loads(r[1] or "[]")], r[2], r[3])


def gravar(con, vid: str, fonte: str, b: Busca) -> None:
    with transacao(con):
        con.execute(
            """INSERT OR REPLACE INTO externos_faixas
                 (video_id, fonte, ref, score, dados, status, erro, tentativas, proxima_em, buscado_em)
               VALUES (?, ?, ?, ?, ?, ?, NULL, 0, NULL, ?)""",
            (
                vid,
                fonte,
                b.ref,
                b.score,
                json.dumps(b.dados, ensure_ascii=False) if b.dados else None,
                b.status,
                agora(),
            ),
        )
        con.execute("DELETE FROM externos_generos WHERE video_id = ? AND fonte = ?", (vid, fonte))
        pesos: Counter = Counter()
        for nivel, rotulo, peso in b.generos:
            pesos[(nivel, rotulo.strip().casefold())] += peso
        con.executemany(
            "INSERT INTO externos_generos (video_id, fonte, nivel, rotulo, peso) VALUES (?, ?, ?, ?, ?)",
            [(vid, fonte, n, r, p) for (n, r), p in pesos.items() if r],
        )


def falhar(con, vid: str, fonte: str, erro: str, agora_dt: datetime) -> None:
    """Erro não é cache: a faixa volta depois de 1 h, 2 h, 4 h… (no máximo 7 dias)."""
    n = (
        con.execute(
            "SELECT tentativas FROM externos_faixas WHERE video_id = ? AND fonte = ?", (vid, fonte)
        ).fetchone()
        or [0]
    )[0] + 1
    proxima = agora_dt + min(timedelta(hours=2 ** (n - 1)), MAX_ESPERA)
    con.execute(
        """INSERT INTO externos_faixas (video_id, fonte, status, erro, tentativas, proxima_em, buscado_em)
           VALUES (?, ?, 'erro', ?, ?, ?, ?)
           ON CONFLICT (video_id, fonte) DO UPDATE SET status = 'erro', erro = excluded.erro,
             tentativas = excluded.tentativas, proxima_em = excluded.proxima_em, buscado_em = excluded.buscado_em,
             importacao_id = NULL""",  # refeita aqui: deixa de ser da importação (#96)
        (vid, fonte, erro[:500], n, proxima.strftime("%Y-%m-%dT%H:%M:%SZ"), agora()),
    )


def pendentes(
    con,
    fonte: str,
    ttl_dias: float,
    agora_dt: datetime,
    forcar: bool = False,
    repetir_sem_casamento: bool = False,
) -> list[str]:
    """Faixas da biblioteca sem dado desta fonte, com dado vencido ou com erro já liberado para nova tentativa.

    `repetir_sem_casamento`: também as que não casaram, antes do ttl (ex.: depois de melhorar o casamento)."""
    limite = (agora_dt - timedelta(days=ttl_dias)).strftime("%Y-%m-%dT%H:%M:%SZ")
    momento = agora_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return [
        r[0]
        for r in con.execute(
            f"""SELECT b.video_id FROM ({BIBLIOTECA}) b JOIN faixas f USING (video_id)
                LEFT JOIN externos_faixas e ON e.video_id = b.video_id AND e.fonte = :fonte
                WHERE :forcar OR e.video_id IS NULL
                   OR (e.status IN ('ok', 'sem-casamento') AND e.buscado_em < :limite)
                   OR (e.status = 'erro' AND coalesce(e.proxima_em, '') <= :agora)
                   OR (:repetir AND e.status = 'sem-casamento')
                ORDER BY e.video_id IS NOT NULL, f.posicao_curtida IS NULL, f.posicao_curtida, b.video_id""",
            {
                "fonte": fonte,
                "limite": limite,
                "agora": momento,
                "forcar": int(forcar),
                "repetir": int(repetir_sem_casamento),
            },
        )
    ]


def user_agent(cfg: dict) -> str:
    from curator import __version__

    contato = cfg["externos"]["contato"]
    return f"yt-music-curator/{__version__}" + (f" ( {contato} )" if contato else "")


def clientes(cfg: dict, abrir=None, dormir=time.sleep) -> dict:
    from curator.coleta.deezer import ClienteDeezer

    ua = user_agent(cfg)
    out = {
        "deezer": ClienteDeezer(dormir=dormir, abrir=abrir),
        # 1 pedido/s é o limite público do MusicBrainz; 1,1 s dá folga
        "musicbrainz": ClienteHTTP("https://musicbrainz.org/ws/2", 1.1, ua, abrir, dormir),
    }
    return out


def coletar(
    con: sqlite3.Connection,
    cfg: dict,
    cli: dict,
    fontes=FONTES,
    limite: int | None = None,
    forcar: bool = False,
    ids: list[str] | None = None,
    repetir_sem_casamento: bool = False,
    ao_processar: Callable[[str, str, str], None] | None = None,
    deve_parar: Callable[[], bool] = lambda: False,
    relogio: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Counter:
    """Uma fonte por vez, na ordem Deezer → MusicBrainz (usa o ISRC do Deezer). `limite`: por fonte."""
    e, p = cfg["externos"], cfg["provedores"]
    cont: Counter = Counter()
    for fonte in [f for f in FONTES if f in fontes and f in cli]:
        lista = pendentes(con, fonte, e["ttl_dias"], relogio(), forcar, repetir_sem_casamento)
        if ids is not None:
            pedidos = set(ids)
            lista = [v for v in lista if v in pedidos]
        for vid in lista[:limite] if limite is not None else lista:
            if deve_parar():
                return cont
            f = _faixa(con, vid)
            try:
                if fonte == "deezer":
                    b = deezer(
                        con, cli["deezer"], f, p["tolerancia_duracao_s"], p["limiar_casamento"], e["ttl_dias"]
                    )
                else:
                    isrc = con.execute(
                        "SELECT json_extract(dados, '$.isrc') FROM externos_faixas "
                        "WHERE video_id = ? AND fonte = 'deezer' AND status = 'ok'",
                        (vid,),
                    ).fetchone()
                    b = musicbrainz(
                        con,
                        cli["musicbrainz"],
                        f,
                        isrc[0] if isrc else None,
                        p["limiar_casamento"],
                        e["ttl_dias"],
                    )
            except Exception as ex:  # ErroExterno, ErroDeezer, resposta inesperada: nova tentativa mais tarde
                log.warning("%s: %s falhou (%s)", f.titulo, fonte, ex)
                falhar(con, vid, fonte, str(ex) or type(ex).__name__, relogio())
                cont[f"{fonte}: erro"] += 1
                if ao_processar:
                    ao_processar(fonte, f.titulo, "erro")
                continue
            gravar(con, vid, fonte, b)
            cont[f"{fonte}: {b.status}"] += 1
            if ao_processar:
                ao_processar(fonte, f.titulo, b.status)
    return cont


def resumo(con: sqlite3.Connection) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {f: {} for f in FONTES}
    for fonte, status, n in con.execute(
        f"""SELECT fonte, status, count(*) FROM externos_faixas WHERE video_id IN ({BIBLIOTECA})
            GROUP BY fonte, status"""
    ):
        out.setdefault(fonte, {})[status] = n
    return out
