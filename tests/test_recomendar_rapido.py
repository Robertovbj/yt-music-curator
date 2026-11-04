"""#66: recomendação rápida, só com as coletas (sem baixar nem analisar as candidatas)."""

import json

import pytest

from curator.banco import consultas
from curator.coleta.cliente import ClienteYT
from curator.config import caminhos, carregar_config
from curator.motor import recomendacoes as rc
from curator.motor import recomendar as rr
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
    a, b = ("Artista A",), ("Artista B",)
    y.radios = {
        "rock00": [faixa("n1", artistas=a), faixa("rock03"), faixa("n2", artistas=b), faixa("n3", artistas=a),
                   faixa("n4", artistas=a), faixa("ruim", artistas=b)],
        "rock01": [faixa("n2", artistas=b), faixa("n1", artistas=a), faixa("n5", artistas=b), faixa("rap00")],
    }  # fmt: skip
    return y


def cfg(**rec):
    c = carregar_config()
    c["recomendacoes"] = {**c["recomendacoes"], **rec}
    return c


def pedir(con, yt, config=None, **kw):
    p = rr.Pedido(
        **{"modo": "rapido", "escopo": "novas", "faixas": ["rock00", "rock01"], "fontes": ["yt_radio"], **kw}
    )
    return rr.recomendar(con, ClienteYT(yt, intervalo_s=0, dormir=lambda s: None), None, p, config or cfg())


def test_so_novas_ordenadas_por_votos_e_posicao(con, yt):
    con.execute("UPDATE faixas SET avaliacao = 'dislike' WHERE video_id = 'rock05'")
    res = pedir(con, yt)
    ids = [i.video_id for i in res.itens]
    assert ids[:2] == ["n1", "n2"]  # as duas que vieram das duas sementes
    assert not set(ids) & set(consultas.biblioteca(con))
    assert {"rock00", "rock01"}.isdisjoint(ids)
    assert res.descartes["já na biblioteca"] == 2
    assert [i["video_id"] for i in rc.itens(con, res.recomendacao_id)] == ids


def test_dislike_nunca_aparece(con, yt):
    pedir(con, yt)  # grava as candidatas
    con.execute("UPDATE faixas SET avaliacao = 'dislike' WHERE video_id = 'n1'")
    res = pedir(con, yt)
    assert "n1" not in [i.video_id for i in res.itens] and res.descartes["dislike"] == 1


def test_limite_por_artista_e_tamanho(con, yt):
    res = pedir(con, yt, config=cfg(max_por_artista=1))
    principais = [json.loads(con.execute("SELECT artistas FROM faixas WHERE video_id = ?", (i.video_id,)).fetchone()[0])[0]["nome"] for i in res.itens]  # fmt: skip
    assert len(principais) == len(set(principais)) == 2
    assert len(pedir(con, yt, tamanho=3).itens) == 3


def test_mesma_coleta_mesmo_resultado(con, yt):
    res = pedir(con, yt)
    assert [i.video_id for i in rr.refazer(con, res.recomendacao_id, cfg())] == [
        i.video_id for i in res.itens
    ]


def test_repeticao_penaliza_e_depois_de_3_vezes_pausa_30_dias(con, yt):
    c = cfg(max_por_artista=10)  # todas as candidatas entram em todas as recomendações
    s1 = {i.video_id: i.score for i in pedir(con, yt, config=c).itens}
    s2 = {i.video_id: i for i in pedir(con, yt, config=c).itens}
    assert s2["n1"].vezes_antes == 1 and s2["n1"].score == pytest.approx(s1["n1"] * 0.85, abs=1e-5)
    pedir(con, yt, config=c)
    quarta = pedir(con, yt, config=c)
    assert quarta.itens == [] and quarta.descartes["recomendada demais sem reação"] == len(s1)
    con.execute("UPDATE recomendacoes SET criada_em = '2000-01-01T00:00:00Z'")  # passaram os 30 dias
    assert [i.video_id for i in pedir(con, yt, config=c).itens][:2] == ["n1", "n2"]


def test_curtida_depois_sai_das_proximas(con, yt):
    pedir(con, yt)
    con.execute("UPDATE faixas SET curtida = 1, avaliacao = 'like' WHERE video_id = 'n1'")
    assert "n1" not in [i.video_id for i in pedir(con, yt).itens]


def test_misturar_intercala_novas_e_conhecidas(con, yt):
    res = pedir(con, yt, escopo="misturar", tamanho=6, proporcao_novas=0.5)
    conhecidas = [i.conhecida for i in res.itens]
    assert conhecidas.count(True) == 3 and conhecidas.count(False) == 3
    assert "rock03" in [i.video_id for i in res.itens]  # conhecida que o rádio trouxe vem antes das do motor
    assert {i.video_id for i in res.itens if i.conhecida} <= set(consultas.biblioteca(con))


def test_pedido_sem_semente_ou_com_perfil_no_rapido_e_recusado():
    with pytest.raises(ValueError, match="semente"):
        rr.Pedido(modo="rapido", escopo="novas")
    with pytest.raises(ValueError, match="lento"):
        rr.Pedido(modo="rapido", escopo="novas", faixas=["x"], perfil_alvo={"energy": {"alvo": 0.9}})
    with pytest.raises(ValueError):
        rr.Pedido(faixas=["x"])  # modo e escopo não têm padrão


def test_intercalar():
    assert rr.intercalar([1, 2, 3], ["a", "b"], 4, 0.5) == [1, "a", 2, "b"]
    assert rr.intercalar([1], ["a", "b", "c"], 4, 0.5) == [1, "a", "b", "c"]
    assert rr.intercalar([1, 2], [], 4, 0.25) == [1, 2]
