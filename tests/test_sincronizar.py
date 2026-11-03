import pytest

from curator.banco.conexao import abrir
from curator.coleta.cliente import ClienteYT
from curator.coleta.sincronizar import completar_metadados, sincronizar_curtidas, sincronizar_playlists
from tests.falso_yt import FalsoYT, faixa


@pytest.fixture
def yt():
    y = FalsoYT()
    y.curtidas = [faixa("a", album="X"), faixa("b", album="X"), faixa("c", artistas=("Outra", "Banda"))]
    y.playlists = {
        "PL1": {"title": "Rock - All", "tracks": [faixa("a", album="X"), faixa("d")]},
        "PL2": {"title": "Rock - Arena", "tracks": [faixa("a", album="X")]},
        "PL3": {"title": "Playlist de um amigo", "tracks": [faixa("z")], "owned": False},
    }
    return y


@pytest.fixture
def con(tmp_path):
    return abrir(tmp_path / "c.db")


def cliente(yt):
    return ClienteYT(yt, intervalo_s=0, dormir=lambda s: None)


def curtidas(con):
    return [
        r[0] for r in con.execute("SELECT video_id FROM faixas WHERE curtida = 1 ORDER BY posicao_curtida")
    ]


def membros(con, pid):
    return [
        r[0]
        for r in con.execute(
            "SELECT video_id FROM playlist_faixas WHERE playlist_id = ? ORDER BY posicao", (pid,)
        )
    ]


def test_curtidas_espelham_o_yt(con, yt):
    r = sincronizar_curtidas(con, cliente(yt))
    assert curtidas(con) == ["a", "b", "c"]
    assert r.contagens["novas"] == 3
    artistas = con.execute(
        "SELECT artista_id FROM faixa_artistas WHERE video_id = 'c' ORDER BY ordem"
    ).fetchall()
    assert [a[0] for a in artistas] == ["UC_outra", "UC_banda"]

    yt.curtidas.pop(1)  # descurtiu "b"
    r = sincronizar_curtidas(con, cliente(yt))
    assert curtidas(con) == ["a", "c"]
    assert r.contagens["removidas"] == 1
    assert con.execute("SELECT count(*) FROM faixas WHERE video_id = 'b'").fetchone()[0] == 1  # a faixa fica


def test_curtida_sem_video_id_e_ignorada(con, yt):
    yt.curtidas.append({"videoId": None, "title": "indisponível"})
    r = sincronizar_curtidas(con, cliente(yt))
    assert r.contagens["sem_video_id"] == 1
    assert r.avisos


def test_playlists_proprias_e_de_terceiros(con, yt):
    r = sincronizar_playlists(con, cliente(yt))
    assert r.contagens == {"baixadas": 2, "de_terceiros": 1}
    assert membros(con, "PL1") == ["a", "d"]
    assert con.execute("SELECT propria FROM playlists WHERE playlist_id = 'PL3'").fetchone()[0] == 0
    assert membros(con, "PL3") == []
    assert con.execute("SELECT count(*) FROM playlists WHERE playlist_id = 'LM'").fetchone()[0] == 0


def test_musica_removida_no_yt_some_do_banco(con, yt):
    sincronizar_playlists(con, cliente(yt))
    yt.playlists["PL1"]["tracks"].pop(0)
    sincronizar_playlists(con, cliente(yt))
    assert membros(con, "PL1") == ["d"]


def test_segunda_sincronizacao_sem_mudancas_nao_baixa_nada(con, yt):
    sincronizar_playlists(con, cliente(yt))
    yt.chamadas.clear()
    r = sincronizar_playlists(con, cliente(yt))
    assert yt.chamadas["get_playlist"] == 0
    assert r.contagens == {"puladas": 2}
    sincronizar_playlists(con, cliente(yt), forcar=True)
    assert yt.chamadas["get_playlist"] == 2  # forçar rebaixa só as próprias; a de terceiros já é conhecida
    assert membros(con, "PL1") == ["a", "d"]


def test_playlist_apagada_no_yt_some_e_avisa_filhas(con, yt):
    sincronizar_playlists(con, cliente(yt))
    con.execute("UPDATE playlists SET mae_id = 'PL1' WHERE playlist_id = 'PL2'")
    del yt.playlists["PL1"]
    r = sincronizar_playlists(con, cliente(yt), forcar=True)
    assert r.contagens["removidas"] == 1
    assert any("filha" in a for a in r.avisos)
    assert con.execute("SELECT mae_id FROM playlists WHERE playlist_id = 'PL2'").fetchone()[0] is None


def test_playlist_repete_faixa(con, yt):
    yt.playlists["PL2"]["tracks"].append(faixa("a", album="X", set_id="outro"))
    sincronizar_playlists(con, cliente(yt))
    assert membros(con, "PL2") == ["a", "a"]


def test_metadados_falha_nao_vira_cache(con, yt):
    sincronizar_curtidas(con, cliente(yt))
    yt.falhar_album.add("MPRE_X")
    r = completar_metadados(con, cliente(yt))
    assert r.contagens["falhas"] == 1
    linha = con.execute("SELECT metadados_em, tentativas, proxima_tentativa_em FROM albuns").fetchone()
    assert linha["metadados_em"] is None and linha["tentativas"] == 1 and linha["proxima_tentativa_em"]

    # dentro do backoff, não tenta de novo
    yt.chamadas.clear()
    completar_metadados(con, cliente(yt))
    assert yt.chamadas["get_album"] == 0

    # depois que o álbum volta (forçando), resolve
    yt.falhar_album.clear()
    completar_metadados(con, cliente(yt), forcar=True)
    linha = con.execute("SELECT ano, descricao, tentativas, erro FROM albuns").fetchone()
    assert tuple(linha) == (2020, "desc", 0, None)


def test_metadados_dentro_do_ttl_nao_chamam_api(con, yt):
    sincronizar_curtidas(con, cliente(yt))
    completar_metadados(con, cliente(yt))
    yt.chamadas.clear()
    r = completar_metadados(con, cliente(yt))
    assert yt.chamadas["get_album"] == 0
    assert r.contagens["em_cache"] == 1
