"""#70: histórico das recomendações, status após sincronizar e taxa de acerto por fonte."""

import json

import pytest
from typer.testing import CliRunner

from curator.banco.conexao import agora
from curator.config import caminhos
from curator.interfaces.cli import app
from curator.motor import recomendacoes as rc
from tests.falso_motor import biblioteca_analisada

runner = CliRunner()


@pytest.fixture
def con():
    c = biblioteca_analisada(caminhos().banco, por_estilo=2)
    for v in ("a", "b", "c", "d"):
        c.execute(
            "INSERT INTO faixas (video_id, titulo, artistas, criada_em, atualizada_em) VALUES (?, ?, ?, ?, ?)",
            (v, v.upper(), json.dumps([{"id": "UC", "nome": "X"}]), agora(), agora()),
        )
    return c


def rec(con, itens, modo="rapido", sementes=("rock00",)):
    its = [rc.ItemRecomendado(v, 1.0, fontes, 1) for v, fontes in itens]
    return rc.salvar(con, modo=modo, escopo="novas", spec={}, sementes=list(sementes), coletas=[], itens=its)


def test_status_muda_depois_de_sincronizar(con):
    rid = rec(con, [("a", ["yt_radio"]), ("b", ["deezer"])])
    con.execute("UPDATE faixas SET curtida = 1 WHERE video_id = 'a'")  # o que a sincronização faz
    con.execute("UPDATE faixas SET avaliacao = 'dislike' WHERE video_id = 'b'")
    assert {i["video_id"]: i["status"] for i in rc.itens(con, rid)} == {"a": "curtida", "b": "dislike"}
    r = runner.invoke(app, ["recomendacoes", "mostrar", str(rid)])
    assert "♥" in r.output and "✗" in r.output


def test_taxa_de_acerto_por_fonte_e_modo(con):
    rec(con, [("a", ["yt_radio"]), ("b", ["yt_radio", "deezer"]), ("c", ["deezer"])])
    rec(con, [("d", ["yt_radio"])], modo="lento")
    con.execute("UPDATE faixas SET curtida = 1 WHERE video_id IN ('a', 'd')")
    con.execute("UPDATE faixas SET avaliacao = 'dislike' WHERE video_id = 'c'")
    a = rc.acerto(con)
    assert a["fonte"]["yt_radio"]["recomendadas"] == 3 and a["fonte"]["yt_radio"][
        "taxa_acerto"
    ] == pytest.approx(2 / 3, abs=1e-3)
    assert a["fonte"]["deezer"]["taxa_acerto"] == 0 and a["fonte"]["deezer"]["taxa_dislike"] == 0.5
    assert a["modo"]["lento"]["taxa_acerto"] == 1.0
    r = runner.invoke(app, ["recomendacoes", "acerto"])
    assert "yt_radio" in r.output and "67%" in r.output


def test_novidades_em_relacao_a_ultima_com_as_mesmas_sementes(con):
    r1 = rec(con, [("a", ["yt_radio"]), ("b", ["yt_radio"])])
    rec(con, [("c", ["yt_radio"])], sementes=("rap00",))
    r3 = rec(con, [("b", ["yt_radio"]), ("c", ["yt_radio"]), ("d", ["yt_radio"])])
    assert rc.novidades(con, r3) == (r1, ["c", "d"])
    assert rc.novidades(con, r1) == (None, [])


def test_listar(con):
    rec(con, [("a", ["yt_radio"])])
    r = runner.invoke(app, ["recomendacoes", "listar"])
    assert r.exit_code == 0 and "sem reação 1" in r.output
