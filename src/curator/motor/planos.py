"""Planos de alteração no YouTube Music (#29). Um plano é só um diff guardado: nada aqui toca o YT.

Operações: criar playlist, adicionar, remover, renomear. A aplicação (M7) lê o plano e executa.
"""

import json
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Literal

from curator.banco.conexao import agora

Tipo = Literal["criar", "adicionar", "remover", "renomear"]


@dataclass
class Operacao:
    tipo: Tipo
    playlist: str  # playlist_id existente, ou "nova:<nome>" para uma playlist que o plano cria
    video_ids: list[str] = field(default_factory=list)
    titulo: str | None = None  # criar / renomear
    motivo: dict[str, str] = field(default_factory=dict)  # video_id → explicação curta


def _agregadoras_acima(con: sqlite3.Connection, pid: str) -> list[str]:
    """Mães (e avós…) do tipo agregadora: precisam conter tudo o que entra nas filhas (ADR-03)."""
    out = []
    atual = con.execute("SELECT mae_id FROM playlists WHERE playlist_id = ?", (pid,)).fetchone()
    while atual and atual[0]:
        mae = con.execute(
            "SELECT playlist_id, tipo, mae_id FROM playlists WHERE playlist_id = ?", (atual[0],)
        ).fetchone()
        if mae is None:
            break
        if mae["tipo"] == "agregadora":
            out.append(mae["playlist_id"])
        atual = (mae["mae_id"],)
    return out


def _propagar(con: sqlite3.Connection, ops: list[Operacao]) -> list[Operacao]:
    """Para cada faixa adicionada numa filha, adiciona também nas agregadoras acima dela (#43)."""
    extra: dict[str, Operacao] = {}
    destinos = {o.playlist for o in ops if o.tipo == "adicionar"}
    for o in ops:
        if o.tipo != "adicionar" or o.playlist.startswith("nova:"):
            continue
        for mae in _agregadoras_acima(con, o.playlist):
            alvo = (
                next((x for x in ops if x.tipo == "adicionar" and x.playlist == mae), None)
                if mae in destinos
                else None
            )
            alvo = alvo or extra.setdefault(mae, Operacao("adicionar", mae))
            for v in o.video_ids:
                if v not in alvo.video_ids:
                    alvo.video_ids.append(v)
                    alvo.motivo[v] = "agregadora: contém tudo o que está nas filhas"
    return ops + list(extra.values())


def criar(
    con: sqlite3.Connection, origem: str, ops: list[Operacao], propagar_agregadoras: bool = True
) -> int:
    """Guarda o plano. Registra o retrato (snapshot_hash) de cada playlist existente afetada, para a aplicação
    recusar o plano se a playlist mudar no YT depois disso (#44)."""
    ops = [o for o in ops if o.tipo in ("criar", "renomear") or o.video_ids]
    if not ops:
        raise ValueError("plano vazio: nenhuma alteração")
    if propagar_agregadoras:
        ops = _propagar(con, ops)
    # não adiciona o que a playlist já tem
    for o in ops:
        if o.tipo == "adicionar" and not o.playlist.startswith("nova:"):
            ja = {
                r[0]
                for r in con.execute(
                    "SELECT video_id FROM playlist_faixas WHERE playlist_id = ?", (o.playlist,)
                )
            }
            o.video_ids = [v for v in dict.fromkeys(o.video_ids) if v not in ja]
    ops = [o for o in ops if o.tipo in ("criar", "renomear") or o.video_ids]
    if not ops:
        raise ValueError("plano vazio: as faixas já estão nas playlists")
    afetadas = sorted({o.playlist for o in ops if not o.playlist.startswith("nova:")})
    snapshots = {
        pid: h
        for pid, h in con.execute(
            f"SELECT playlist_id, snapshot_hash FROM playlists WHERE playlist_id IN ({','.join('?' * len(afetadas))})",
            afetadas,
        )
    } if afetadas else {}  # fmt: skip
    cur = con.execute(
        "INSERT INTO planos (origem, diff, snapshots, criado_em) VALUES (?, ?, ?, ?)",
        (origem, json.dumps([asdict(o) for o in ops], ensure_ascii=False), json.dumps(snapshots), agora()),
    )
    return cur.lastrowid


def de_agregadoras(con: sqlite3.Connection) -> int:
    """Plano que faz cada agregadora conter todas as faixas das descendentes (#43)."""
    from curator.banco.hierarquia import faltantes_nas_agregadoras

    ops = [
        Operacao("adicionar", mae, ids, motivo=dict.fromkeys(ids, "está numa filha e falta na agregadora"))
        for mae, ids in faltantes_nas_agregadoras(con).items()
    ]
    return criar(con, "agregadoras", ops, propagar_agregadoras=False)


def obter(con: sqlite3.Connection, plano_id: int) -> tuple[dict, list[Operacao]] | None:
    linha = con.execute("SELECT * FROM planos WHERE plano_id = ?", (plano_id,)).fetchone()
    if linha is None:
        return None
    return dict(linha), [Operacao(**o) for o in json.loads(linha["diff"])]


def listar(con: sqlite3.Connection, status: str | None = None) -> list[dict]:
    sql = "SELECT plano_id, origem, status, criado_em, aplicado_em, diff FROM planos"
    linhas = con.execute(
        sql + (" WHERE status = ?" if status else "") + " ORDER BY plano_id DESC", (status,) if status else ()
    )
    out = []
    for r in linhas:
        linha = dict(r)
        ops = json.loads(linha.pop("diff"))
        out.append({**linha, "operacoes": len(ops), "faixas": sum(len(o["video_ids"]) for o in ops)})
    return out


def descartar(con: sqlite3.Connection, plano_id: int) -> bool:
    cur = con.execute(
        "UPDATE planos SET status = 'descartado' WHERE plano_id = ? AND status = 'pendente'", (plano_id,)
    )
    return cur.rowcount == 1


def diff_legivel(con: sqlite3.Connection, ops: list[Operacao]) -> str:
    """Agrupado por playlist: o que entra (+) e o que sai (−), com título e artista de cada faixa."""
    ids = sorted({v for o in ops for v in o.video_ids})
    nomes = {}
    if ids:
        for r in con.execute(
            f"SELECT video_id, titulo, artistas FROM faixas WHERE video_id IN ({','.join('?' * len(ids))})",
            ids,
        ):
            nomes[r["video_id"]] = (
                f"{r['titulo']} — {', '.join(a['nome'] for a in json.loads(r['artistas'])[:2])}"
            )
    titulos = dict(con.execute("SELECT playlist_id, titulo FROM playlists"))
    por_playlist = defaultdict(list)
    for o in ops:
        por_playlist[o.playlist].append(o)
    linhas = []
    for pl, lista in por_playlist.items():
        nome = pl.removeprefix("nova:") + " (nova)" if pl.startswith("nova:") else titulos.get(pl, pl)
        linhas.append(f"■ {nome}")
        for o in lista:
            if o.tipo == "criar":
                linhas.append(f"  criar playlist “{o.titulo or pl.removeprefix('nova:')}”")
            elif o.tipo == "renomear":
                linhas.append(f"  renomear para “{o.titulo}”")
            sinal = {"adicionar": "+", "remover": "−"}.get(o.tipo)
            for v in o.video_ids if sinal else []:
                motivo = f"  ({o.motivo[v]})" if v in o.motivo else ""
                linhas.append(f"  {sinal} {nomes.get(v, v)}{motivo}")
    return "\n".join(linhas)


# ------------------------------------------------------------------ planos a partir do motor (CLI e MCP)


def de_receita(
    con: sqlite3.Connection, nome: str, ids: list[str], scores: dict[str, float], destino: str | None = None
) -> int:
    """Plano com o resultado de uma receita: cria uma playlist nova com o nome da receita, ou completa `destino`."""
    alvo = destino or f"nova:{nome}"
    ops = [] if destino else [Operacao("criar", alvo, titulo=nome)]
    ops.append(
        Operacao(
            "adicionar", alvo, ids, motivo={v: f"receita {nome}, score {scores.get(v, 0):.3f}" for v in ids}
        )
    )
    return criar(con, f"receita:{nome}", ops)


def de_classificacao(con: sqlite3.Connection, decisoes) -> int:
    por: dict[str, Operacao] = {}
    for d in decisoes:
        for x in d.destinos:
            op = por.setdefault(x.playlist_id, Operacao("adicionar", x.playlist_id))
            op.video_ids.append(d.video_id)
            op.motivo[d.video_id] = f"score {x.score:.2f} ≥ limiar {x.limiar:.2f}"
    return criar(con, "classificar", list(por.values()))


def de_dislikes(con: sqlite3.Connection) -> int:
    """Plano que tira das playlists próprias as faixas com dislike (#61). Nunca é automático: o usuário revisa
    e aplica. As faixas continuam no banco, com a análise."""
    from curator.banco.consultas import dislikes_em_playlists

    ops = [
        Operacao("remover", pid, ids, motivo=dict.fromkeys(ids, "dislike no YouTube Music"))
        for pid, ids in dislikes_em_playlists(con).items()
    ]
    return criar(con, "dislikes", ops, propagar_agregadoras=False)


def de_reorganizacao(con: sqlite3.Connection, sugestoes, escopo: str) -> int:
    return criar(con, f"reorganizar:{escopo}", [o for s in sugestoes for o in s.operacoes])
