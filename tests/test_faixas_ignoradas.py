"""#100: faixas ignoradas uma a uma. Saem de tudo, mesmo curtidas e mesmo numa playlist válida, sem mexer na conta
do YouTube; `considerar` desfaz."""

import json
import sqlite3
import zipfile

import pytest
from typer.testing import CliRunner

from curator.audio import fila
from curator.banco import compartilhar, consultas
from curator.coleta.candidatas import SementeInvalida
from curator.coleta.cliente import ClienteYT
from curator.coleta.sincronizar import sincronizar_curtidas
from curator.config import caminhos, carregar_config
from curator.interfaces import mcp as m
from curator.interfaces.cli import app
from curator.motor import aprendido, clusters, espacos, planos
from curator.motor import recomendar as rr
from curator.motor.executor import executar
from curator.motor.receita import carregar
from tests.falso_motor import biblioteca_analisada, playlist
from tests.falso_yt import FalsoYT, faixa

MEME = "rock01"  # curtida e numa playlist válida: mesmo assim sai
COVER = "rap02"


@pytest.fixture
def con():
    c = biblioteca_analisada(caminhos().banco, por_estilo=14)
    playlist(c, "PLrock", "Rock", [f"rock{i:02d}" for i in range(10)])
    consultas.ignorar_faixas(c, [MEME, COVER], "meme")
    return c


def test_fora_de_tudo_mas_com_a_analise_guardada(con):
    for ids in (consultas.biblioteca(con), consultas.utilizaveis(con), consultas.nao_alocadas(con),
                espacos.da_biblioteca(con), clusters._universo(con, "biblioteca"),
                clusters._universo(con, clusters.CURTIDAS), consultas.fora_das_gerais(con)):  # fmt: skip
        assert MEME not in ids and COVER not in ids
    assert {MEME, COVER} <= consultas.bloqueadas(con)
    assert (
        con.execute(f"SELECT count(*) FROM analises WHERE video_id IN ('{MEME}', '{COVER}')").fetchone()[0]
        == 2
    )
    assert con.execute(f"SELECT curtida FROM faixas WHERE video_id = '{MEME}'").fetchone()[0] == 1
    est = consultas.estatisticas(con)
    assert est["faixas_ignoradas"] == 2 and est["videos_ignorados"] == 2


def test_ignorar_e_considerar(con):
    r = consultas.ignorar_faixas(con, [MEME, "rock02", "nao-existe"], "outro motivo")
    assert r == {"ignoradas": ["rock02"], "ja_estavam": [MEME], "desconhecidas": ["nao-existe"]}
    motivos = {f["video_id"]: f["motivo"] for f in consultas.faixas_ignoradas(con)}
    assert motivos == {MEME: "outro motivo", COVER: "meme", "rock02": "outro motivo"}
    r = consultas.considerar_faixas(con, [MEME, "rock03"])
    assert r == {"consideradas": [MEME], "nao_estavam": ["rock03"]}
    assert MEME in consultas.biblioteca(con) and MEME in espacos.da_biblioteca(con)


def test_considerar_nao_tira_de_uma_playlist_ignorada(con):
    playlist(con, "PLjogo", "Valorant Plays", [COVER])
    con.execute("UPDATE playlists SET ignorada = 1 WHERE playlist_id = 'PLjogo'")
    con.execute(f"UPDATE faixas SET curtida = 0 WHERE video_id = '{COVER}'")
    consultas.considerar_faixas(con, [COVER])
    assert COVER in consultas.ignoradas(con)  # continua fora pela playlist (#77)


def test_receita_nao_usa(con):
    ex = executar(con, carregar({"nome": "x", "tamanho": 100, "universo": "curtidas",
                                 "diversidade": {"max_por_artista": None}}))  # fmt: skip
    assert ex.ids and MEME not in ex.ids and COVER not in ex.ids
    assert ex.descartes["ignorada"] == 2
    ex = executar(con, carregar({"nome": "x", "tamanho": 100, "universo": "PLrock"}))
    assert MEME not in ex.ids and ex.descartes["ignorada"] == 1


def test_plano_nao_adiciona(con):
    pid = planos.criar(con, "teste", [planos.Operacao("adicionar", "PLrock", [MEME, "rock11"])])
    diff = json.loads(con.execute("SELECT diff FROM planos WHERE plano_id = ?", (pid,)).fetchone()[0])
    assert [o["video_ids"] for o in diff] == [["rock11"]]


def test_fila_nao_baixa(con):
    fila.enfileirar_biblioteca(con, "versao-nova", "h")
    na_fila = {r[0] for r in con.execute("SELECT video_id FROM fila_download")}
    assert na_fila and MEME not in na_fila and COVER not in na_fila


def test_recomendacao_nem_semente_nem_candidata(con):
    cfg = carregar_config()
    cfg["recomendacoes"] = {**cfg["recomendacoes"], "conferir_avaliacao": False}
    yt = FalsoYT()
    yt.radios = {"rock05": [faixa(MEME), faixa("novo1", artistas=("Outra",))]}
    cli = ClienteYT(yt, intervalo_s=0, dormir=lambda s: None)

    def pedir(**kw):
        return rr.recomendar(
            con,
            cli,
            None,
            rr.Pedido(**{"modo": "rapido", "escopo": "novas", "fontes": ["yt_radio"], **kw}),
            cfg,
        )

    res = pedir(faixas=["rock05"])
    assert [i.video_id for i in res.itens] == ["novo1"] and res.descartes["ignorada"] == 1
    with pytest.raises(SementeInvalida):
        pedir(faixas=[MEME])


def test_fora_do_pacote_compartilhado(con, tmp_path):
    ex = compartilhar.exportar(con, tmp_path / "p.zip", versao_analisador="1", hash_modelos="h")
    with zipfile.ZipFile(ex.caminho) as z:
        (tmp_path / "p.db").write_bytes(z.read(compartilhar.BANCO))
    pc = sqlite3.connect(tmp_path / "p.db")
    ids = {r[0] for r in pc.execute("SELECT video_id FROM analises")}
    assert ids and MEME not in ids and COVER not in ids
    tabelas = {r[0] for r in pc.execute("SELECT name FROM sqlite_master")}
    assert "faixas_ignoradas" not in tabelas


def test_rotulo_de_faixa_ignorada_nao_ensina(con):
    aprendido.rotular(con, MEME, "Rock", True, "cli")
    aprendido.rotular(con, "rock02", "Rock", True, "cli")
    assert set(aprendido.rotulos(con)["Rock"]) == {"rock02"}
    antes = aprendido.hash_rotulos(con)
    consultas.considerar_faixas(con, [MEME])
    assert set(aprendido.rotulos(con)["Rock"]) == {MEME, "rock02"} and aprendido.hash_rotulos(con) != antes
    consultas.ignorar_faixas(con, ["rock03"])
    assert "rock03" not in {
        d["video_id"] for d in aprendido.duvidosas(con, "Rock", n=100, max_por_artista=100)
    }


def test_sincronizar_nao_desfaz():
    from curator.banco.conexao import abrir

    yt = FalsoYT()
    yt.curtidas = [faixa(v) for v in "abc"]
    con = abrir(caminhos().banco)
    cli = ClienteYT(yt, intervalo_s=0, dormir=lambda s: None)
    sincronizar_curtidas(con, cli)
    consultas.ignorar_faixas(con, ["b"], "meme")
    sincronizar_curtidas(con, cli)
    assert consultas.biblioteca(con) == ["a", "c"]
    assert con.execute("SELECT curtida FROM faixas WHERE video_id = 'b'").fetchone()[0] == 1


def test_mcp_nao_mostra_e_ignora_so_por_pedido(con):
    assert MEME not in {f["video_id"] for f in m.buscar_faixas("rock 0")}
    r = m.ignorar_faixa("rock03", "o usuário disse que é meme")
    assert r["ok"] and r["ignorada"] and not r["ja_estava"]
    assert "rock03" not in consultas.biblioteca(con)
    r = m.ignorar_faixa("rock03", desfazer=True)
    assert r["estava_ignorada"] and not r["ignorada"] and "rock03" in consultas.biblioteca(con)
    assert m.ignorar_faixa("nao-existe")["erros"]
    assert "ignorar_faixa" in {f.__name__ for f in m.FERRAMENTAS}


def test_cli(con):
    run = CliRunner().invoke
    r = run(app, ["faixas", "ignoradas"])
    assert r.exit_code == 0 and f"({MEME}): meme" in r.output and "2 faixa(s) ignorada(s)" in r.output
    r = run(app, ["faixas", "ignorar", "rock04", "nao-existe", "--motivo", "x"])
    assert r.exit_code != 0 and "rock04" in consultas.biblioteca(con)  # nada gravado
    r = run(app, ["faixas", "ignorar", "rock04", "--motivo", "cover de piada"])
    assert r.exit_code == 0 and "ignorada:" in r.output and "rock04" not in consultas.biblioteca(con)
    r = run(app, ["faixas", "considerar", "rock04", "rock05"])
    assert r.exit_code == 0 and "considerada de novo" in r.output and "não estava ignorada" in r.output
    assert "rock04" in consultas.biblioteca(con)
