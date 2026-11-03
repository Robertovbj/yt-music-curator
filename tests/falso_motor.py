"""Biblioteca sintética já analisada, para testar o motor sem Essentia.

Três "estilos" com embeddings agrupados (rock, ambient, rap), features coerentes com cada estilo e
probabilidades Discogs400 reduzidas a poucos rótulos. Tudo com semente fixa.
"""

import json

import numpy as np

from curator.audio.analisador import Resultado
from curator.audio.persistir import Origem, gravar_analise
from curator.banco.conexao import abrir, agora

ROTULOS = [
    "Rock---Alternative Rock",
    "Rock---Hard Rock",
    "Electronic---Ambient",
    "Hip Hop---Trap",
    "Hip Hop---Boom Bap",
]

ESTILOS = {
    # estilo: (features, índice do rótulo dominante, artista)
    "rock": ({"energy": 0.8, "aggressive": 0.7, "relaxed": 0.2, "happy": 0.5, "bpm": 140.0}, 1, "Banda Rock"),
    "ambient": (
        {"energy": 0.1, "aggressive": 0.02, "relaxed": 0.95, "happy": 0.3, "bpm": 70.0},
        2,
        "Ambientista",
    ),
    "rap": ({"energy": 0.6, "aggressive": 0.4, "relaxed": 0.3, "happy": 0.4, "bpm": 90.0}, 3, "MC Teste"),
}


def biblioteca_analisada(caminho, por_estilo=12, semente=0, dim=32):
    """Banco com `por_estilo` faixas curtidas de cada estilo (ids rock00, ambient00, rap00…) já analisadas."""
    rng = np.random.default_rng(semente)
    con = abrir(caminho)
    centros = {e: rng.standard_normal(dim) * 3 for e in ESTILOS}
    pos = 0
    for estilo, (feats, rot, artista) in ESTILOS.items():
        for i in range(por_estilo):
            vid = f"{estilo}{i:02d}"
            pos += 1
            arts = [{"id": f"UC_{estilo}", "nome": artista}]
            if estilo == "rap" and i == 0:
                arts.append({"id": "UC_convidado", "nome": "Convidado Especial"})
            con.execute(
                "INSERT INTO faixas (video_id, titulo, artistas, album_nome, duracao_s, curtida, posicao_curtida, "
                "criada_em, atualizada_em) VALUES (?, ?, ?, ?, 200, 1, ?, ?, ?)",
                (vid, f"{estilo.title()} {i}", json.dumps(arts), f"Álbum {estilo}", pos, agora(), agora()),
            )
            for ordem, a in enumerate(arts):
                con.execute("INSERT OR IGNORE INTO artistas VALUES (?, ?)", (a["id"], a["nome"]))
                con.execute("INSERT INTO faixa_artistas VALUES (?, ?, ?)", (vid, a["id"], ordem))
            f = {
                k: float(np.clip(v + rng.normal(0, 0.05 if k != "bpm" else 4), 0, 400))
                for k, v in feats.items()
            }
            probs = np.full(len(ROTULOS), 0.01, np.float32)
            probs[rot] = 0.5 + rng.uniform(0, 0.3)
            emb = (centros[estilo] + rng.standard_normal(dim)).astype(np.float32)
            r = Resultado(f, {"effnet_emb": emb, "discogs400": probs}, {"discogs400": ROTULOS}, 200.0, {})
            gravar_analise(con, vid, r, Origem("youtube_music", vid), "1", "h", 1.0)
    return con


def playlist(con, pid, titulo, ids, mae=None, tipo="folha"):
    con.execute(
        "INSERT INTO playlists (playlist_id, titulo, propria, tipo, total, sincronizada_em) VALUES (?, ?, 1, ?, ?, ?)",
        (pid, titulo, tipo, len(ids), agora()),
    )
    if mae:
        con.execute("UPDATE playlists SET mae_id = ? WHERE playlist_id = ?", (mae, pid))
    con.executemany(
        "INSERT INTO playlist_faixas (playlist_id, posicao, video_id) VALUES (?, ?, ?)",
        [(pid, i, v) for i, v in enumerate(ids, start=1)],
    )
