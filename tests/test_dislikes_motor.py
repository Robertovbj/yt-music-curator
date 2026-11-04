"""#61: faixa com dislike é filtro duro em tudo o que monta lista, e continua guardada com a análise."""

import pytest

from curator.banco import consultas
from curator.config import caminhos
from curator.motor import classificar as cl
from curator.motor import clusters, espacos, planos
from curator.motor.executor import executar
from curator.motor.receita import carregar
from tests.falso_motor import biblioteca_analisada, playlist


@pytest.fixture
def con():
    c = biblioteca_analisada(caminhos().banco)
    playlist(c, "PLrock", "Rock", [f"rock{i:02d}" for i in range(8)])
    playlist(c, "PLcalma", "Calma", [f"ambient{i:02d}" for i in range(8)])
    return c


def dislike(con, *ids):
    con.executemany("UPDATE faixas SET avaliacao = 'dislike' WHERE video_id = ?", [(v,) for v in ids])


RECEITA = {"nome": "energia", "tamanho": 50, "perfil_alvo": {"energy": {"alvo": 0.9}}}


@pytest.mark.parametrize("universo", ["biblioteca", "curtidas", "PLrock"])
def test_receita_nunca_escolhe_dislike_em_nenhum_universo(con, universo):
    escolhida = executar(con, carregar({**RECEITA, "universo": universo})).ids[0]
    dislike(con, escolhida)
    ex = executar(con, carregar({**RECEITA, "universo": universo}))
    assert ex.ids and escolhida not in ex.ids
    assert ex.descartes["dislike"] == 1


def test_mesma_receita_mesmo_banco_mesmo_resultado(con):
    dislike(con, "rock00", "rap03")
    r = carregar(RECEITA)
    assert executar(con, r).ids == executar(con, r).ids


def test_classificacao_ignora_dislikes_nas_decisoes_e_nos_centroides(con):
    dislike(con, "rock11", "rock01")
    c = cl.classificar(con)
    ids = [d.video_id for d in c.decisoes]
    assert "rock11" not in ids and "rock10" in ids
    ps, _ = cl.perfis(con)
    assert "rock01" not in ps["PLrock"].membros


def test_clusters_e_mapa_ignoram_dislikes(con):
    dislike(con, "ambient00")
    assert "ambient00" not in clusters._universo(con, "biblioteca")
    assert "ambient00" not in clusters._universo(con, "PLcalma")
    assert "ambient00" not in espacos.da_biblioteca(con)
    assert "ambient00" in espacos.analisadas(con)  # a análise continua lá


def test_plano_sugere_tirar_dislikes_das_playlists(con):
    dislike(con, "rock02", "ambient03", "rap00")  # rap00 não está em playlist nenhuma
    assert consultas.dislikes_em_playlists(con) == {"PLcalma": ["ambient03"], "PLrock": ["rock02"]}
    meta, ops = planos.obter(con, planos.de_dislikes(con))
    assert meta["status"] == "pendente" and meta["origem"] == "dislikes"
    assert {(o.tipo, o.playlist, tuple(o.video_ids)) for o in ops} == {
        ("remover", "PLcalma", ("ambient03",)),
        ("remover", "PLrock", ("rock02",)),
    }


def test_plano_de_dislikes_vazio(con):
    with pytest.raises(ValueError):
        planos.de_dislikes(con)
