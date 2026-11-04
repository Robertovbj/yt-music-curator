"""#60/#61: o dislike dado fora das playlists é visto (botão de like do player) e o que parece com dislikes
é penalizado."""

import json

import numpy as np
import pytest

from curator.coleta.cliente import ClienteYT
from curator.coleta.sincronizar import avaliacao_atual, conferir_avaliacoes
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
    # o rádio mostra "INDIFFERENT" até para o dislike (como o YT real faz no get_watch_playlist)
    y.radios = {"rock00": [faixa(f"n{i}", artistas=(f"Art{i}",), like="INDIFFERENT") for i in range(5)]}
    y.avaliacoes = {"n0": "DISLIKE"}
    return y


def cli(yt):
    return ClienteYT(yt, intervalo_s=0, dormir=lambda s: None)


def pedir(con, yt, **kw):
    p = rr.Pedido(**{"modo": "rapido", "escopo": "novas", "faixas": ["rock00"], "fontes": ["yt_radio"], **kw})
    return rr.recomendar(con, cli(yt), None, p, carregar_config())


def avaliacao(con, vid):
    return con.execute("SELECT avaliacao FROM faixas WHERE video_id = ?", (vid,)).fetchone()[0]


def test_le_o_botao_de_like_do_player(yt):
    yt.avaliacoes = {"a": "DISLIKE", "b": "LIKE"}
    assert [avaliacao_atual(cli(yt), v) for v in "abc"] == ["DISLIKE", "LIKE", "INDIFFERENT"]


def test_dislike_que_o_radio_esconde_e_conferido_e_descartado(con, yt):
    res = pedir(con, yt)
    assert "n0" not in [i.video_id for i in res.itens]
    assert res.descartes["dislike visto na conferência"] == 1
    assert avaliacao(con, "n0") == "dislike"
    # a conferência vale por alguns dias: o pedido seguinte não chama o YT de novo para as mesmas faixas
    antes = yt.chamadas["next"]
    pedir(con, yt)
    assert yt.chamadas["next"] == antes


def test_ja_curtida_no_yt_tambem_sai(con, yt):
    yt.avaliacoes = {"n1": "LIKE"}
    assert "n1" not in [i.video_id for i in pedir(con, yt).itens]


def test_lento_confere_antes_de_baixar(con, yt, monkeypatch):
    from curator.motor import lento

    monkeypatch.setattr(lento, "_versao", lambda: ("1", "h"))
    pedir(con, yt, modo="lento")
    fila = {r[0] for r in con.execute("SELECT video_id FROM fila_download")}
    assert "n0" not in fila and {"n1", "n2"} <= fila


def test_sincronizar_confere_as_recomendadas_sem_reacao(con, yt):
    rid = pedir(con, yt).recomendacao_id
    con.execute("UPDATE faixas SET avaliacao_conferida_em = '2000-01-01T00:00:00Z'")  # conferência vencida
    yt.avaliacoes["n3"] = "DISLIKE"  # dislike dado depois, ouvindo pelo app
    ids = rc.a_conferir(con, limite=100, validade_dias=7)
    assert set(ids) == {i["video_id"] for i in rc.itens(con, rid)}
    r = conferir_avaliacoes(con, cli(yt), ids)
    assert r.contagens["dislikes_novos"] == 1
    assert {i["video_id"]: i["status"] for i in rc.itens(con, rid)}["n3"] == "dislike"
    assert rc.a_conferir(con, limite=100, validade_dias=7) == []  # o resto foi conferido agora


def test_artista_com_dislikes_e_sem_curtidas_e_penalizado(con, yt):
    s0 = {i.video_id: i.score for i in pedir(con, yt).itens}
    # outra faixa do Art2, fora das recomendações, levou dislike
    con.execute(
        "INSERT INTO faixas (video_id, titulo, artistas, avaliacao, criada_em, atualizada_em) VALUES ('x', 'x', ?, 'dislike', 'x', 'x')",
        (json.dumps([{"id": "UC_art2", "nome": "Art2"}]),),
    )
    con.execute("INSERT INTO faixa_artistas VALUES ('x', 'UC_art2', 0)")
    con.execute("DELETE FROM recomendacoes")  # sem penalidade de repetição no meio
    itens = {i.video_id: i for i in pedir(con, yt).itens}
    assert itens["n2"].componentes["dislike"] == pytest.approx(0.5)
    assert itens["n2"].score == pytest.approx(s0["n2"] * 0.5, abs=1e-5)
    assert "dislike" not in itens["n1"].componentes


def test_artista_com_muitas_curtidas_quase_nao_e_penalizado(con):
    con.execute("UPDATE faixas SET avaliacao = 'dislike' WHERE video_id = 'rock05'")  # 1 dislike, 5 curtidas
    f = rr.fatores_dislike(con, ["rock04"], carregar_config())
    assert f["rock04"] == pytest.approx(1 - 0.5 * 1 / 6, abs=1e-4)


def test_parecida_pelo_som_com_um_dislike_e_penalizada(con):
    from curator.audio.analisador import Resultado
    from curator.audio.persistir import Origem, gravar_analise
    from curator.banco.conexao import agora

    con.execute("UPDATE faixas SET avaliacao = 'dislike' WHERE video_id = 'ambient00'")
    e_dis = np.frombuffer(
        con.execute(
            "SELECT dados FROM vetores WHERE video_id = 'ambient00' AND tipo = 'effnet_emb'"
        ).fetchone()[0],
        np.float32,
    ).copy()
    for vid in ("perto", "longe"):
        con.execute(
            "INSERT INTO faixas (video_id, titulo, criada_em, atualizada_em) VALUES (?, ?, ?, ?)",
            (vid, vid, agora(), agora()),
        )
        e = e_dis if vid == "perto" else -e_dis
        gravar_analise(
            con, vid, Resultado({}, {"effnet_emb": e}, {}, 200.0, {}), Origem("x", vid), "1", "h", 1.0
        )
    f = rr.fatores_dislike(con, ["perto", "longe"], carregar_config())
    assert f["perto"] < 1 and "longe" not in f
