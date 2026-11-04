"""#63: candidatas e histórico de recomendações no banco, sempre fora da biblioteca."""

import json

import pytest

from curator.banco import consultas
from curator.banco.conexao import agora
from curator.config import caminhos
from curator.motor import recomendacoes as rc
from curator.motor.executor import executar
from curator.motor.receita import carregar
from tests.falso_motor import biblioteca_analisada, playlist


@pytest.fixture
def con():
    c = biblioteca_analisada(caminhos().banco, por_estilo=4)
    for v in ("novo1", "novo2", "novo3"):
        c.execute(
            "INSERT INTO faixas (video_id, titulo, artistas, criada_em, atualizada_em) VALUES (?, ?, ?, ?, ?)",
            (v, v.title(), json.dumps([{"id": "UC_x", "nome": "Outro"}]), agora(), agora()),
        )
    return c


def recomendar(con, ids, **kw):
    itens = [rc.ItemRecomendado(v, 1.0 - n / 10, ["yt_radio"], 1) for n, v in enumerate(ids)]
    cid = rc.salvar_coleta(con, "yt_radio", "rock00", ids)
    return rc.salvar(con, modo="rapido", escopo="novas", spec={"x": 1}, sementes=["rock00"], coletas=[cid],
                     itens=itens, **kw)  # fmt: skip


def status(con, rid):
    return {i["video_id"]: i["status"] for i in rc.itens(con, rid)}


def test_candidatas_nunca_entram_na_biblioteca(con):
    recomendar(con, ["novo1", "novo2"])
    assert not {"novo1", "novo2"} & set(consultas.biblioteca(con))
    assert not {"novo1", "novo2"} & set(consultas.nao_alocadas(con))
    ex = executar(con, carregar({"nome": "x", "tamanho": 100}))
    assert not {"novo1", "novo2"} & set(ex.ids)


def test_status_dos_itens_acompanha_a_sincronizacao(con):
    rid = recomendar(con, ["novo1", "novo2", "novo3"])
    assert set(status(con, rid).values()) == {"sem_reacao"}
    con.execute("UPDATE faixas SET curtida = 1, avaliacao = 'like' WHERE video_id = 'novo1'")
    con.execute("UPDATE faixas SET avaliacao = 'dislike' WHERE video_id = 'novo2'")
    playlist(con, "PLx", "Minha", ["novo3"])
    assert status(con, rid) == {"novo1": "curtida", "novo2": "dislike", "novo3": "em_playlist"}
    assert "novo1" in consultas.biblioteca(con)  # curtida depois: agora é da biblioteca
    assert rc.listar(con)[0]["status_itens"] == {
        "curtida": 1,
        "dislike": 1,
        "em_playlist": 1,
        "sem_reacao": 0,
    }


def test_playlist_de_descobertas_nao_conta_como_biblioteca(con):
    playlist(con, "PLdesc", "Descobertas · rock", ["novo1", "rock00"])
    con.execute("UPDATE playlists SET descobertas = 1 WHERE playlist_id = 'PLdesc'")
    rid = recomendar(con, ["novo1"])
    assert "novo1" not in consultas.biblioteca(con)
    assert status(con, rid) == {"novo1": "sem_reacao"}


def test_historico_conta_recomendacoes_anteriores(con):
    recomendar(con, ["novo1", "novo2"])
    recomendar(con, ["novo1"])
    h = rc.historico(con)
    assert h["novo1"].vezes == 2 and h["novo2"].vezes == 1
    assert rc.historico(con, antes_de="2000") == {}


def test_coleta_recente_respeita_o_cache(con):
    cid = rc.salvar_coleta(con, "yt_radio", "rock00", ["novo1"])
    assert rc.coleta_recente(con, "yt_radio", "rock00", cache_h=1) == cid
    con.execute("UPDATE coletas SET coletada_em = '2000-01-01T00:00:00Z'")
    assert rc.coleta_recente(con, "yt_radio", "rock00", cache_h=1) is None
    assert rc.ler_coletas(con, [cid])[0]["itens"] == ["novo1"]


def test_faixa_recomendada_nao_pode_ser_apagada(con):
    import sqlite3

    recomendar(con, ["novo1"])
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM faixas WHERE video_id = 'novo1'")
