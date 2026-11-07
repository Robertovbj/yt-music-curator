"""Espelha a biblioteca do YouTube Music no SQLite: curtidas, playlists próprias e metadados de álbum.

A sincronização é total: o que sumiu no YT some no banco (o helper antigo só acrescentava).
"""

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from curator.banco.conexao import agora, transacao
from curator.banco.consultas import BIBLIOTECA
from curator.coleta.cliente import ClienteYT
from curator.texto import id_artista

log = logging.getLogger(__name__)

# Playlists automáticas que aparecem na biblioteca mas não são do usuário.
IGNORADAS = {"LM", "SE"}


@dataclass
class Resumo:
    contagens: dict[str, int] = field(default_factory=dict)
    avisos: list[str] = field(default_factory=list)

    def conta(self, chave: str, n: int = 1) -> None:
        self.contagens[chave] = self.contagens.get(chave, 0) + n


# ------------------------------------------------------------------ faixas

# likeStatus do ytmusicapi → avaliação. O `get_playlist` (e as curtidas, que são uma playlist) lê o botão de
# like de verdade; no `get_watch_playlist` (rádio) "INDIFFERENT" pode ser um dislike, então de lá só o LIKE é
# confiável (ver a docstring do ytmusicapi), e nada que venha do rádio desfaz um dislike. Para uma faixa fora
# das playlists, `avaliacao_atual` lê o botão de like do player, que mostra o dislike.
AVALIACAO = {"LIKE": "like", "DISLIKE": "dislike", "INDIFFERENT": "neutra"}


def registrar_avaliacao(
    con: sqlite3.Connection, video_id: str, like_status: str | None, confiavel: bool = True
) -> bool:
    """Grava a avaliação vista no YT; devolve True se mudou. Com `confiavel=False`, só aceita LIKE."""
    nova = AVALIACAO.get(like_status or "")
    if nova is None or (not confiavel and nova != "like"):
        return False
    momento = agora()
    if confiavel:
        con.execute("UPDATE faixas SET avaliacao_conferida_em = ? WHERE video_id = ?", (momento, video_id))
    cur = con.execute(
        "UPDATE faixas SET avaliacao = ?, avaliacao_em = ? WHERE video_id = ? AND avaliacao != ?",
        (nova, momento, video_id, nova),
    )
    return cur.rowcount == 1


def avaliacao_atual(cli: ClienteYT, video_id: str) -> str | None:
    """LIKE, DISLIKE ou INDIFFERENT da faixa, lido do botão de like do player (endpoint "next", o mesmo do
    `get_watch_playlist`): o único lugar fora das playlists em que o dislike aparece. O ytmusicapi não expõe
    esse campo, por isso a chamada crua. None se a resposta não trouxer o botão."""
    raw = cli.chamar("_send_request", "next", {"videoId": video_id, "isAudioOnly": True})
    for a in (raw.get("playerOverlays") or {}).get("playerOverlayRenderer", {}).get("actions") or []:
        b = a.get("likeButtonRenderer") if isinstance(a, dict) else None
        if b and (b.get("target") or {}).get("videoId") == video_id:
            return b.get("likeStatus")
    return None


def conferir_avaliacoes(con: sqlite3.Connection, cli: ClienteYT, ids: list[str]) -> Resumo:
    """Lê a avaliação real de cada faixa (uma chamada por faixa, com a pausa do cliente)."""
    r = Resumo()
    for vid in ids:
        try:
            status = avaliacao_atual(cli, vid)
        except Exception as e:  # uma faixa indisponível não interrompe a conferência
            log.warning("avaliação de %s: %s", vid, e)
            r.conta("falhas")
            continue
        if status is None:
            r.conta("sem_resposta")
            continue
        mudou = registrar_avaliacao(con, vid, status, confiavel=True)
        r.conta("conferidas")
        if mudou and status == "DISLIKE":
            r.conta("dislikes_novos")
        elif mudou and status == "LIKE":
            r.conta("likes_novos")
    return r


def gravar_faixa(con: sqlite3.Connection, t: dict, avaliacao_confiavel: bool = True) -> str | None:
    """Insere ou atualiza uma faixa do ytmusicapi; devolve o video_id (None se indisponível).

    `avaliacao_confiavel=False` para respostas do rádio (ver AVALIACAO)."""
    video_id = t.get("videoId")
    if not video_id:
        return None
    artistas = [
        {"id": id_artista(a.get("id"), a.get("name") or ""), "nome": a.get("name") or ""}
        for a in (t.get("artists") or [])
        if a.get("name")
    ]
    album = t.get("album") or {}
    album_id = album.get("id")
    explicita = None if t.get("isExplicit") is None else int(bool(t["isExplicit"]))
    tipo_video = (t.get("videoType") or "").removeprefix("MUSIC_VIDEO_TYPE_") or None  # ATV, OMV, UGC…
    momento = agora()
    if album_id:
        con.execute(
            "INSERT INTO albuns (album_id, titulo) VALUES (?, ?) ON CONFLICT (album_id) DO NOTHING",
            (album_id, album.get("name")),
        )
    con.execute(
        """INSERT INTO faixas (video_id, titulo, artistas, album_id, album_nome, duracao_s, explicita, tipo_video,
                              criada_em, atualizada_em)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (video_id) DO UPDATE SET
             titulo = excluded.titulo,
             artistas = excluded.artistas,
             album_id = coalesce(excluded.album_id, faixas.album_id),
             album_nome = coalesce(excluded.album_nome, faixas.album_nome),
             duracao_s = coalesce(excluded.duracao_s, faixas.duracao_s),
             explicita = coalesce(excluded.explicita, faixas.explicita),
             tipo_video = coalesce(excluded.tipo_video, faixas.tipo_video),
             atualizada_em = excluded.atualizada_em""",
        (
            video_id,
            t.get("title") or video_id,
            json.dumps(artistas, ensure_ascii=False),
            album_id,
            album.get("name"),
            t.get("duration_seconds"),
            explicita,
            tipo_video,
            momento,
            momento,
        ),
    )
    con.execute("DELETE FROM faixa_artistas WHERE video_id = ?", (video_id,))
    for ordem, a in enumerate(artistas):
        con.execute(
            "INSERT INTO artistas (artista_id, nome) VALUES (?, ?) "
            "ON CONFLICT (artista_id) DO UPDATE SET nome = excluded.nome",
            (a["id"], a["nome"]),
        )
        con.execute(
            "INSERT OR IGNORE INTO faixa_artistas (video_id, artista_id, ordem) VALUES (?, ?, ?)",
            (video_id, a["id"], ordem),
        )
    registrar_avaliacao(con, video_id, t.get("likeStatus"), avaliacao_confiavel)
    return video_id


def sincronizar_curtidas(con: sqlite3.Connection, cli: ClienteYT) -> Resumo:
    r = Resumo()
    resposta = cli.chamar("get_liked_songs", limit=None)
    antes = {row[0] for row in con.execute("SELECT video_id FROM faixas WHERE curtida = 1")}
    agora_ids: list[str] = []
    with transacao(con):
        for t in resposta.get("tracks") or []:
            vid = gravar_faixa(con, t)
            if vid is None:
                r.conta("sem_video_id")
                continue
            if vid not in agora_ids:
                agora_ids.append(vid)
        con.execute("UPDATE faixas SET curtida = 0, posicao_curtida = NULL WHERE curtida = 1")
        con.executemany(
            "UPDATE faixas SET curtida = 1, posicao_curtida = ? WHERE video_id = ?",
            [(i, vid) for i, vid in enumerate(agora_ids, start=1)],
        )
        # descurtida: deixa de ser "like" (se virou dislike, a playlist em que ela estiver diz)
        con.execute(
            "UPDATE faixas SET avaliacao = 'neutra', avaliacao_em = ? WHERE avaliacao = 'like' AND curtida = 0",
            (agora(),),
        )
        con.execute(
            "UPDATE faixas SET avaliacao = 'like', avaliacao_em = ? WHERE curtida = 1 AND avaliacao != 'like'",
            (agora(),),
        )
    novos = set(agora_ids)
    r.conta("total", len(novos))
    r.conta("novas", len(novos - antes))
    r.conta("removidas", len(antes - novos))
    r.conta("inalteradas", len(novos & antes))
    if r.contagens.get("sem_video_id"):
        r.avisos.append(
            f"{r.contagens['sem_video_id']} curtida(s) indisponível(is), sem videoId, ignorada(s)"
        )
    return r


# ------------------------------------------------------------------ playlists


def gravar_playlist(
    con: sqlite3.Connection, pid: str, titulo: str, tracks: list[dict], total: int | None
) -> tuple[int, int]:
    """Reescreve o pertencimento de uma playlist própria (sincronização total). Devolve (faixas, sem video_id)."""
    sem = 0
    with transacao(con):
        itens = []
        for t in tracks:
            vid = gravar_faixa(con, t)
            if vid is None:
                sem += 1
                continue
            itens.append((vid, t.get("setVideoId")))
        ids = [vid for vid, _ in itens]
        con.execute(
            """INSERT INTO playlists (playlist_id, titulo, propria, total, snapshot_hash, sincronizada_em)
               VALUES (?, ?, 1, ?, ?, ?)
               ON CONFLICT (playlist_id) DO UPDATE SET titulo = excluded.titulo, propria = 1,
                 total = excluded.total, snapshot_hash = excluded.snapshot_hash,
                 sincronizada_em = excluded.sincronizada_em""",
            (pid, titulo, total if total is not None else len(ids), _hash(ids), agora()),
        )
        con.execute("DELETE FROM playlist_faixas WHERE playlist_id = ?", (pid,))
        con.executemany(
            "INSERT INTO playlist_faixas (playlist_id, posicao, video_id, set_video_id) VALUES (?, ?, ?, ?)",
            [(pid, i, vid, svid) for i, (vid, svid) in enumerate(itens, start=1)],
        )
    return len(itens), sem


def sincronizar_uma(con: sqlite3.Connection, cli: ClienteYT, pid: str) -> list[tuple[str, str | None]]:
    """Baixa e regrava uma playlist própria agora (usado depois de aplicar um plano). Devolve [(video_id, setVideoId)]."""
    detalhe = cli.chamar("get_playlist", pid, limit=None)
    tracks = detalhe.get("tracks") or []
    gravar_playlist(con, pid, detalhe.get("title") or pid, tracks, _contagem(detalhe.get("trackCount")))
    return [(t["videoId"], t.get("setVideoId")) for t in tracks if t.get("videoId")]


def _contagem(valor) -> int | None:
    if isinstance(valor, int):
        return valor
    m = re.search(r"\d[\d.,]*", str(valor or ""))
    return int(re.sub(r"[.,]", "", m[0])) if m else None


def _hash(video_ids: list[str]) -> str:
    return hashlib.sha256("\n".join(video_ids).encode()).hexdigest()[:16]


def _eh_propria(p: dict, conta: str | None) -> bool:
    if "owned" in p:
        return bool(p["owned"])
    autor = p.get("author")
    nomes = [a.get("name") for a in autor] if isinstance(autor, list) else [(autor or {}).get("name")]
    return conta is not None and conta in nomes


def sincronizar_playlists(
    con: sqlite3.Connection, cli: ClienteYT, forcar: bool = False, ttl_h: float = 24
) -> Resumo:
    """Baixa as playlists próprias da biblioteca e reescreve o pertencimento de cada uma."""
    r = Resumo()
    conta = (cli.chamar("get_account_info") or {}).get("accountName")
    listadas = [
        p for p in cli.chamar("get_library_playlists", limit=None) if p.get("playlistId") not in IGNORADAS
    ]
    limite = datetime.now(UTC) - timedelta(hours=ttl_h)
    vistas: set[str] = set()

    for p in listadas:
        pid = p["playlistId"]
        total_listado = _contagem(p.get("count"))
        atual = con.execute(
            "SELECT total, sincronizada_em, propria FROM playlists WHERE playlist_id = ?", (pid,)
        ).fetchone()
        if atual and not atual["propria"]:
            continue  # já sabemos que é de outra pessoa
        if (
            not forcar
            and atual
            and total_listado is not None
            and atual["total"] == total_listado
            and atual["sincronizada_em"]
            and datetime.fromisoformat(atual["sincronizada_em"]) > limite
        ):
            vistas.add(pid)
            r.conta("puladas")
            continue

        detalhe = cli.chamar("get_playlist", pid, limit=None)
        propria = _eh_propria(detalhe, conta) if "owned" in detalhe else _eh_propria(p, conta)
        titulo = detalhe.get("title") or p.get("title") or pid
        if not propria:
            con.execute(
                "INSERT INTO playlists (playlist_id, titulo, propria) VALUES (?, ?, 0) "
                "ON CONFLICT (playlist_id) DO UPDATE SET titulo = excluded.titulo, propria = 0",
                (pid, titulo),
            )
            r.conta("de_terceiros")
            continue

        vistas.add(pid)
        n, sem = gravar_playlist(con, pid, titulo, detalhe.get("tracks") or [], total_listado)
        if sem:
            r.conta("sem_video_id", sem)
        r.conta("baixadas")
        log.info("playlist %s: %d faixas", titulo, n)

    # Playlists próprias que sumiram da biblioteca foram apagadas no YT.
    sumidas = con.execute(
        f"SELECT playlist_id, titulo FROM playlists WHERE propria = 1 "
        f"AND playlist_id NOT IN ({','.join('?' * len(vistas))})",
        tuple(vistas),
    ).fetchall()
    with transacao(con):
        for s in sumidas:
            filhas = con.execute("SELECT count(*) FROM playlists WHERE mae_id = ?", (s[0],)).fetchone()[0]
            if filhas:
                r.avisos.append(f"playlist apagada no YT tinha {filhas} filha(s), agora sem mãe: {s[1]}")
            con.execute("DELETE FROM playlists WHERE playlist_id = ?", (s[0],))
            r.conta("removidas")
    return r


# ------------------------------------------------------------------ metadados


def completar_metadados(
    con: sqlite3.Connection, cli: ClienteYT, ttl_dias: float = 30, forcar: bool = False
) -> Resumo:
    """Ano e descrição dos álbuns. Falha agenda nova tentativa e nunca conta como cache."""
    r = Resumo()
    momento = datetime.now(UTC)
    vencido = (momento - timedelta(days=ttl_dias)).strftime("%Y-%m-%dT%H:%M:%SZ")
    agora_s = momento.strftime("%Y-%m-%dT%H:%M:%SZ")
    # só álbuns da biblioteca: as candidatas de recomendação (ADR-13) não geram buscas a cada sincronização
    da_biblioteca = f"album_id IN (SELECT album_id FROM faixas WHERE video_id IN ({BIBLIOTECA}))"
    filtro = (
        f"WHERE {da_biblioteca}"
        if forcar
        else f"WHERE {da_biblioteca} AND (metadados_em IS NULL OR metadados_em < ?) "
        "AND (proxima_tentativa_em IS NULL OR proxima_tentativa_em <= ?)"
    )
    pendentes = con.execute(
        f"SELECT album_id, titulo, tentativas FROM albuns {filtro} ORDER BY album_id",
        () if forcar else (vencido, agora_s),
    ).fetchall()
    total = con.execute(f"SELECT count(*) FROM albuns WHERE {da_biblioteca}").fetchone()[0]
    r.conta("em_cache", total - len(pendentes))

    for a in pendentes:
        try:
            album = cli.chamar("get_album", a["album_id"])
        except Exception as e:
            tentativas = a["tentativas"] + 1
            proxima = momento + min(timedelta(hours=2**tentativas), timedelta(days=7))
            con.execute(
                "UPDATE albuns SET tentativas = ?, erro = ?, proxima_tentativa_em = ? WHERE album_id = ?",
                (tentativas, str(e)[:500], proxima.strftime("%Y-%m-%dT%H:%M:%SZ"), a["album_id"]),
            )
            r.conta("falhas")
            log.warning("álbum %s: %s (nova tentativa em %s)", a["titulo"], e, proxima)
            continue
        ano = _contagem(album.get("year"))
        con.execute(
            """UPDATE albuns SET titulo = coalesce(?, titulo), ano = ?, descricao = ?, metadados_em = ?,
                 tentativas = 0, erro = NULL, proxima_tentativa_em = NULL WHERE album_id = ?""",
            (album.get("title"), ano, album.get("description"), agora(), a["album_id"]),
        )
        r.conta("buscados")
    return r


# ------------------------------------------------------------------ estatísticas (#85)


def completar_estatisticas(
    con: sqlite3.Connection, cli: ClienteYT, por_rodada: int = 300, ttl_dias: float = 30, forcar: bool = False
) -> Resumo:
    """Visualizações (globais) e data de publicação de cada faixa da biblioteca, pelo `get_song`.

    Uma chamada por faixa: no máximo `por_rodada` por sincronização, das que nunca foram buscadas ou venceram
    (`forcar` ignora a validade, não o limite). A API não tem contagem de reproduções por usuário. Falha agenda
    nova tentativa e nunca conta como cache."""
    r = Resumo()
    momento = datetime.now(UTC)
    agora_s = momento.strftime("%Y-%m-%dT%H:%M:%SZ")
    vencido = "9999" if forcar else (momento - timedelta(days=ttl_dias)).strftime("%Y-%m-%dT%H:%M:%SZ")
    pendentes = con.execute(
        f"""SELECT video_id, titulo, estatisticas_tentativas FROM faixas
            WHERE video_id IN ({BIBLIOTECA}) AND (estatisticas_em IS NULL OR estatisticas_em < :vencido)
              AND (:forcar OR estatisticas_proxima IS NULL OR estatisticas_proxima <= :agora)
            ORDER BY estatisticas_em IS NOT NULL, estatisticas_em, video_id""",
        {"vencido": vencido, "agora": agora_s, "forcar": forcar},
    ).fetchall()
    _buscar_estatisticas(con, cli, pendentes[:por_rodada], r, momento)
    if len(pendentes) > por_rodada:
        r.conta("faltam", len(pendentes) - por_rodada)
    return r


def estatisticas_de(con: sqlite3.Connection, cli: ClienteYT, ids: list[str], ttl_dias: float = 30) -> Resumo:
    """As mesmas estatísticas para faixas escolhidas (#87: as melhores candidatas de uma recomendação), só as
    que não têm o dado dentro da validade nem uma falha recente."""
    r = Resumo()
    momento = datetime.now(UTC)
    agora_s = momento.strftime("%Y-%m-%dT%H:%M:%SZ")
    vencido = (momento - timedelta(days=ttl_dias)).strftime("%Y-%m-%dT%H:%M:%SZ")
    linhas = []
    for i in range(0, len(ids), 900):
        lote = ids[i : i + 900]
        linhas += con.execute(
            f"""SELECT video_id, titulo, estatisticas_tentativas FROM faixas
                WHERE video_id IN ({",".join("?" * len(lote))})
                  AND (estatisticas_em IS NULL OR estatisticas_em < ?)
                  AND (estatisticas_proxima IS NULL OR estatisticas_proxima <= ?)""",
            (*lote, vencido, agora_s),
        ).fetchall()
    ordem = {v: i for i, v in enumerate(ids)}
    _buscar_estatisticas(con, cli, sorted(linhas, key=lambda f: ordem[f["video_id"]]), r, momento)
    return r


def _buscar_estatisticas(
    con: sqlite3.Connection, cli: ClienteYT, linhas, r: Resumo, momento: datetime
) -> None:
    for f in linhas:
        try:
            song = cli.chamar("get_song", f["video_id"])
        except Exception as e:
            tentativas = f["estatisticas_tentativas"] + 1
            proxima = momento + min(timedelta(hours=2**tentativas), timedelta(days=7))
            con.execute(
                "UPDATE faixas SET estatisticas_tentativas = ?, estatisticas_proxima = ? WHERE video_id = ?",
                (tentativas, proxima.strftime("%Y-%m-%dT%H:%M:%SZ"), f["video_id"]),
            )
            r.conta("falhas")
            log.warning("estatísticas de %s: %s (nova tentativa em %s)", f["titulo"], e, proxima)
            continue
        detalhes = song.get("videoDetails") or {}
        micro = (song.get("microformat") or {}).get("microformatDataRenderer") or {}
        publicada = (micro.get("publishDate") or micro.get("uploadDate") or "")[:10] or None
        con.execute(
            """UPDATE faixas SET visualizacoes = coalesce(?, visualizacoes), publicada_em = coalesce(?, publicada_em),
                 estatisticas_em = ?, estatisticas_tentativas = 0, estatisticas_proxima = NULL WHERE video_id = ?""",
            (_contagem(detalhes.get("viewCount")), publicada, agora(), f["video_id"]),
        )
        r.conta("buscadas" if detalhes else "sem_dados")
