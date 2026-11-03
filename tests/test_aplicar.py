import json

import pytest
from typer.testing import CliRunner

from curator.banco import hierarquia
from curator.coleta import aplicar as ap
from curator.coleta.cliente import ClienteYT
from curator.coleta.sincronizar import sincronizar_curtidas, sincronizar_playlists
from curator.config import caminhos
from curator.interfaces.cli import app
from curator.motor import planos
from tests.falso_yt import HIERARQUIA_ROCK, FalsoYT, faixa


@pytest.fixture
def yt():
    y = FalsoYT()
    y.curtidas = [faixa(v) for v in "abcdef"]
    y.playlists = {
        "PLall": {"title": "Rock - All", "tracks": [faixa("a"), faixa("b"), faixa("c")]},
        "PLarena": {"title": "Rock - Arena", "tracks": [faixa("a")]},
        "PLfolk": {"title": "Rock - Folk", "tracks": [faixa("b")]},
    }
    return y


@pytest.fixture
def cli(yt):
    return ClienteYT(yt, intervalo_s=0, dormir=lambda s: None)


@pytest.fixture
def con(cli, tmp_path):
    from curator.banco.conexao import abrir

    con = abrir(caminhos().banco)
    sincronizar_curtidas(con, cli)
    sincronizar_playlists(con, cli)
    arq = tmp_path / "h.toml"
    arq.write_text(HIERARQUIA_ROCK)
    hierarquia.aplicar(con, hierarquia.ler(arq))
    return con


def ids(yt, pid):
    return [t["videoId"] for t in yt.playlists[pid]["tracks"]]


def test_aplicar_cria_adiciona_e_confere(con, cli, yt):
    pid = planos.criar(con, "teste", [
        planos.Operacao("criar", "nova:Foco", titulo="Foco"),
        planos.Operacao("adicionar", "nova:Foco", ["d", "e"]),
        planos.Operacao("adicionar", "PLfolk", ["e"]),
    ])  # fmt: skip
    res = ap.aplicar(con, cli, pid)
    assert res.status == "aplicado" and res.divergencias == []
    nova = res.criadas["nova:Foco"]
    assert yt.playlists[nova]["title"] == "Foco" and yt.playlists[nova]["privacy"] == "PRIVATE"
    assert ids(yt, nova) == ["d", "e"] and ids(yt, "PLfolk") == ["b", "e"]
    assert ids(yt, "PLall") == ["a", "b", "c", "e"]  # propagado para a agregadora acima de PLfolk
    linha = con.execute("SELECT status, aplicado_em FROM planos WHERE plano_id = ?", (pid,)).fetchone()
    assert linha["status"] == "aplicado" and linha["aplicado_em"]
    assert [
        r[0]
        for r in con.execute(
            "SELECT video_id FROM playlist_faixas WHERE playlist_id = 'PLfolk' ORDER BY posicao"
        )
    ] == ["b", "e"]


def test_plano_obsoleto_e_recusado_sem_tocar_no_yt(con, cli, yt):
    pid = planos.criar(con, "teste", [planos.Operacao("adicionar", "PLarena", ["d"])])
    yt.playlists["PLarena"]["tracks"].append(faixa("z", set_id="manual"))  # alguém mexeu no app depois
    yt.chamadas.clear()
    with pytest.raises(ap.PlanoObsoleto, match="Rock - Arena"):
        ap.aplicar(con, cli, pid)
    assert yt.chamadas["add_playlist_items"] == 0
    assert con.execute("SELECT status FROM planos WHERE plano_id = ?", (pid,)).fetchone()[0] == "pendente"


def test_falha_no_meio_deixa_parcial_e_para(con, cli, yt):
    yt.falhar_escrita.add("PLarena")
    pid = planos.criar(con, "teste", [
        planos.Operacao("adicionar", "PLarena", ["d"]),
        planos.Operacao("adicionar", "PLfolk", ["e"]),
    ], propagar_agregadoras=False)  # fmt: skip
    res = ap.aplicar(con, cli, pid)
    assert res.status == "parcial" and [x["ok"] for x in res.log] == [False]
    assert ids(yt, "PLfolk") == ["b"]  # a segunda operação não rodou


def test_desfazer_restaura_e_apaga_as_criadas(con, cli, yt):
    antes = {p: ids(yt, p) for p in ("PLall", "PLarena")}
    pid = planos.criar(con, "teste", [
        planos.Operacao("criar", "nova:Foco", titulo="Foco"),
        planos.Operacao("adicionar", "nova:Foco", ["d"]),
        planos.Operacao("adicionar", "PLarena", ["d", "e"]),
        planos.Operacao("remover", "PLall", ["c"]),
        planos.Operacao("renomear", "PLarena", titulo="Arena!"),
    ])  # fmt: skip
    res = ap.aplicar(con, cli, pid)
    assert res.status == "aplicado" and yt.playlists["PLarena"]["title"] == "Arena!"
    assert "c" not in ids(yt, "PLall")
    ap.desfazer(con, cli, pid)
    assert ids(yt, "PLarena") == antes["PLarena"] and yt.playlists["PLarena"]["title"] == "Rock - Arena"
    assert sorted(ids(yt, "PLall")) == sorted(antes["PLall"])
    assert res.criadas["nova:Foco"] not in yt.playlists
    assert con.execute("SELECT status FROM planos WHERE plano_id = ?", (pid,)).fetchone()[0] == "desfeito"
    with pytest.raises(ap.PlanoInvalido):
        ap.desfazer(con, cli, pid)


def test_plano_corrige_agregadoras(con, cli, yt):
    yt.playlists["PLfolk"]["tracks"].append(faixa("f", set_id="x"))
    sincronizar_playlists(con, cli, forcar=True)
    pid = planos.de_agregadoras(con)
    ap.aplicar(con, cli, pid)
    assert "f" in ids(yt, "PLall")
    assert hierarquia.faltantes_nas_agregadoras(con) == {}


def test_cli_aplicar_pede_sim_fora_do_terminal_e_respeita_limite(con, yt, monkeypatch):
    from curator.coleta import cliente as mod

    monkeypatch.setattr(
        mod, "abrir_cliente", lambda *a, **k: ClienteYT(yt, intervalo_s=0, dormir=lambda s: None)
    )
    pid = planos.criar(con, "teste", [planos.Operacao("adicionar", "PLarena", ["d"])])
    run = CliRunner().invoke
    r = run(app, ["plano", "aplicar", str(pid)])
    assert r.exit_code == 1 and "--sim" in r.output and ids(yt, "PLarena") == ["a"]
    caminhos().config_dir.mkdir(parents=True, exist_ok=True)
    caminhos().config_toml.write_text("[planos]\nlimite_faixas = 1\n")
    r = run(app, ["plano", "aplicar", str(pid), "--sim"])
    assert r.exit_code == 1 and "--grande" in r.output  # 2 faixas (com a agregadora) > limite 1
    r = run(app, ["plano", "aplicar", str(pid), "--sim", "--grande"])
    assert r.exit_code == 0, r.output
    assert ids(yt, "PLarena") == ["a", "d"] and "aplicado" in r.output
    r = run(app, ["plano", "desfazer", str(pid), "--sim"])
    assert r.exit_code == 0 and ids(yt, "PLarena") == ["a"]
    assert (
        json.loads(con.execute("SELECT backup FROM planos WHERE plano_id = ?", (pid,)).fetchone()[0])[
            "PLarena"
        ]["titulo"]
        == "Rock - Arena"
    )
