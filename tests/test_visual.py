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
    assert [len(d["eixos"]["effnet"][k]) for k in ("2", "3")] == [2, 3] and d["eixos"]["musicnn"]["2"] == []
    assert d["eixos"]["effnet"]["2"][0]["rotulo"].startswith("eixo 1:")
    assert len(d["coords"]["pca"]["2"]) == 36 and None not in d["coords"]["pca"]["3"]
    assert d["eixos"]["pca"]["2"][0]["pesos"] and d["eixos"]["pca"]["2"][0]["variancia"] > 0
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
    assert run(app, ["reorganizar"]).exit_code != 0  # sem --mae não mexe em nada
    r = run(app, ["playlists", "sugerir", "--min-cluster", "6", "--plano"])
    assert r.exit_code == 0, r.output
    assert "agrupamento #1" in r.output and "plano #1 criado (só playlists novas)" in r.output
    r = run(app, ["playlists", "sugerir", "--min-cluster", "6"])  # reaproveita o agrupamento salvo
    assert "agrupamento #1" in r.output
    r = run(app, ["clusters", "renomear", "0", "Meu cluster", "--gerais"])
    assert r.exit_code == 0
    r = run(app, ["visualizar", "--saida", str(tmp_path / "m.html")])
    assert r.exit_code == 0 and (tmp_path / "m.html").exists()
    assert "Meu cluster" in (tmp_path / "m.html").read_text(encoding="utf-8")


# Catppuccin Mocha (catppuccin.com/palette)
MOCHA = {
    "rosewater": "#f5e0dc", "flamingo": "#f2cdcd", "pink": "#f5c2e7", "mauve": "#cba6f7", "red": "#f38ba8",
    "maroon": "#eba0ac", "peach": "#fab387", "yellow": "#f9e2af", "green": "#a6e3a1", "teal": "#94e2d5",
    "sky": "#89dceb", "sapphire": "#74c7ec", "blue": "#89b4fa", "lavender": "#b4befe", "text": "#cdd6f4",
    "subtext1": "#bac2de", "subtext0": "#a6adc8", "overlay2": "#9399b2", "overlay1": "#7f849c", "overlay0": "#6c7086",
    "surface2": "#585b70", "surface1": "#45475a", "surface0": "#313244", "base": "#1e1e2e", "mantle": "#181825",
    "crust": "#11111b",
}  # fmt: skip


def test_html_so_usa_cores_do_mocha():
    cores = {c.lower() for c in re.findall(r"#[0-9a-fA-F]{6}\b", html.MODELO)}
    assert cores and cores <= set(MOCHA.values()), cores - set(MOCHA.values())
    assert not re.search(r"#[0-9a-fA-F]{3}\b", html.MODELO)  # nada de hex curto escapando da checagem
    assert set(re.findall(r"rgba?\([^)]*\)", html.MODELO)) <= {"rgba(0,0,0,0)"}  # só o transparente
    assert "Viridis" not in html.MODELO and "prefers-color-scheme" not in html.MODELO


def _luminancia(hexa: str) -> float:
    def canal(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (canal(int(hexa[i : i + 2], 16)) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contraste(a: str, b: str) -> float:
    la, lb = sorted((_luminancia(a), _luminancia(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_texto_tem_contraste_sobre_os_fundos():
    tokens = dict(re.findall(r"--([a-z-]+):(#[0-9a-f]{6})", html.MODELO))
    for texto in ("tinta", "suave", "acento"):  # texto normal, texto suave e links
        for fundo in ("bg", "painel"):
            assert _contraste(tokens[texto], tokens[fundo]) >= 4.5, (texto, fundo)
