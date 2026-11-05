"""#85: o que a API do YouTube Music oferece de uso e popularidade das faixas."""

import pytest

from curator.banco.conexao import abrir
from curator.coleta.cliente import ClienteYT
from curator.coleta.sincronizar import completar_estatisticas, gravar_faixa, sincronizar_curtidas
from tests.falso_yt import FalsoYT, faixa


@pytest.fixture
def yt():
    y = FalsoYT()
    y.curtidas = [faixa("a", explicita=True, tipo="ATV"), faixa("b", explicita=False, tipo="OMV"), faixa("c")]
    return y


@pytest.fixture
def con(tmp_path):
    return abrir(tmp_path / "c.db")


def cliente(yt):
    return ClienteYT(yt, intervalo_s=0, dormir=lambda s: None)


def linha(con, vid, *colunas):
    return tuple(
        con.execute(f"SELECT {', '.join(colunas)} FROM faixas WHERE video_id = ?", (vid,)).fetchone()
    )


def test_curtidas_gravam_explicita_e_tipo_do_video(con, yt):
    sincronizar_curtidas(con, cliente(yt))
    assert linha(con, "a", "explicita", "tipo_video") == (1, "ATV")
    assert linha(con, "b", "explicita", "tipo_video") == (0, "OMV")
    assert linha(con, "c", "explicita", "tipo_video") == (None, None)
    gravar_faixa(
        con, faixa("a"), avaliacao_confiavel=False
    )  # rádio sem os campos: não apaga o que já se sabe
    assert linha(con, "a", "explicita", "tipo_video") == (1, "ATV")


def test_estatisticas_respeitam_o_limite_por_rodada_e_a_validade(con, yt):
    sincronizar_curtidas(con, cliente(yt))
    r = completar_estatisticas(con, cliente(yt), por_rodada=2)
    assert r.contagens == {"buscadas": 2, "faltam": 1} and yt.chamadas["get_song"] == 2
    assert linha(con, "a", "visualizacoes", "publicada_em") == (1234567, "2020-08-06")
    r = completar_estatisticas(con, cliente(yt), por_rodada=2)
    assert r.contagens == {"buscadas": 1} and yt.chamadas["get_song"] == 3  # só a que faltava
    yt.chamadas.clear()
    assert completar_estatisticas(con, cliente(yt)).contagens == {} and yt.chamadas["get_song"] == 0
    r = completar_estatisticas(
        con, cliente(yt), por_rodada=2, forcar=True
    )  # forçar ignora a validade, não o limite
    assert r.contagens == {"buscadas": 2, "faltam": 1}


def test_falha_reagenda_e_nao_vira_cache(con, yt):
    sincronizar_curtidas(con, cliente(yt))
    yt.falhar_musica.add("a")
    r = completar_estatisticas(con, cliente(yt))
    assert r.contagens == {"falhas": 1, "buscadas": 2}
    em, tentativas, proxima = linha(
        con, "a", "estatisticas_em", "estatisticas_tentativas", "estatisticas_proxima"
    )
    assert em is None and tentativas == 1 and proxima
    yt.chamadas.clear()
    completar_estatisticas(con, cliente(yt))
    assert yt.chamadas["get_song"] == 0  # dentro do backoff
    yt.falhar_musica.clear()
    completar_estatisticas(con, cliente(yt), forcar=True)
    assert linha(con, "a", "visualizacoes", "estatisticas_tentativas", "estatisticas_proxima") == (
        1234567,
        0,
        None,
    )


def test_resposta_sem_dados_conta_como_buscada(con, yt):
    sincronizar_curtidas(con, cliente(yt))
    yt.musicas["a"] = {"playabilityStatus": {"status": "ERROR"}}
    r = completar_estatisticas(con, cliente(yt))
    assert r.contagens == {"sem_dados": 1, "buscadas": 2}
    assert linha(con, "a", "visualizacoes", "publicada_em")[0] is None
    assert linha(con, "a", "estatisticas_em")[0] is not None  # a API respondeu: não repete a cada rodada


def test_so_faixas_da_biblioteca(con, yt):
    sincronizar_curtidas(con, cliente(yt))
    gravar_faixa(con, faixa("candidata"), avaliacao_confiavel=False)  # recomendação, fora da biblioteca
    completar_estatisticas(con, cliente(yt))
    assert yt.chamadas["get_song"] == 3 and linha(con, "candidata", "estatisticas_em") == (None,)
