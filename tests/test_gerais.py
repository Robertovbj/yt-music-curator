"""#80: playlists gerais a partir de todas as curtidas, sem olhar as playlists existentes."""

import json

import numpy as np
import pytest

pytest.importorskip("umap")

from curator.audio.analisador import Resultado  # noqa: E402
from curator.audio.persistir import Origem, gravar_analise  # noqa: E402
from curator.banco.conexao import abrir  # noqa: E402
from curator.coleta import aplicar as ap  # noqa: E402
from curator.coleta.cliente import ClienteYT  # noqa: E402
from curator.config import caminhos  # noqa: E402
from curator.motor import classificar as cl  # noqa: E402
from curator.motor import clusters, espacos, gerais  # noqa: E402
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
    assert sorted(clusters.nomes_unicos(cs).values()) == [
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


# ------------------------------------------------------------------ playlists master (#105)

M = gerais.parametros(quantas=3)


def test_masters_tem_n_exatas_e_cobrem_todas_as_curtidas(con):
    aid, cs, _ = gerais.obter(con, M)
    assert len(cs) == 3
    universo = set(clusters._universo(con, clusters.CURTIDAS))
    cobertas = {v for c in cs for v in c.membros}
    assert cobertas == universo
    nucleos = [set(c.nucleo) for c in cs]
    assert sum(map(len, nucleos)) == len(universo)  # o núcleo é uma partição: cada faixa numa master só
    for c in cs:  # e a fronteira só acrescenta, no máximo uma segunda master
        assert set(c.nucleo) <= set(c.membros)
    contagem = {v: sum(v in c.membros for c in cs) for v in universo}
    assert set(contagem.values()) <= {1, 2}
    # os três estilos sintéticos viram as três masters
    assert sorted({v[:-2] for v in c.nucleo} == {c.nucleo[0][:-2]} for c in cs) == [True] * 3


def test_masters_deterministicas_e_reaproveitadas(con):
    a, cs, _ = gerais.obter(con, M)
    assert gerais.obter(con, M)[0] == a  # mesmos parâmetros: não recalcula
    b, cs2, _ = gerais.obter(con, M, reagrupar=True)
    assert b != a and [c.membros for c in cs] == [c.membros for c in cs2]
    assert gerais.obter(con, gerais.parametros(quantas=3, fronteira=0.0))[0] not in (a, b)


def test_fronteira_zero_so_quem_esta_mais_perto_da_outra(con):
    _, cs, _ = gerais.obter(con, gerais.parametros(quantas=3, fronteira=0.0))
    C = np.vstack([c.centroide for c in cs])
    emb = espacos.embeddings(con)
    for c in cs:
        for v in set(c.membros) - set(c.nucleo):
            sims = C @ emb.linha(v)
            propria = next(i for i, d in enumerate(cs) if v in d.nucleo)
            assert sims[c.cluster_id] >= sims[propria]


def test_curtida_nova_entra_na_master_mais_proxima(con):
    aid, cs, _ = gerais.obter(con, M)
    vec = espacos.embeddings(con).linha("rap03")
    con.execute(
        "INSERT INTO faixas (video_id, titulo, artistas, curtida, criada_em, atualizada_em) "
        "VALUES ('novo', 'Novo', '[]', 1, 'x', 'x')"
    )
    gravar_analise(con, "novo", Resultado({}, {"effnet_emb": vec}, {}, 200.0, {}), Origem("youtube_music", "novo"),
                   "1", "h", 1.0)  # fmt: skip
    a2, cs2, novos = gerais.obter(con, M)
    assert a2 == aid and novos >= 1
    casa = next(c for c in cs2 if "rap03" in c.nucleo)
    assert "novo" in casa.membros and sum("novo" in c.membros for c in cs2) <= 2
    assert gerais.obter(con, M)[2] == 0  # idempotente


def test_parametros_invalidos():
    for kw in ({"quantas": 1}, {"quantas": 50}, {"quantas": 6, "fronteira": 0.5}):
        with pytest.raises(ValueError):
            gerais.parametros(**kw)
