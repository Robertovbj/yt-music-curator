import json

import pytest
from typer.testing import CliRunner

from curator.config import caminhos
from curator.interfaces.cli import app
from curator.motor import planos, receitas
from curator.motor.receita import Receita
from tests.falso_motor import biblioteca_analisada, playlist


@pytest.fixture
def con():
    con = biblioteca_analisada(caminhos().banco)
    playlist(con, "PLcalma", "Calma", [f"ambient{i:02d}" for i in range(6)])
    return con


def test_salvar_executar_e_comparar(con):
    r = Receita(
        nome="dormir",
        tamanho=5,
        perfil_alvo={"relaxed": {"alvo": 0.95}},
        diversidade={"max_por_artista": None},
    )
    receitas.salvar(con, r, pedido="Quero músicas para DORMIR!")
    assert receitas.por_pedido(con, "quero musicas para dormir").nome == "dormir"
    ex1, _ = receitas.executar_e_registrar(con, "dormir")
    ex2, _ = receitas.executar_e_registrar(con, "dormir")
    hist = receitas.execucoes(con, "dormir")
    c = receitas.comparar(hist[0]["resultado"], hist[1]["resultado"])
    assert c.jaccard == 1.0 and c.mesma_ordem and ex1.ids == ex2.ids

    # entra uma faixa nova muito calma: só ela aparece na diferença
    con.execute("UPDATE features SET relaxed = 0.999 WHERE video_id = 'rock05'")
    con.execute("UPDATE analises SET analisada_em = '2099-01-01T00:00:00Z' WHERE video_id = 'rock05'")
    receitas.executar_e_registrar(con, "dormir")
    hist = receitas.execucoes(con, "dormir")
    c = receitas.comparar(hist[1]["resultado"], hist[2]["resultado"])
    assert c.entraram == ["rock05"] and len(c.sairam) == 1


def test_plano_nao_repete_o_que_ja_esta_na_playlist(con):
    pid = planos.criar(
        con, "teste", [planos.Operacao("adicionar", "PLcalma", ["ambient00", "ambient07", "ambient07"])]
    )
    _, ops = planos.obter(con, pid)
    assert ops[0].video_ids == ["ambient07"]
    with pytest.raises(ValueError, match="já estão"):
        planos.criar(con, "teste", [planos.Operacao("adicionar", "PLcalma", ["ambient00"])])


def test_diff_legivel_e_descartar(con):
    pid = planos.criar(con, "receita:x", [
        planos.Operacao("criar", "nova:Foco", titulo="Foco"),
        planos.Operacao("adicionar", "nova:Foco", ["rock00"], motivo={"rock00": "score 0.9"}),
        planos.Operacao("remover", "PLcalma", ["ambient01"]),
    ])  # fmt: skip
    _, ops = planos.obter(con, pid)
    texto = planos.diff_legivel(con, ops)
    assert "■ Foco (nova)" in texto and "+ Rock 0 — Banda Rock  (score 0.9)" in texto
    assert "■ Calma" in texto and "− Ambient 1 — Ambientista" in texto
    assert planos.descartar(con, pid) and not planos.descartar(con, pid)


def test_cli_receita_classificar_e_plano(con, tmp_path):
    arq = tmp_path / "r.json"
    arq.write_text(json.dumps({"nome": "pesadas", "tamanho": 3, "perfil_alvo": {"energy": {"alvo": 0.9}}}))
    run = CliRunner().invoke
    assert run(app, ["receita", "criar", str(arq), "--pedido", "rock pesado"]).exit_code == 0
    r = run(app, ["receita", "executar", "pesadas", "--plano-nova"])
    assert r.exit_code == 0 and "3 faixa(s)" in r.output and "plano #1 criado" in r.output
    assert (
        "Foco" not in run(app, ["plano", "mostrar"]).output
        and "pesadas (nova)" in run(app, ["plano", "mostrar"]).output
    )

    arq.write_text(json.dumps({"nome": "x", "filtros": {"excluir_artistas": ["Banda Roc"]}}))
    r = run(app, ["receita", "criar", str(arq)])
    assert r.exit_code == 1 and "quis dizer: Banda Rock" in r.output

    r = run(app, ["classificar", "--plano"])
    assert r.exit_code == 0 and "com destino" in r.output
