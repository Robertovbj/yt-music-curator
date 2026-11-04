"""#68: comandos separados para novas, curtidas e misturar; recomendações nunca caem nas playlists das
curtidas sem pedido explícito."""

import pytest
from typer.testing import CliRunner

from curator.banco import consultas
from curator.banco.conexao import abrir
from curator.coleta import aplicar as ap
from curator.coleta.cliente import ClienteYT
from curator.config import caminhos
from curator.interfaces.cli import app
from curator.interfaces.comandos import recomendar as cmd
from curator.motor import planos
from curator.motor import recomendacoes as rc
from tests.falso_motor import biblioteca_analisada, playlist
from tests.falso_yt import FalsoYT, faixa

runner = CliRunner()


@pytest.fixture
def yt(monkeypatch):
    y = FalsoYT()
    y.radios = {"rock00": [faixa("n1", artistas=("A",)), faixa("rock02"), faixa("n2", artistas=("B",)),
                           faixa("rock03"), faixa("n3", artistas=("C",))]}  # fmt: skip
    monkeypatch.setattr(
        cmd, "_clientes", lambda fontes: (ClienteYT(y, intervalo_s=0, dormir=lambda s: None), None)
    )
    return y


@pytest.fixture
def con():
    c = biblioteca_analisada(caminhos().banco, por_estilo=6)
    playlist(c, "PLrock", "Rock", [f"rock{i:02d}" for i in range(6)])
    c.execute("UPDATE playlists SET snapshot_hash = 'x'")
    return c


def rodar(*args):
    return runner.invoke(app, ["recomendar", *args, "--fonte", "yt_radio"])


def ultima(con):
    return rc.itens(con, rc.listar(con, 1)[0]["recomendacao_id"])


def test_rapido_so_traz_novas(con, yt):
    r = rodar("rapido", "-f", "rock00", "--sim")
    assert r.exit_code == 0, r.output
    assert "só músicas NOVAS" in r.output and "RÁPIDA" in r.output
    ids = [i["video_id"] for i in ultima(con)]
    assert ids and not set(ids) & set(consultas.biblioteca(con))


def test_misturar_so_com_a_flag(con, yt):
    r = rodar("rapido", "-f", "rock00", "--misturar-curtidas", "--sim")
    assert r.exit_code == 0, r.output
    assert "MISTURANDO" in r.output
    assert any(i["conhecida"] for i in ultima(con)) and any(not i["conhecida"] for i in ultima(con))


def test_sem_modo_nao_recomenda(con, yt):
    r = runner.invoke(app, ["recomendar", "-f", "rock00"])
    assert r.exit_code != 0 and yt.chamadas["get_watch_playlist"] == 0


def test_sem_terminal_e_sem_sim_nao_executa(con, yt):
    r = rodar("rapido", "-f", "rock00")
    assert r.exit_code != 0 and "--sim" in r.output
    assert yt.chamadas["get_watch_playlist"] == 0 and rc.listar(con) == []


def test_criar_playlist_gera_plano_de_playlist_nova(con, yt):
    r = rodar("rapido", "-f", "rock00", "--criar-playlist", "--nome", "Descobertas · rock", "--sim")
    assert r.exit_code == 0, r.output
    meta, ops = planos.obter(con, planos.listar(con)[0]["plano_id"])
    assert meta["origem"].startswith("recomendacao:")
    assert [o.tipo for o in ops] == ["criar", "adicionar"] and ops[0].titulo == "Descobertas · rock"


def test_recomendacao_numa_playlist_das_curtidas_exige_flag(con, yt):
    r = rodar("rapido", "-f", "rock00", "--adicionar-em", "Rock", "--sim")
    assert r.exit_code != 0 and "--permitir-novas" in r.output
    assert planos.listar(con) == []
    rid = rc.listar(con, 1)[0]["recomendacao_id"]
    with pytest.raises(planos.NovasNaBiblioteca):
        planos.de_recomendacao(con, rid, destino="PLrock")
    assert planos.de_recomendacao(con, rid, destino="PLrock", permitir_novas=True)


def test_receita_continua_so_na_biblioteca(con, yt):
    rodar("rapido", "-f", "rock00", "--sim")
    r = runner.invoke(app, ["receita", "criar", "-"], input='{"nome": "tudo", "tamanho": 200}')
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["receita", "executar", "tudo", "--json"])
    assert not {"n1", "n2", "n3"} & set(r.output.split('"'))


def test_playlist_de_descobertas_aplicada_fica_marcada_e_fora_da_biblioteca(con, yt):
    rodar("rapido", "-f", "rock00", "--criar-playlist", "--sim")
    pid = planos.listar(con)[0]["plano_id"]
    res = ap.aplicar(abrir(caminhos().banco), ClienteYT(yt, intervalo_s=0, dormir=lambda s: None), pid)
    assert res.status == "aplicado", res.log
    nova = next(iter(res.criadas.values()))
    assert con.execute("SELECT descobertas FROM playlists WHERE playlist_id = ?", (nova,)).fetchone()[0] == 1
    assert "n1" not in consultas.biblioteca(con)
