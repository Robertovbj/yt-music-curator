"""#64: candidatas do rádio do YT a partir de várias sementes, sem rede (FalsoYT)."""

import pytest

from curator.coleta import candidatas as cd
from curator.coleta.cliente import ClienteYT
from curator.config import caminhos
from curator.motor import recomendacoes as rc
from tests.falso_motor import biblioteca_analisada, playlist
from tests.falso_yt import FalsoYT, faixa


@pytest.fixture
def con():
    c = biblioteca_analisada(caminhos().banco, por_estilo=6)
    playlist(c, "PLrock", "Rock", [f"rock{i:02d}" for i in range(6)])
    return c


@pytest.fixture
def yt():
    y = FalsoYT()
    y.radios = {
        "rock00": [faixa("n1", like="INDIFFERENT"), faixa("rock01"), faixa("n2"), faixa("n3", like="LIKE")],
        "rock01": [faixa("n2"), faixa("n4"), faixa("rock00")],
    }
    y.relacionadas = {"rock00": [faixa("r1"), faixa("n1")]}
    return y


def cli(yt):
    return ClienteYT(yt, intervalo_s=0, dormir=lambda s: None)


def test_coleta_salva_snapshot_e_grava_candidatas_fora_da_biblioteca(con, yt):
    from curator.banco import consultas

    ids = cd.coletar(con, cli(yt), ["rock00", "rock01"], ["yt_radio", "yt_related"], cache_h=12)
    coletas = {(c["fonte"], c["semente"]): c["itens"] for c in rc.ler_coletas(con, ids)}
    assert coletas[("yt_radio", "rock00")] == ["n1", "rock01", "n2", "n3"]  # sem a própria semente
    assert coletas[("yt_related", "rock00")] == ["r1", "n1"]
    assert coletas[("yt_related", "rock01")] == []  # sem aba "relacionadas"
    assert not {"n1", "n2", "r1"} & set(consultas.biblioteca(con))
    dur, aval = con.execute("SELECT duracao_s, avaliacao FROM faixas WHERE video_id = 'n3'").fetchone()
    assert dur == 200 and aval == "like"
    assert yt.chamadas["get_watch_playlist"] == 2  # uma chamada serve ao rádio e às relacionadas


def test_cache_reaproveita_a_coleta_e_depois_coleta_de_novo(con, yt):
    primeira = cd.coletar(con, cli(yt), ["rock00"], ["yt_radio"], cache_h=12)
    assert cd.coletar(con, cli(yt), ["rock00"], ["yt_radio"], cache_h=12) == primeira
    assert yt.chamadas["get_watch_playlist"] == 1
    con.execute("UPDATE coletas SET coletada_em = '2000-01-01T00:00:00Z'")
    assert cd.coletar(con, cli(yt), ["rock00"], ["yt_radio"], cache_h=12) != primeira


def test_semente_indisponivel_nao_derruba_o_pedido(con, yt):
    ids = cd.coletar(con, cli(yt), ["sumiu", "rock00"], ["yt_radio"], cache_h=12)
    assert [c["semente"] for c in rc.ler_coletas(con, ids)] == ["rock00"]


def test_sementes_de_playlist_sao_as_mais_centrais_e_deterministicas(con):
    a = cd.resolver_sementes(con, playlist="Rock", k=3)
    assert len(a) == 3 and set(a) <= {f"rock{i:02d}" for i in range(6)}
    assert cd.resolver_sementes(con, playlist="PLrock", k=3) == a


def test_sementes_sem_dislike_e_sem_repetir(con):
    con.execute("UPDATE faixas SET avaliacao = 'dislike' WHERE video_id = 'rock02'")
    s = cd.resolver_sementes(con, faixas=["rock02", "rap00"], playlist="Rock", artista="banda rock", k=6)
    assert "rock02" not in s and s[0] == "rap00" and len(s) == len(set(s))


def test_semente_invalida(con):
    with pytest.raises(cd.SementeInvalida):
        cd.resolver_sementes(con)
    with pytest.raises(cd.SementeInvalida):
        cd.resolver_sementes(con, playlist="Não existe")
    with pytest.raises(cd.SementeInvalida):
        cd.resolver_sementes(con, artista="Ninguém")


def test_candidatas_nao_geram_busca_de_album(con, yt):
    from curator.coleta.sincronizar import completar_metadados

    yt.radios["rock00"] = [faixa("n1", album="DeFora")]
    cd.coletar(con, cli(yt), ["rock00"], ["yt_radio"], cache_h=12)
    completar_metadados(con, cli(yt))
    assert yt.chamadas["get_album"] == 0
