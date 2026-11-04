"""#77: playlists ignoradas. Os vídeos delas ficam fora de tudo, mesmo curtidos, salvo os que também estão
numa playlist válida."""

import json
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from curator.audio import fila
from curator.banco import consultas, hierarquia
from curator.coleta.candidatas import SementeInvalida
from curator.coleta.cliente import ClienteYT
from curator.coleta.sincronizar import sincronizar_curtidas, sincronizar_playlists
from curator.config import caminhos, carregar_config
from curator.interfaces import mcp as m
from curator.interfaces.cli import app
from curator.motor import classificar as cl
from curator.motor import clusters, espacos, planos
from curator.motor import recomendar as rr
from curator.motor.executor import executar
from curator.motor.receita import ReceitaInvalida, carregar, resolver_playlist
from tests.falso_motor import biblioteca_analisada, playlist
from tests.falso_yt import HIERARQUIA_ROCK, FalsoYT, faixa

JOGO = [f"ambient{i:02d}" for i in range(14)]  # todas curtidas
TAMBEM_NO_ROCK = "rock00"  # está na playlist ignorada e na Rock: continua valendo


@pytest.fixture
def con():
    c = biblioteca_analisada(caminhos().banco, por_estilo=14)
    playlist(c, "PLrock", "Rock", [f"rock{i:02d}" for i in range(10)])
    playlist(c, "PLrap", "Rap", [f"rap{i:02d}" for i in range(10)])
    playlist(c, "PLjogo", "Valorant Plays", [*JOGO, TAMBEM_NO_ROCK])
    c.execute("UPDATE playlists SET ignorada = 1 WHERE playlist_id = 'PLjogo'")
    return c


def test_fora_da_biblioteca_mas_com_a_analise_guardada(con):
    for ids in (consultas.biblioteca(con), consultas.utilizaveis(con), consultas.nao_alocadas(con),
                espacos.da_biblioteca(con), clusters._universo(con, "biblioteca")):  # fmt: skip
        assert not set(JOGO) & set(ids)
    assert {"rock01", TAMBEM_NO_ROCK} <= set(consultas.biblioteca(con))
    assert (
        con.execute("SELECT count(*) FROM analises WHERE video_id IN ('ambient00', 'rock00')").fetchone()[0]
        == 2
    )
    est = consultas.estatisticas(con)
    assert est["playlists_ignoradas"] == 1 and est["videos_ignorados"] == 14
    assert "Valorant Plays" not in {p["titulo"] for p in est["playlists"]}


def test_receita_nao_usa_nem_referencia(con):
    ex = executar(con, carregar({"nome": "x", "tamanho": 100, "universo": "curtidas",
                                 "diversidade": {"max_por_artista": None}}))  # fmt: skip
    assert ex.ids and not set(JOGO) & set(ex.ids)
    assert ex.descartes["playlist ignorada"] == 14
    assert resolver_playlist(con, "Valorant Plays") is None
    with pytest.raises(ReceitaInvalida):
        executar(con, carregar({"nome": "x", "universo": "PLjogo"}))


def test_classificacao_nao_enxerga(con):
    ps, _ = cl.perfis(con)
    assert "PLjogo" not in ps and TAMBEM_NO_ROCK in ps["PLrock"].membros
    c = cl.classificar(con, ids=["ambient00", "rock11"])
    assert [d.video_id for d in c.decisoes] == ["rock11"]


def test_mcp_nao_mostra(con):
    assert "Valorant Plays" not in {p["titulo"] for p in m.arvore_playlists()}
    assert m.buscar_faixas("ambient") == [] and m.buscar_artistas("ambientista") == []
    assert {f["video_id"] for f in m.buscar_faixas("rock 1")} == {f"rock{i}" for i in range(10, 14)} | {
        "rock01"
    }


def test_plano_recusa_a_playlist_e_filtra_os_videos(con):
    with pytest.raises(planos.PlaylistIgnorada, match="Valorant Plays"):
        planos.criar(con, "teste", [planos.Operacao("adicionar", "PLjogo", ["rock11"])])
    pid = planos.criar(con, "teste", [planos.Operacao("adicionar", "PLrock", ["ambient01", "rock11"])])
    diff = json.loads(con.execute("SELECT diff FROM planos WHERE plano_id = ?", (pid,)).fetchone()[0])
    assert [o["video_ids"] for o in diff] == [["rock11"]]


def test_fila_nao_baixa(con):
    fila.enfileirar_biblioteca(con, "versao-nova", "h")  # tudo precisaria de análise de novo
    na_fila = {r[0] for r in con.execute("SELECT video_id FROM fila_download")}
    assert na_fila and not set(JOGO) & na_fila
    con.execute(  # pendente de antes da marcação: fica parado na fila
        "INSERT INTO fila_download (video_id, status, criada_em, atualizada_em) VALUES ('ambient00', 'pendente', '', '')"
    )
    reservadas = []
    while item := fila.reservar(con, datetime.now(UTC), 6):
        reservadas.append(item["video_id"])
    assert reservadas and "ambient00" not in reservadas


def cfg_rec():
    c = carregar_config()
    c["recomendacoes"] = {**c["recomendacoes"], "conferir_avaliacao": False}
    return c


def test_recomendacao_nao_semeia_nem_sugere(con):
    yt = FalsoYT()
    yt.radios = {"rock05": [faixa("ambient03"), faixa("novo1", artistas=("Outra",))]}
    cli = ClienteYT(yt, intervalo_s=0, dormir=lambda s: None)
    pedir = lambda **kw: rr.recomendar(  # noqa: E731
        con,
        cli,
        None,
        rr.Pedido(**{"modo": "rapido", "escopo": "novas", "fontes": ["yt_radio"], **kw}),
        cfg_rec(),
    )
    res = pedir(faixas=["rock05"])
    assert [i.video_id for i in res.itens] == ["novo1"] and res.descartes["playlist ignorada"] == 1
    with pytest.raises(SementeInvalida):
        pedir(faixas=["ambient00"])
    with pytest.raises(SementeInvalida):
        pedir(playlist="Valorant Plays")


def test_cli_ignorar_e_considerar(con):
    run = CliRunner().invoke
    r = run(app, ["playlists", "considerar", "Valorant Plays"])
    assert r.exit_code == 0, r.output
    assert "ambient00" in consultas.biblioteca(con)
    r = run(app, ["playlists", "ignorar", "PLjogo"])
    assert r.exit_code == 0 and "15 vídeo(s): 14 fora de tudo (14 curtido(s)), 1 continuam" in r.output
    assert "ambient00" not in consultas.biblioteca(con)
    assert "Valorant Plays" in run(app, ["playlists", "ignoradas"]).output
    assert run(app, ["playlists", "ignorar", "não existe"]).exit_code != 0


def test_marcacao_sobrevive_a_sincronizar_e_a_hierarquia(tmp_path):
    from curator.banco.conexao import abrir

    yt = FalsoYT()
    yt.curtidas = [faixa(v) for v in "abc"]
    yt.playlists = {"PLall": {"title": "Rock - All", "tracks": [faixa("a")]},
                    "PLarena": {"title": "Rock - Arena", "tracks": [faixa("a")]},
                    "PLfolk": {"title": "Rock - Folk", "tracks": [faixa("b")]},
                    "PLaula": {"title": "Piano Learning", "tracks": [faixa("c")]}}  # fmt: skip
    con = abrir(caminhos().banco)
    cli = ClienteYT(yt, intervalo_s=0, dormir=lambda s: None)
    sincronizar_curtidas(con, cli)
    sincronizar_playlists(con, cli)
    con.execute("UPDATE playlists SET ignorada = 1 WHERE playlist_id = 'PLaula'")
    sincronizar_playlists(con, cli, forcar=True)
    arq = tmp_path / "h.toml"
    arq.write_text(HIERARQUIA_ROCK)
    hierarquia.aplicar(con, hierarquia.ler(arq))
    assert con.execute("SELECT ignorada FROM playlists WHERE playlist_id = 'PLaula'").fetchone()[0] == 1
    assert "c" not in consultas.biblioteca(con)
    assert "Piano Learning (1) [ignorada]" in hierarquia.arvore(con)
