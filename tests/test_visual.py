import json
import re

import pytest

pytest.importorskip("umap")
pytest.importorskip("plotly")

from typer.testing import CliRunner  # noqa: E402

from curator.config import caminhos  # noqa: E402
from curator.interfaces.cli import app  # noqa: E402
from curator.visual import html  # noqa: E402
from tests.falso_motor import biblioteca_analisada, playlist  # noqa: E402


@pytest.fixture
def con():
    con = biblioteca_analisada(caminhos().banco, por_estilo=12)
    playlist(con, "PLcalma", "Calma", [f"ambient{i:02d}" for i in range(6)])
    playlist(con, "PLrock", "Rock", [f"rock{i:02d}" for i in range(6)] + ["ambient00"])
    return con


def test_dados_do_visualizador(con):
    d = html.dados(con)
    assert len(d["faixas"]) == 36 and d["playlists"] == ["Calma", "Rock"]
    f = next(x for x in d["faixas"] if x["id"] == "ambient00")
    assert sorted(d["playlists"][k] for k in f["pl"]) == ["Calma", "Rock"]  # em duas playlists (contorno)
    assert f["e"] == "Electronic · Ambient" and d["generos"][f["g"]] == "Electronic"
    assert next(x for x in d["faixas"] if x["id"] == "rap05")["na"] is True
    assert len(d["coords"]["effnet"]["2"]) == 36 and len(d["coords"]["effnet"]["3"][0]) == 3
    assert d["coords"]["musicnn"]["2"] == [None] * 36  # a biblioteca de teste não tem MusiCNN
    viz = d["vizinhos"][0]
    assert len(viz) == html.K_VIZINHOS and 0 not in viz


def test_html_autocontido(con, tmp_path):
    destino = html.gerar(con, tmp_path / "mapa.html")
    texto = destino.read_text(encoding="utf-8")
    assert "<title>Mapa da biblioteca</title>" in texto
    assert not re.search(r"<script[^>]+src=", texto)  # nada externo: abre offline
    assert "Plotly" in texto and texto.count("__DADOS__") == 0
    bloco = re.search(r'<script type="application/json" id="dados">(.*?)</script>', texto, re.S)[1]
    assert len(json.loads(bloco.replace("<\\/", "</"))["faixas"]) == 36


def test_cli_reorganizar_e_visualizar(con, tmp_path):
    run = CliRunner().invoke
    r = run(app, ["reorganizar", "--min-cluster", "6", "--plano"])
    assert r.exit_code == 0, r.output
    assert "agrupamento #1" in r.output and "criar" in r.output and "plano #1 criado" in r.output
    r = run(app, ["reorganizar", "--min-cluster", "6"])  # reaproveita o agrupamento salvo
    assert "agrupamento #1" in r.output
    r = run(app, ["clusters", "renomear", "0", "Meu cluster"])
    assert r.exit_code == 0
    r = run(app, ["visualizar", "--saida", str(tmp_path / "m.html")])
    assert r.exit_code == 0 and (tmp_path / "m.html").exists()
    assert "Meu cluster" in (tmp_path / "m.html").read_text(encoding="utf-8")
