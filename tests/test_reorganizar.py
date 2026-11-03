import pytest

pytest.importorskip("umap")
pytest.importorskip("sklearn")

from curator.motor import clusters as cl  # noqa: E402
from curator.motor import reorganizar  # noqa: E402
from tests.falso_motor import biblioteca_analisada, playlist  # noqa: E402

P = cl.Parametros(min_cluster_size=8, n_neighbors=10)


@pytest.fixture
def con(tmp_path):
    return biblioteca_analisada(tmp_path / "c.db", por_estilo=25)


def test_reorganizar_completa_cria_e_sugere_fusao(con):
    playlist(con, "PLrock", "Rock", [f"rock{i:02d}" for i in range(15)])
    playlist(con, "PLrock2", "Rock de novo", [f"rock{i:02d}" for i in range(14)])
    cs = cl.calcular(con, p=P)
    sug = reorganizar.sugerir(con, cs)
    tipos = [s.tipo for s in sug]
    assert tipos.count("criar") == 2  # ambient e rap não têm playlist
    completar = [s for s in sug if s.tipo == "completar"]
    assert len(completar) == 1 and completar[0].operacoes[0].playlist in ("PLrock", "PLrock2")
    assert all(v.startswith("rock") for v in completar[0].operacoes[0].video_ids)
    assert any(s.tipo == "fundir" and "Rock de novo" in s.texto for s in sug)


def test_reorganizar_sugere_divisao(con):
    playlist(
        con,
        "PLmisturada",
        "Misturada",
        [f"rock{i:02d}" for i in range(10)] + [f"ambient{i:02d}" for i in range(10)],
    )
    sug = reorganizar.sugerir(con, cl.calcular(con, p=P))
    assert any(s.tipo == "dividir" and "Misturada" in s.texto for s in sug)
