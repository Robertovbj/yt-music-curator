"""#59: a análise de uma faixa nunca é apagada, mesmo quando ela sai das curtidas e de todas as playlists."""

import sqlite3
from pathlib import Path

import pytest

from curator.audio import fila
from curator.audio.persistir import Origem, gravar_analise
from curator.banco import consultas
from curator.coleta.cliente import ClienteYT
from curator.coleta.sincronizar import sincronizar_curtidas, sincronizar_playlists
from curator.config import caminhos
from curator.motor import espacos
from tests.falso_audio import resultado_falso
from tests.falso_yt import FalsoYT, faixa


@pytest.fixture
def yt():
    y = FalsoYT()
    y.curtidas = [faixa("a"), faixa("b")]
    y.playlists = {"PL1": {"title": "Rock", "tracks": [faixa("a")]}}
    return y


def sincronizar(con, yt):
    cli = ClienteYT(yt, intervalo_s=0, dormir=lambda s: None)
    sincronizar_curtidas(con, cli)
    sincronizar_playlists(con, cli, forcar=True)


@pytest.fixture
def con(yt):
    from curator.banco.conexao import abrir

    c = abrir(caminhos().banco)
    sincronizar(c, yt)
    for vid in ("a", "b"):
        gravar_analise(c, vid, resultado_falso(), Origem("youtube_music", vid), "1", "h", 1.0)
    return c


def contagens(con, vid):
    return tuple(
        con.execute(f"SELECT count(*) FROM {t} WHERE video_id = ?", (vid,)).fetchone()[0]
        for t in ("faixas", "analises", "features", "vetores")
    )


def test_faixa_que_sai_de_tudo_mantem_analise_e_fica_fora_dos_universos(con, yt):
    antes = contagens(con, "a")
    yt.curtidas = [faixa("b")]
    yt.playlists["PL1"]["tracks"] = []
    sincronizar(con, yt)
    assert contagens(con, "a") == antes and antes[3] > 0
    assert "a" not in consultas.biblioteca(con)
    assert "a" not in consultas.nao_alocadas(con)
    assert con.execute("SELECT curtida FROM faixas WHERE video_id = 'a'").fetchone()[0] == 0
    assert consultas.estatisticas(con)["analisadas_fora_da_biblioteca"] == 1
    assert "a" in espacos.analisadas(con)  # continua guardada para reaproveitar


def test_apagar_faixa_analisada_falha(con):
    with pytest.raises(sqlite3.IntegrityError, match="#59"):
        con.execute("DELETE FROM faixas WHERE video_id = 'a'")
    assert contagens(con, "a")[1] == 1


def test_faixa_sem_analise_ainda_pode_ser_apagada(con):
    from curator.banco.conexao import agora

    con.execute(
        "INSERT INTO faixas (video_id, titulo, criada_em, atualizada_em) VALUES ('z', 'z', ?, ?)",
        (agora(), agora()),
    )
    con.execute("DELETE FROM faixas WHERE video_id = 'z'")


def test_faixa_que_volta_as_curtidas_nao_e_baixada_de_novo(con, yt):
    yt.curtidas = [faixa("b")]
    yt.playlists["PL1"]["tracks"] = []
    sincronizar(con, yt)
    yt.curtidas = [faixa("a"), faixa("b")]
    sincronizar(con, yt)
    assert "a" in consultas.biblioteca(con)
    assert fila.enfileirar_biblioteca(con, "1", "h") == 0


def test_nenhum_codigo_substitui_faixas_com_replace():
    # INSERT OR REPLACE em `faixas` apagaria a linha (e, pelo CASCADE, a análise) sem passar pela trava.
    raiz = Path(__file__).parents[1] / "src"
    for arq in raiz.rglob("*.py"):
        assert "REPLACE INTO faixas" not in arq.read_text(encoding="utf-8").upper(), arq


def test_backup_e_restauracao_levam_as_faixas_fora_da_biblioteca(con, yt, tmp_path, monkeypatch):
    from curator.banco import backup
    from curator.banco.conexao import abrir

    yt.curtidas = [faixa("b")]
    yt.playlists["PL1"]["tracks"] = []
    sincronizar(con, yt)
    c = caminhos()
    backup.criar(tmp_path / "b.zip", c)
    monkeypatch.setenv("CURATOR_CONFIG_DIR", str(tmp_path / "pc2" / "config"))
    monkeypatch.setenv("CURATOR_DATA_DIR", str(tmp_path / "pc2" / "dados"))
    backup.restaurar(tmp_path / "b.zip", caminhos())
    assert contagens(abrir(caminhos().banco), "a") == contagens(con, "a")
