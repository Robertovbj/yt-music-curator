"""Aplica um plano no YouTube Music (M7, #42) com as salvaguardas da #44.

Antes de escrever qualquer coisa:
- confere se cada playlist afetada ainda está como estava quando o plano foi criado (snapshot_hash); se mudou
  no YT, o plano é recusado (regere o plano depois de sincronizar);
- guarda um backup do estado atual de cada playlist afetada (faixas, setVideoId e título), para `desfazer`.

Durante: executa as operações em ordem e para na primeira falha (status "parcial"), registrando cada uma.
Depois: ressincroniza as playlists afetadas e confere se o resultado bate com o plano.

Playlists criadas pelo plano são PRIVADAS. Quem decide aplicar é a CLI, com confirmação; o MCP não aplica.
"""

import json
import logging
import sqlite3
from dataclasses import dataclass, field

from curator.banco.conexao import agora
from curator.coleta.cliente import ClienteYT
from curator.coleta.sincronizar import _hash, sincronizar_uma
from curator.motor import planos

log = logging.getLogger(__name__)

LOTE = 50  # faixas por chamada de add/remove


class PlanoInvalido(RuntimeError):
    pass


class PlanoObsoleto(PlanoInvalido):
    pass


@dataclass
class Resultado:
    plano_id: int
    status: str
    log: list[dict] = field(default_factory=list)
    criadas: dict[str, str] = field(default_factory=dict)
    divergencias: list[str] = field(default_factory=list)


def _ok(resposta) -> bool:
    """O ytmusicapi devolve dict com 'status' (ou uma string de status) nas escritas."""
    if isinstance(resposta, dict):
        return "SUCCEEDED" in str(resposta.get("status", "")) or "playlistEditResults" in resposta
    return "SUCCEEDED" in str(resposta)


def _lotes(itens: list, n: int = LOTE):
    for i in range(0, len(itens), n):
        yield itens[i : i + n]


def total_faixas(ops: list[planos.Operacao]) -> int:
    return sum(len(o.video_ids) for o in ops)


def conferir_e_guardar(
    con: sqlite3.Connection, cli: ClienteYT, plano_id: int
) -> tuple[dict, list[planos.Operacao]]:
    """Salvaguardas antes de aplicar: plano pendente, playlists inalteradas no YT, backup gravado."""
    achado = planos.obter(con, plano_id)
    if achado is None:
        raise PlanoInvalido(f"plano #{plano_id} não existe")
    meta, ops = achado
    if meta["status"] != "pendente":
        raise PlanoInvalido(f"plano #{plano_id} está '{meta['status']}', não pendente")
    snapshots = json.loads(meta["snapshots"] or "{}")
    backup, mudaram = {}, []
    for pid in sorted({o.playlist for o in ops if not o.playlist.startswith("nova:")}):
        detalhe = cli.chamar("get_playlist", pid, limit=None)
        itens = [[t["videoId"], t.get("setVideoId")] for t in detalhe.get("tracks") or [] if t.get("videoId")]
        if pid in snapshots and snapshots[pid] and _hash([v for v, _ in itens]) != snapshots[pid]:
            mudaram.append(detalhe.get("title") or pid)
        backup[pid] = {"titulo": detalhe.get("title"), "itens": itens}
    if mudaram:
        raise PlanoObsoleto(
            "a(s) playlist(s) mudaram no YouTube Music depois que o plano foi criado: " + ", ".join(mudaram)
            + ". Rode `curator sincronizar` e gere o plano de novo."
        )  # fmt: skip
    con.execute(
        "UPDATE planos SET backup = ? WHERE plano_id = ?", (json.dumps(backup, ensure_ascii=False), plano_id)
    )
    return meta, ops


def aplicar(con: sqlite3.Connection, cli: ClienteYT, plano_id: int) -> Resultado:
    meta, ops = conferir_e_guardar(con, cli, plano_id)
    backup = json.loads(
        con.execute("SELECT backup FROM planos WHERE plano_id = ?", (plano_id,)).fetchone()[0]
    )
    res = Resultado(plano_id, "aplicado")
    for n, o in enumerate(ops):
        alvo = res.criadas.get(o.playlist, o.playlist)
        try:
            if o.tipo == "criar":
                nome = o.titulo or o.playlist.removeprefix("nova:")
                novo = cli.chamar(
                    "create_playlist", nome, f"Criada pelo yt-music-curator (plano #{plano_id})", "PRIVATE"
                )
                if not isinstance(novo, str):
                    raise RuntimeError(f"create_playlist não devolveu um id: {novo}")
                res.criadas[o.playlist] = novo
                detalhe = f"criada {novo}"
            elif o.tipo == "adicionar":
                for lote in _lotes(o.video_ids):
                    r = cli.chamar("add_playlist_items", alvo, lote, duplicates=False)
                    if not _ok(r):
                        raise RuntimeError(f"add_playlist_items recusou: {r}")
                detalhe = f"+{len(o.video_ids)}"
            elif o.tipo == "remover":
                sets = {v: s for v, s in backup.get(alvo, {}).get("itens", [])}
                videos = [{"videoId": v, "setVideoId": sets[v]} for v in o.video_ids if sets.get(v)]
                for lote in _lotes(videos):
                    r = cli.chamar("remove_playlist_items", alvo, lote)
                    if not _ok(r):
                        raise RuntimeError(f"remove_playlist_items recusou: {r}")
                detalhe = f"−{len(videos)}"
            elif o.tipo == "renomear":
                r = cli.chamar("edit_playlist", alvo, title=o.titulo)
                if not _ok(r):
                    raise RuntimeError(f"edit_playlist recusou: {r}")
                detalhe = f"renomeada para {o.titulo}"
            else:
                raise RuntimeError(f"operação desconhecida: {o.tipo}")
            res.log.append({"op": n, "tipo": o.tipo, "playlist": alvo, "ok": True, "detalhe": detalhe})
        except Exception as e:
            log.warning("plano #%d: operação %d (%s em %s) falhou: %s", plano_id, n, o.tipo, alvo, e)
            res.log.append({"op": n, "tipo": o.tipo, "playlist": alvo, "ok": False, "detalhe": str(e)[:500]})
            res.status = "parcial"
            break

    # ressincroniza e confere o que foi feito
    for o in ops:
        alvo = res.criadas.get(o.playlist, o.playlist)
        if alvo.startswith("nova:"):
            continue  # não chegou a ser criada
        try:
            ids = {v for v, _ in sincronizar_uma(con, cli, alvo)}
        except Exception as e:
            res.divergencias.append(f"não consegui ressincronizar {alvo}: {e}")
            continue
        if o.tipo == "adicionar" and (faltam := [v for v in o.video_ids if v not in ids]):
            res.divergencias.append(f"{alvo}: {len(faltam)} faixa(s) não apareceram depois de adicionar")
        if o.tipo == "remover" and (ficaram := [v for v in o.video_ids if v in ids]):
            res.divergencias.append(f"{alvo}: {len(ficaram)} faixa(s) continuam depois de remover")
    con.execute(
        "UPDATE planos SET status = ?, log = ?, criadas = ?, aplicado_em = ? WHERE plano_id = ?",
        (res.status, json.dumps({"operacoes": res.log, "divergencias": res.divergencias}, ensure_ascii=False),
         json.dumps(res.criadas), agora(), plano_id),
    )  # fmt: skip
    return res


def desfazer(con: sqlite3.Connection, cli: ClienteYT, plano_id: int) -> list[str]:
    """Volta cada playlist afetada ao backup e apaga as playlists que o plano criou. Devolve o que foi feito."""
    linha = con.execute(
        "SELECT status, backup, criadas FROM planos WHERE plano_id = ?", (plano_id,)
    ).fetchone()
    if linha is None:
        raise PlanoInvalido(f"plano #{plano_id} não existe")
    if linha["status"] not in ("aplicado", "parcial"):
        raise PlanoInvalido(
            f"plano #{plano_id} está '{linha['status']}'; só dá para desfazer um plano aplicado"
        )
    feito = []
    for pid, antes in json.loads(linha["backup"] or "{}").items():
        originais = [v for v, _ in antes["itens"]]
        atuais = sincronizar_uma(con, cli, pid)
        sobrando = [{"videoId": v, "setVideoId": s} for v, s in atuais if v not in set(originais) and s]
        faltando = [v for v in dict.fromkeys(originais) if v not in {x for x, _ in atuais}]
        for lote in _lotes(sobrando):
            cli.chamar("remove_playlist_items", pid, lote)
        for lote in _lotes(faltando):
            cli.chamar("add_playlist_items", pid, lote, duplicates=False)
        if antes.get("titulo"):
            cli.chamar("edit_playlist", pid, title=antes["titulo"])
        sincronizar_uma(con, cli, pid)
        feito.append(f"{antes.get('titulo') or pid}: −{len(sobrando)} +{len(faltando)}")
    for nome, pid in json.loads(linha["criadas"] or "{}").items():
        cli.chamar("delete_playlist", pid)
        con.execute("DELETE FROM playlists WHERE playlist_id = ?", (pid,))
        feito.append(f"{nome.removeprefix('nova:')}: playlist criada pelo plano apagada")
    con.execute(
        "UPDATE planos SET status = 'desfeito', desfeito_em = ? WHERE plano_id = ?", (agora(), plano_id)
    )
    return feito
