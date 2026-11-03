import pytest

pytest.importorskip("umap")

from curator.visual import projecoes  # noqa: E402
from tests.falso_motor import biblioteca_analisada  # noqa: E402


@pytest.fixture
def con(tmp_path):
    return biblioteca_analisada(tmp_path / "c.db", por_estilo=12)


def test_projecao_em_cache_ate_a_biblioteca_mudar(con):
    a = projecoes.projetar(con, "effnet", 2)
    assert a.X.shape == (36, 2)
    n = con.execute("SELECT count(*) FROM projecoes WHERE espaco = 'effnet' AND dims = 2").fetchone()[0]
    assert n == 36
    b = projecoes.projetar(con, "effnet", 2)  # vem do cache: idêntico
    assert (a.X == b.X).all()
    con.execute("DELETE FROM analises WHERE video_id = 'rock00'")
    c = projecoes.projetar(con, "effnet", 2)
    assert c.X.shape == (35, 2)
