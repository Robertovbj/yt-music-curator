import numpy as np
import pytest

from curator.audio.analisador import (
    Resultado,
    concordancia_tempo,
    energia,
    generos_personalizados,
    normalizar_1a9,
)
from curator.audio.persistir import Origem, gravar_analise, gravar_falha, precisa_analise
from curator.banco import vetores
from curator.banco.conexao import abrir, agora


@pytest.fixture
def con(tmp_path):
    con = abrir(tmp_path / "c.db")
    con.execute(
        "INSERT INTO faixas (video_id, titulo, criada_em, atualizada_em) VALUES ('v', 't', ?, ?)",
        (agora(), agora()),
    )
    return con


def resultado():
    rng = np.random.default_rng(1)
    return Resultado(
        features={
            "bpm": 128.0,
            "tom": "A",
            "escala": "menor",
            "danceability": 0.8,
            "relaxed": 0.1,
            "energy": 0.7,
        },
        vetores={
            "effnet_emb": rng.random(1280, dtype=np.float32),
            "discogs400": rng.random(400, dtype=np.float32),
        },
        rotulos={"discogs400": [f"Rock---Estilo {i}" for i in range(400)]},
        duracao_s=201.5,
        tempos={"effnet": 3.2},
    )


def test_gravar_analise_completa(con):
    r = resultado()
    gravar_analise(con, "v", r, Origem("youtube_music", "https://x", 1.0, 3_400_000), "1", "abc", 12.5)
    a = con.execute("SELECT * FROM analises WHERE video_id = 'v'").fetchone()
    assert (a["status"], a["fonte_audio"], a["bytes_baixados"], a["duracao_analisada"]) == (
        "ok",
        "youtube_music",
        3_400_000,
        201.5,
    )
    f = con.execute("SELECT bpm, tom, escala, danceability, aggressive FROM features").fetchone()
    assert tuple(f) == (128.0, "A", "menor", 0.8, None)
    assert np.array_equal(vetores.ler(con, "v", "discogs400"), r.vetores["discogs400"])
    assert (
        con.execute("SELECT rotulo FROM rotulos_modelo WHERE tipo = 'discogs400' AND indice = 7").fetchone()[
            0
        ]
        == "Rock---Estilo 7"
    )


def test_reanalise_so_quando_versao_ou_modelos_mudam(con):
    assert precisa_analise(con, "v", "1", "abc")
    gravar_analise(con, "v", resultado(), Origem("youtube_music", "x"), "1", "abc", 1.0)
    assert not precisa_analise(con, "v", "1", "abc")
    assert precisa_analise(con, "v", "2", "abc")
    assert precisa_analise(con, "v", "1", "outro")


def test_falha_registrada_e_continua_pendente(con):
    gravar_falha(con, "v", "sem-audio", "nenhum provedor")
    assert con.execute("SELECT status, erro FROM analises").fetchone()[:] == ("sem-audio", "nenhum provedor")
    assert precisa_analise(con, "v", "1", "abc")


def test_regravar_substitui_vetores(con):
    gravar_analise(con, "v", resultado(), Origem("youtube_music", "x"), "1", "abc", 1.0)
    r = resultado()
    del r.vetores["discogs400"]
    gravar_analise(con, "v", r, Origem("youtube_music", "x"), "2", "abc", 1.0)
    assert vetores.ler(con, "v", "discogs400") is None


def test_heuristicas():
    assert (
        normalizar_1a9(1) == 0.0
        and normalizar_1a9(9) == 1.0
        and normalizar_1a9(5) == 0.5
        and normalizar_1a9(12) == 1.0
    )
    assert energia(0.8, 0.6, 0.2, 0.4) == pytest.approx((0.8 + 0.6 + 0.8 + 0.4) / 4)
    assert energia(None, None, None, None) is None
    assert concordancia_tempo(120, 120.5) == "consistente"
    assert concordancia_tempo(240, 120) == "oitava"
    assert concordancia_tempo(100, 140) == "divergem"
    rot = [
        "Rock---Alternative Rock",
        "Rock---Pop Rock",
        "Rock---Hard Rock",
        "Rock---Post-Hardcore",
        "Pop---K-pop",
    ]
    g = generos_personalizados(np.array([0.1, 0.5, 0.2, 0.0, 0.9]), rot)
    assert g == {"Alternative Rock": 0.1, "Modern Rock": 0.5}
