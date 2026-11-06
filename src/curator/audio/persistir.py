"""Grava o resultado do analisador em `analises`, `features`, `vetores` e `rotulos_modelo` (ADR-02)."""

import json
import sqlite3
from dataclasses import dataclass

from curator.audio import extras
from curator.audio.analisador import Resultado
from curator.banco import vetores
from curator.banco.conexao import agora, transacao

COLUNAS_FEATURES = (
    "bpm", "bpm_rhythm", "bpm_tempocnn", "beat_confidence", "tom", "escala", "key_strength",
    "danceability", "acousticness", "aggressive", "electronic", "happy", "sad", "relaxed", "party",
    "instrumental", "voz_feminina", "engagement", "approachability",
    "valence", "arousal", "valence_spread", "arousal_spread", "energy",
)  # fmt: skip


@dataclass
class Origem:
    """De onde veio o áudio analisado."""

    fonte: str  # nome do provedor
    ref: str  # id/URL no provedor
    score: float = 1.0
    bytes_baixados: int | None = None


def precisa_analise(con: sqlite3.Connection, video_id: str, versao: str, hash_modelos: str) -> bool:
    linha = con.execute(
        "SELECT status, versao_analisador, hash_modelos FROM analises WHERE video_id = ?", (video_id,)
    ).fetchone()
    return not (
        linha
        and linha["status"] == "ok"
        and linha["versao_analisador"] == versao
        and linha["hash_modelos"] == hash_modelos
    )


def gravar_analise(
    con: sqlite3.Connection,
    video_id: str,
    r: Resultado,
    origem: Origem,
    versao: str,
    hash_modelos: str,
    tempo_s: float,
) -> None:
    with transacao(con):
        for tipo, rotulos in r.rotulos.items():
            con.executemany(
                "INSERT INTO rotulos_modelo (tipo, indice, rotulo) VALUES (?, ?, ?) "
                "ON CONFLICT (tipo, indice) DO UPDATE SET rotulo = excluded.rotulo",
                [(tipo, i, rot) for i, rot in enumerate(rotulos)],
            )
        valores = [r.features.get(c) for c in COLUNAS_FEATURES]
        con.execute(
            f"INSERT OR REPLACE INTO features (video_id, {', '.join(COLUNAS_FEATURES)}) "
            f"VALUES (?, {', '.join('?' * len(COLUNAS_FEATURES))})",
            (video_id, *valores),
        )
        # os extras (#88) têm versão própria: só caem se o áudio mudou de fonte
        antes = con.execute("SELECT fonte_ref FROM analises WHERE video_id = ?", (video_id,)).fetchone()
        if antes and antes[0] != origem.ref:
            extras.descartar(con, video_id)
        con.execute(
            f"DELETE FROM vetores WHERE video_id = ? AND tipo NOT IN ({','.join('?' * len(extras.TIPOS_EXTRAS))})",
            (video_id, *extras.TIPOS_EXTRAS),
        )
        for tipo, v in r.vetores.items():
            vetores.gravar(con, video_id, tipo, v)
        extras.gravar(con, video_id, r.extras, origem.ref)
        _gravar_status(
            con, video_id, "ok", versao=versao, hash_modelos=hash_modelos, origem=origem,
            duracao=r.duracao_analisada_s or r.duracao_s, tempo_s=tempo_s, tempos=r.tempos,
        )  # fmt: skip


def gravar_falha(con: sqlite3.Connection, video_id: str, status: str, erro: str) -> None:
    """`erro` (falha da análise) ou `sem-audio` (nenhum provedor teve a faixa)."""
    assert status in ("erro", "sem-audio")
    _gravar_status(con, video_id, status, erro=erro[:1000])


def _gravar_status(con, video_id, status, *, versao=None, hash_modelos=None, origem: Origem | None = None,
                   duracao=None, tempo_s=None, tempos=None, erro=None) -> None:  # fmt: skip
    con.execute(
        """INSERT OR REPLACE INTO analises (video_id, status, versao_analisador, hash_modelos, fonte_audio, fonte_ref,
             score_casamento, duracao_analisada, bytes_baixados, tempo_s, tempos, erro, analisada_em)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            video_id, status, versao, hash_modelos,
            origem.fonte if origem else None, origem.ref if origem else None,
            origem.score if origem else None, duracao,
            origem.bytes_baixados if origem else None, tempo_s,
            json.dumps(tempos) if tempos else None, erro, agora(),
        ),
    )  # fmt: skip
