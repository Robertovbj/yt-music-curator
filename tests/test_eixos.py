"""#57: o que cada eixo do mapa acompanha, orientação estável e a PCA do espaço interpretável."""

import numpy as np
import pytest

from curator.motor import espacos
from curator.visual import eixos
from tests.falso_motor import biblioteca_analisada


@pytest.fixture
def con(tmp_path):
    return biblioteca_analisada(tmp_path / "c.db", por_estilo=12)


@pytest.fixture
def refs(con):
    return eixos.referencias(con, espacos.da_biblioteca(con))


def test_rotulo_aponta_para_a_feature_conhecida_com_o_sinal_certo(refs):
    Y = np.column_stack([refs["energia"], -refs["BPM"]])
    e1, e2 = eixos.correlacoes(Y, refs)
    assert e1.rotulo.startswith("eixo 1: ↑ energia") and e1.correlacoes[0] == ("energia", 1.0)
    assert e2.correlacoes[0] == ("BPM", -1.0) and "↓ BPM" in e2.rotulo
    assert "(ρ 1,00)" in e1.rotulo
    assert {n for n, _ in e1.correlacoes} >= {"gênero Rock", "gênero Electronic", "acústico"}


def test_eixo_sem_relacao_e_sem_direcao_clara(refs):
    (e,) = eixos.correlacoes(np.zeros((len(refs["energia"]), 1)), refs)
    assert e.rotulo == "eixo 1: sem direção clara"


def test_orientar_desfaz_giro_e_espelho_e_cresce_com_a_energia(refs):
    rng = np.random.default_rng(1)
    Y0 = np.column_stack([refs["energia"] * 10, rng.normal(0, 1, len(refs["energia"]))])
    a = np.deg2rad(37)
    giro = np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]])
    torto = (Y0 @ giro.T) * np.array([-1, 1]) + 5  # girado, espelhado e deslocado
    o1, o2 = eixos.orientar(Y0, refs["energia"]), eixos.orientar(torto, refs["energia"])
    assert np.allclose(o1, o2, atol=1e-4)
    assert eixos.spearman(o1[:, 0].astype(float), refs["energia"]) > 0.9


def test_pca_interpretavel_com_pesos_variancia_e_deterministica(con):
    ids = espacos.da_biblioteca(con)
    m, ex = eixos.pca_interpretavel(con, ids, 3)
    m2, ex2 = eixos.pca_interpretavel(con, ids, 3)
    assert (
        m.X.shape == (36, 3)
        and np.array_equal(m.X, m2.X)
        and [e.como_dict() for e in ex] == [e.como_dict() for e in ex2]
    )
    assert ex[0].variancia > ex[1].variancia > ex[2].variancia > 0
    for e in ex:
        assert len(e.pesos) == eixos.MAX_PESOS and e.pesos[0][1] > 0  # o maior peso sai positivo
        assert e.rotulo.startswith(f"PC{ex.index(e) + 1} (") and "% da variância)" in e.rotulo


def test_projecao_guarda_os_eixos_e_regera_igual(con):
    pytest.importorskip("umap")
    from curator.visual import projecoes

    a = projecoes.projetar(con, "effnet", 2)
    rot = projecoes.eixos(con, "effnet", 2)
    assert len(rot) == 2 and all(r["rotulo"].startswith(f"eixo {k + 1}:") for k, r in enumerate(rot))
    assert len(rot[0]["correlacoes"]) == len(eixos.referencias(con, a.ids))
    con.execute("DELETE FROM projecao_eixos")  # cache de antes da #57: sem eixos, a projeção é refeita
    b = projecoes.projetar(con, "effnet", 2)
    assert np.allclose(a.X, b.X, atol=1e-5) and projecoes.eixos(con, "effnet", 2) == rot
    con.execute("DELETE FROM analises WHERE video_id = 'rock00'")
    assert projecoes.eixos(con, "effnet", 2) == []  # a biblioteca mudou: os eixos guardados não valem mais
