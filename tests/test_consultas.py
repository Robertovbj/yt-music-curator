import pytest
from typer.testing import CliRunner

from curator.banco import consultas, hierarquia
from curator.config import caminhos
from curator.interfaces.cli import app
from tests.falso_yt import HIERARQUIA_ROCK, biblioteca_exemplo


@pytest.fixture
def con(tmp_path):
    con = biblioteca_exemplo(caminhos().banco)
    arq = tmp_path / "h.toml"
    arq.write_text(HIERARQUIA_ROCK)
    hierarquia.aplicar(con, hierarquia.ler(arq))
    return con


def test_consultas(con):
    assert consultas.biblioteca(con) == list("abcdefx")
    assert consultas.nao_alocadas(con) == ["e", "f"]
    assert consultas.exclusivas_da_mae(con, "PLall") == ["c"]
    est = consultas.estatisticas(con)
    assert est["biblioteca"] == 7 and est["curtidas"] == 6 and est["nao_alocadas"] == 2
    assert est["analise"] == {"sem análise": 7}
    assert {"titulo": "Rock - Arena", "tipo": "folha", "mae": "Rock - All", "faixas": 2} in est["playlists"]


def test_cli_estatisticas(con):
    r = CliRunner().invoke(app, ["estatisticas"])
    assert r.exit_code == 0
    assert "biblioteca: 7 faixas" in r.output and "não alocadas: 2" in r.output
    assert "Rock - Arena  ← Rock - All" in r.output
