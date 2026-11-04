"""#73: playlist "Descobertas curtidas" com o que veio de recomendação e foi curtido depois."""

import json

import pytest
from typer.testing import CliRunner

from curator.coleta import aplicar as ap
from curator.coleta.cliente import ClienteYT
from curator.config import caminhos, carregar_config
from curator.interfaces.cli import app
from curator.motor import planos
from curator.motor import recomendacoes as rc
from curator.motor import recomendar as rr
from tests.falso_motor import biblioteca_analisada, playlist
from tests.falso_yt import FalsoYT, faixa


@pytest.fixture
def yt():
    y = FalsoYT()
    y.radios = {
        "rock00": [faixa(v, artistas=(f"Artista {v}",)) for v in ("n1", "n2", "n3")] + [faixa("rock02")]
    }
    return y


@pytest.fixture
def con(yt):
    c = biblioteca_analisada(caminhos().banco, por_estilo=6)
    playlist(c, "PLrock", "Rock", [f"rock{i:02d}" for i in range(6)])
    cfg = carregar_config()
    cfg["recomendacoes"] = {**cfg["recomendacoes"], "conferir_avaliacao": False}
    p = rr.Pedido(modo="rapido", escopo="novas", faixas=["rock00"], fontes=["yt_radio"])
    rr.recomendar(c, ClienteYT(yt, intervalo_s=0, dormir=lambda s: None), None, p, cfg)
    return c


def diff(con, pid):
    return json.loads(con.execute("SELECT diff FROM planos WHERE plano_id = ?", (pid,)).fetchone()[0])


def test_so_as_novas_que_viraram_curtida(con):
    assert rc.descobertas_curtidas(con) == []
    with pytest.raises(ValueError, match="nenhuma música recomendada"):
        planos.de_descobertas_curtidas(con)
    con.execute(
        "UPDATE faixas SET curtida = 1 WHERE video_id IN ('n1', 'n3', 'rock02')"
    )  # rock02 já era conhecida
    con.execute("UPDATE faixas SET curtida = 1, avaliacao = 'dislike' WHERE video_id = 'n2'")
    assert rc.descobertas_curtidas(con) == ["n1", "n3"]


def test_cria_marca_e_depois_so_completa(con, yt):
    con.execute("UPDATE faixas SET curtida = 1 WHERE video_id = 'n1'")
    pid = planos.de_descobertas_curtidas(con)
    assert [(o["tipo"], o["playlist"], o["video_ids"]) for o in diff(con, pid)] == [
        ("criar", "nova:Descobertas curtidas", []), ("adicionar", "nova:Descobertas curtidas", ["n1"])
    ]  # fmt: skip
    res = ap.aplicar(con, ClienteYT(yt, intervalo_s=0, dormir=lambda s: None), pid)
    assert res.status == "aplicado", res.log
    nova = next(iter(res.criadas.values()))
    assert con.execute("SELECT descobertas FROM playlists WHERE playlist_id = ?", (nova,)).fetchone()[0] == 1
    con.execute("UPDATE faixas SET curtida = 1 WHERE video_id = 'n3'")
    pid2 = planos.de_descobertas_curtidas(con)
    assert [(o["tipo"], o["playlist"], o["video_ids"]) for o in diff(con, pid2)] == [
        ("adicionar", nova, ["n3"])
    ]


def test_cli(con):
    con.execute("UPDATE faixas SET curtida = 1 WHERE video_id = 'n1'")
    r = CliRunner().invoke(app, ["recomendacoes", "curtidas", "--plano"])
    assert r.exit_code == 0, r.output
    assert "1 música(s)" in r.output and "Música n1" in r.output and "plano #1 criado" in r.output
