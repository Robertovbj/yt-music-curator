"""#60: dislikes do YouTube Music sincronizados, sem nunca apagar a faixa nem a análise."""

import pytest

from curator.audio.persistir import Origem, gravar_analise
from curator.banco import consultas
from curator.banco.conexao import abrir
from curator.coleta.cliente import ClienteYT
from curator.coleta.sincronizar import gravar_faixa, sincronizar_curtidas, sincronizar_playlists
from curator.config import caminhos
from tests.falso_audio import resultado_falso
from tests.falso_yt import FalsoYT, faixa


@pytest.fixture
def yt():
    y = FalsoYT()
    y.curtidas = [faixa("a"), faixa("b")]
    y.playlists = {
        "PL1": {"title": "Rock", "tracks": [faixa("a", like="LIKE"), faixa("c", like="INDIFFERENT")]}
    }
    return y


@pytest.fixture
def con():
    return abrir(caminhos().banco)


def sincronizar(con, yt):
    cli = ClienteYT(yt, intervalo_s=0, dormir=lambda s: None)
    sincronizar_curtidas(con, cli)
    sincronizar_playlists(con, cli, forcar=True)


def avaliacao(con, vid):
    return con.execute("SELECT avaliacao FROM faixas WHERE video_id = ?", (vid,)).fetchone()[0]


def test_curtidas_viram_like_e_o_resto_neutra(con, yt):
    sincronizar(con, yt)
    assert (avaliacao(con, "a"), avaliacao(con, "b"), avaliacao(con, "c")) == ("like", "like", "neutra")


def test_dislike_na_playlist_e_desfeito_quando_volta_a_indiferente(con, yt):
    sincronizar(con, yt)
    yt.playlists["PL1"]["tracks"][1] = faixa("c", like="DISLIKE")
    sincronizar(con, yt)
    assert avaliacao(con, "c") == "dislike"
    assert consultas.estatisticas(con)["dislikes"] == 1
    yt.playlists["PL1"]["tracks"][1] = faixa("c", like="INDIFFERENT")
    sincronizar(con, yt)
    assert avaliacao(con, "c") == "neutra"


def test_descurtir_e_dar_dislike_tira_o_like(con, yt):
    sincronizar(con, yt)
    yt.curtidas = [faixa("b")]  # o YT tira das curtidas quando recebe dislike
    yt.playlists["PL1"]["tracks"][0] = faixa("a", like="DISLIKE")
    sincronizar(con, yt)
    assert avaliacao(con, "a") == "dislike"
    yt.playlists["PL1"]["tracks"] = []
    yt.curtidas = []
    sincronizar(con, yt)
    assert avaliacao(con, "b") == "neutra"  # só descurtida


def test_radio_so_confirma_like_e_nunca_desfaz_dislike(con, yt):
    sincronizar(con, yt)
    yt.playlists["PL1"]["tracks"][1] = faixa("c", like="DISLIKE")
    sincronizar(con, yt)
    gravar_faixa(con, faixa("c", like="INDIFFERENT"), avaliacao_confiavel=False)
    assert avaliacao(con, "c") == "dislike"
    gravar_faixa(con, faixa("novo", like="INDIFFERENT"), avaliacao_confiavel=False)
    assert avaliacao(con, "novo") == "neutra"
    gravar_faixa(con, faixa("novo", like="LIKE"), avaliacao_confiavel=False)
    assert avaliacao(con, "novo") == "like"


def test_faixa_analisada_com_dislike_mantem_analise(con, yt):
    sincronizar(con, yt)
    gravar_analise(con, "c", resultado_falso(), Origem("youtube_music", "c"), "1", "h", 1.0)
    yt.playlists["PL1"]["tracks"][1] = faixa("c", like="DISLIKE")
    sincronizar(con, yt)
    assert avaliacao(con, "c") == "dislike"
    assert con.execute("SELECT count(*) FROM vetores WHERE video_id = 'c'").fetchone()[0] > 0
    assert con.execute("SELECT status FROM analises WHERE video_id = 'c'").fetchone()[0] == "ok"
