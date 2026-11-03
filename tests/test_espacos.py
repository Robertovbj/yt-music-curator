import time

import numpy as np
import pytest

from curator.motor import espacos
from tests.falso_motor import biblioteca_analisada


@pytest.fixture
def con(tmp_path):
    return biblioteca_analisada(tmp_path / "c.db")


def test_embeddings_normalizados_e_ordenados(con):
    m = espacos.embeddings(con)
    assert len(m.ids) == 36 and m.ids == sorted(m.ids)
    assert np.allclose(np.linalg.norm(m.X, axis=1), 1, atol=1e-5)


def test_vizinhos_do_mesmo_estilo(con):
    m = espacos.embeddings(con)
    viz = espacos.vizinhos(m, m.linha("rock00"), k=5, excluir={"rock00"})
    assert [v for v, _ in viz] and all(v.startswith("rock") for v, _ in viz)
    assert viz == espacos.vizinhos(m, m.linha("rock00"), k=5, excluir={"rock00"})  # determinístico


def test_empate_resolvido_por_id():
    m = espacos.Matriz(["b", "a", "c"], espacos.normalizar_l2(np.array([[1, 0], [1, 0], [0, 1]], np.float32)))
    assert [v for v, _ in espacos.vizinhos(m, np.array([1, 0]), k=2)] == ["a", "b"]


def test_interpretavel_pondera_grupos(con):
    m = espacos.interpretavel(con)
    assert len(m.ids) == 36
    assert m.colunas[0] == "escalares:danceability" and "discogs400:Hip Hop---Trap" in m.colunas
    n_esc = len(espacos.ESCALARES)
    esc, gen = m.X[:, :n_esc], m.X[:, n_esc:]
    # cada grupo contribui ~peso² para a norma ao quadrado média, independente da dimensão
    assert np.mean(np.sum(esc**2, axis=1)) <= 1.0 + 1e-3
    assert np.mean(np.sum(gen**2, axis=1)) <= 1.0 + 1e-3


def test_hash_biblioteca_muda_com_nova_analise(con):
    h = espacos.hash_biblioteca(con)
    con.execute("DELETE FROM analises WHERE video_id = 'rock00'")
    assert espacos.hash_biblioteca(con) != h


def test_zscore_trata_nan_e_desvio_zero():
    Z, media, desvio = espacos.zscore(np.array([[1, 5, np.nan], [3, 5, 2]], np.float32))
    assert np.allclose(Z[:, 0], [-1, 1]) and np.allclose(Z[:, 1], 0) and Z[0, 2] == 0


def test_top10_rapido_para_10_mil_faixas():
    rng = np.random.default_rng(0)
    m = espacos.Matriz(
        [f"v{i:05d}" for i in range(10_000)],
        espacos.normalizar_l2(rng.standard_normal((10_000, 1280)).astype(np.float32)),
    )
    espacos.vizinhos(m, m.X[0], k=10)  # aquece
    t = time.perf_counter()
    for i in range(20):
        espacos.vizinhos(m, m.X[i], k=10)
    assert (time.perf_counter() - t) / 20 < 0.05
