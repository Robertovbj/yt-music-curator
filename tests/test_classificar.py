import numpy as np
import pytest

from curator.motor import classificar as cl
from tests.falso_motor import biblioteca_analisada, playlist


@pytest.fixture
def con(tmp_path):
    con = biblioteca_analisada(tmp_path / "c.db", por_estilo=14)
    # "Pesado" é agregadora de "Rock" e "Rap"; "Calma" fica na raiz. As faixas 10–13 de cada estilo ficam soltas.
    playlist(
        con,
        "PLpesado",
        "Pesado",
        [f"rock{i:02d}" for i in range(10)] + [f"rap{i:02d}" for i in range(10)],
        tipo="agregadora",
    )
    playlist(con, "PLrock", "Rock", [f"rock{i:02d}" for i in range(10)], mae="PLpesado")
    playlist(con, "PLrap", "Rap", [f"rap{i:02d}" for i in range(10)], mae="PLpesado")
    playlist(con, "PLcalma", "Calma", [f"ambient{i:02d}" for i in range(10)])
    playlist(con, "PLpouca", "Quase vazia", ["rock00", "rap00"])
    return con


def destinos(c, vid):
    return [d.playlist_id for d in next(x for x in c.decisoes if x.video_id == vid).destinos]


def test_nao_alocadas_vao_para_a_familia_certa(con):
    c = cl.classificar(con)
    assert len(c.decisoes) == 12  # 4 soltas × 3 estilos
    familia = {"rock": {"PLrock", "PLpesado"}, "rap": {"PLrap", "PLpesado"}, "ambient": {"PLcalma"}}
    folha = {"rock": "PLrock", "rap": "PLrap", "ambient": "PLcalma"}
    na_folha = 0
    for d in c.decisoes:
        estilo = d.video_id.rstrip("0123456789")
        ids = {x.playlist_id for x in d.destinos}
        assert ids <= familia[estilo]  # nunca no estilo errado
        if folha[estilo] in ids:
            na_folha += 1
        elif not ids:  # abaixo de todos os limiares: a própria folha é a sugestão mais próxima
            assert d.proximas[0].playlist_id == folha[estilo]
    # o limiar no percentil 10 deixa alguns legítimos de fora por pouco (com 10 membros, margem ~0,01)
    assert na_folha >= len(c.decisoes) // 2


def test_quem_nao_passa_na_filha_fica_na_mae(con):
    for d in cl.classificar(con).decisoes:
        if d.video_id.startswith("rap") and "PLrap" not in {x.playlist_id for x in d.destinos}:
            assert [x.playlist_id for x in d.destinos] in ([], ["PLpesado"])


def test_playlist_com_poucos_membros_fica_de_fora(con):
    _, frias = cl.perfis(con)
    assert ("Quase vazia", 2) in frias


def test_deterministico(con):
    a, b = cl.classificar(con), cl.classificar(con)
    assert [(d.video_id, [(x.playlist_id, x.score) for x in d.destinos]) for d in a.decisoes] == [
        (d.video_id, [(x.playlist_id, x.score) for x in d.destinos]) for d in b.decisoes
    ]


def test_crossover_entra_em_duas_filhas(con):
    """Duas filhas de estilo parecido: a faixa que cabe nas duas entra nas duas (multi-rótulo)."""
    playlist(con, "PLguitarra", "Guitarra", [f"rock{i:02d}" for i in range(8)], mae="PLpesado")
    c = cl.classificar(con, ids=[f"rock{i:02d}" for i in range(10, 14)], percentil=1)
    assert any({"PLrock", "PLguitarra"} <= set(destinos(c, v)) for v in [d.video_id for d in c.decisoes])


def test_sem_destino_sugere_as_mais_proximas(con):
    # restringe a busca às filhas de "Pesado": uma faixa de ambient não cabe em nenhuma
    c = cl.classificar(con, ids=["ambient12"], mae="PLpesado")
    d = c.decisoes[0]
    assert d.destinos == [] and {p.playlist_id for p in d.proximas} == {"PLrock", "PLrap"}


def test_limiar_loo_e_um_percentil_baixo_dos_membros(con):
    ps, _ = cl.perfis(con)
    p = ps["PLrock"]
    assert -1 <= p.limiar <= 1
    # quase todos os membros (≥ 90% pela construção) passam no próprio limiar
    from curator.motor.classificar import _centroide, _score

    passam = sum(
        _score(p.E[i], np.delete(p.E, i, 0), _centroide(np.delete(p.E, i, 0)), cl.K, cl.ALFA) >= p.limiar
        for i in range(len(p.E))
    )
    assert passam >= 0.9 * len(p.E)


def test_avaliacao_leave_one_out(con):
    m = {x.titulo: x for x in cl.avaliar(con)}
    assert m["Rock"].precisao == 1.0 and m["Rock"].revocacao >= 0.8
    assert m["Calma"].precisao == 1.0
