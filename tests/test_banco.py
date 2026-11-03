import sqlite3

import numpy as np
import pytest

from curator.banco import vetores
from curator.banco.conexao import abrir, agora, migracoes_do_pacote, migrar, transacao, versao


def test_banco_novo_na_ultima_versao(tmp_path):
    con = abrir(tmp_path / "c.db")
    assert versao(con) == migracoes_do_pacote()[-1][0]
    tabelas = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {
        "faixas",
        "playlists",
        "playlist_faixas",
        "analises",
        "vetores",
        "fila_download",
        "proxies",
    } <= tabelas
    assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_banco_antigo_migra_sem_perder_dados(tmp_path):
    caminho = tmp_path / "c.db"
    v1 = [(1, "a", "CREATE TABLE t (x INTEGER);")]
    con = abrir(caminho, v1)
    con.execute("INSERT INTO t VALUES (42)")
    con.close()

    v2 = v1 + [(2, "b", "ALTER TABLE t ADD COLUMN y TEXT DEFAULT 'novo';")]
    con = abrir(caminho, v2)
    assert versao(con) == 2
    assert tuple(con.execute("SELECT x, y FROM t").fetchone()) == (42, "novo")


def test_migracao_com_erro_nao_avanca_versao(tmp_path):
    con = abrir(tmp_path / "c.db", [(1, "a", "CREATE TABLE t (x);")])
    with pytest.raises(sqlite3.Error):
        migrar(con, [(1, "a", ""), (2, "quebrada", "CREATE TABLE u (y); SELEC errado;")])
    assert versao(con) == 1
    assert con.execute("SELECT count(*) FROM sqlite_master WHERE name = 'u'").fetchone()[0] == 0


def test_transacao_desfaz_em_erro(tmp_path):
    con = abrir(tmp_path / "c.db")
    with pytest.raises(RuntimeError), transacao(con):
        con.execute(
            "INSERT INTO faixas (video_id, titulo, criada_em, atualizada_em) VALUES ('v', 't', ?, ?)",
            (agora(), agora()),
        )
        raise RuntimeError
    assert con.execute("SELECT count(*) FROM faixas").fetchone()[0] == 0


def test_vetor_ida_e_volta_identico(tmp_path):
    con = abrir(tmp_path / "c.db")
    con.execute(
        "INSERT INTO faixas (video_id, titulo, criada_em, atualizada_em) VALUES ('v', 't', ?, ?)",
        (agora(), agora()),
    )
    v = np.random.default_rng(0).standard_normal(1280).astype(np.float32)
    vetores.gravar(con, "v", "effnet_emb", v)
    lido = vetores.ler(con, "v", "effnet_emb")
    assert lido.dtype == np.float32
    assert np.array_equal(lido, v)
    assert vetores.ler(con, "v", "inexistente") is None
