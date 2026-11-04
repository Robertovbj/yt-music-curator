"""#80: playlists gerais a partir de todas as curtidas, sem olhar as playlists existentes."""

import json

import pytest

pytest.importorskip("umap")

from curator.banco.conexao import abrir  # noqa: E402
from curator.coleta import aplicar as ap  # noqa: E402
from curator.coleta.cliente import ClienteYT  # noqa: E402
from curator.config import caminhos  # noqa: E402
from curator.motor import classificar as cl  # noqa: E402
from curator.motor import clusters, gerais  # noqa: E402
from tests.falso_motor import biblioteca_analisada, playlist  # noqa: E402
from tests.falso_yt import FalsoYT  # noqa: E402

P = gerais.parametros(6)


@pytest.fixture
def con():
    c = biblioteca_analisada(caminhos().banco, por_estilo=14)
    playlist(c, "PLminha", "Minha lógica", ["rock00", "ambient00", "rap00"])  # uma playlist "por jogo"
    c.execute("UPDATE playlists SET snapshot_hash = 'x'")
    return c


def test_agrupa_so_as_curtidas_e_o_plano_so_cria(con):
    con.execute(
        "UPDATE faixas SET curtida = 0 WHERE video_id = 'rock00'"
    )  # está na "Minha lógica", mas não curtida
    aid, cs, _ = gerais.obter(con, P)
    membros = {v for c in cs for v in c.membros}
    assert membros and "rock00" not in membros
    pid = gerais.plano(con, aid, cs)
    diff = json.loads(con.execute("SELECT diff FROM planos WHERE plano_id = ?", (pid,)).fetchone()[0])
    assert {o["tipo"] for o in diff} == {"criar", "adicionar"}
    assert all(o["playlist"].startswith("nova:") for o in diff)  # nenhuma playlist existente é tocada
    assert len({o["playlist"] for o in diff}) == len(cs)  # nomes repetidos viram playlists distintas


def test_reaproveita_e_recalcula_com_outros_parametros(con):
    a, _, _ = gerais.obter(con, P)
    assert gerais.obter(con, P)[0] == a
    assert gerais.obter(con, gerais.parametros(8))[0] != a
    salvos = json.loads(
        con.execute("SELECT parametros FROM agrupamentos WHERE agrupamento_id = ?", (a,)).fetchone()[0]
    )
    assert salvos["selecao"] == "leaf" and clusters.ultimo_geral(con) is not None


def test_nomes_repetidos_sao_numerados():
    cs = [
        clusters.Cluster(i, [], None, 0, {f"v{i}{j}": 1.0 for j in range(3 - i)}, "Rock · calmo")
        for i in range(3)
    ]
    assert sorted(gerais._nomes_unicos(cs).values()) == [
        "Rock · calmo",
        "Rock · calmo (2)",
        "Rock · calmo (3)",
    ]


def test_aplicado_marca_as_gerais_e_classificar_usa_elas(con):
    aid, cs, _ = gerais.obter(con, P)
    pid = gerais.plano(con, aid, cs)
    yt = FalsoYT()
    res = ap.aplicar(abrir(caminhos().banco), ClienteYT(yt, intervalo_s=0, dormir=lambda s: None), pid)
    assert res.status == "aplicado", res.log
    assert sorted(gerais.playlists(con)) == sorted(res.criadas.values())
    assert con.execute("SELECT geral FROM playlists WHERE playlist_id = 'PLminha'").fetchone()[0] == 0
    # uma curtida nova entra: vai para uma geral, nunca para a playlist "Minha lógica"
    con.execute(
        "DELETE FROM playlist_faixas WHERE video_id = 'rock05' AND playlist_id IN (SELECT playlist_id FROM playlists WHERE geral = 1)"
    )
    c = cl.classificar(con, nas_gerais=True)
    assert [d.video_id for d in c.decisoes] == ["rock05"]
    assert {x.playlist_id for x in c.decisoes[0].destinos} <= set(gerais.playlists(con))
