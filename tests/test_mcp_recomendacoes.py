"""#69: ferramentas MCP de recomendação, explícitas e com confirmação na dúvida."""

import pytest

from curator.coleta.cliente import ClienteYT
from curator.config import caminhos
from curator.interfaces import mcp as m
from tests.falso_motor import biblioteca_analisada, playlist
from tests.falso_yt import FalsoYT, faixa


@pytest.fixture
def con(monkeypatch):
    c = biblioteca_analisada(caminhos().banco, por_estilo=6)
    playlist(c, "PLrock", "Rock", [f"rock{i:02d}" for i in range(6)])
    yt = FalsoYT()
    yt.radios = {s: [faixa("n1", artistas=("A",)), faixa("rock03"), faixa("n2", artistas=("B",))]
                 for s in [f"rock{i:02d}" for i in range(6)]}  # fmt: skip
    monkeypatch.setattr(m, "_cliente_yt", lambda: ClienteYT(yt, intervalo_s=0, dormir=lambda s: None))
    monkeypatch.setattr(m, "_cliente_deezer", lambda: None)  # sem rede nos testes
    return c


def test_sem_modo_pede_confirmacao_e_nao_coleta(con):
    r = m.recomendar_novas(faixas=["rock00"])
    assert "CONFIRME" in r["erros"][0]
    assert con.execute("SELECT count(*) FROM coletas").fetchone()[0] == 0


def test_recomendar_novas(con):
    r = m.recomendar_novas(modo="rapido", playlist="Rock", pedido="parecidas com a playlist Rock")
    assert r["status"] == "pronta" and r["escopo"] == "novas"
    assert [i["video_id"] for i in r["itens"]] == ["n1", "n2"]
    assert not any(i["da_biblioteca"] for i in r["itens"])
    h = m.historico_recomendacoes()
    assert h[0]["pedido"] == "parecidas com a playlist Rock" and h[0]["status_itens"]["sem_reacao"] == 2


def test_misturar_e_ferramenta_separada(con):
    r = m.recomendar_misturando_curtidas(modo="rapido", faixas=["rock00"], tamanho=2)
    assert r["escopo"] == "misturar" and {i["da_biblioteca"] for i in r["itens"]} == {True, False}


def test_plano_da_recomendacao_e_playlist_nova_e_destino_exige_permissao(con):
    rid = m.recomendar_novas(modo="rapido", faixas=["rock00"])["recomendacao_id"]
    assert "erros" in m.criar_plano_da_recomendacao(rid, playlist_destino="Rock")
    plano = m.criar_plano_da_recomendacao(rid, nome="Descobertas · rock")
    assert "Descobertas · rock (nova)" in plano["diff"]


def test_erros_claros(con):
    assert "erros" in m.recomendar_novas(modo="rapido")  # sem semente
    assert "erros" in m.recomendar_novas(
        modo="rapido", faixas=["rock00"], perfil_alvo={"energy": {"alvo": 0.9}}
    )
    assert "erros" in m.mostrar_recomendacao(999)


def test_exemplos_cobrem_os_tres_pedidos():
    ex = m.exemplos_de_recomendacoes()
    assert [e["chamada"] and e["chamada"]["ferramenta"] for e in ex] == [
        "recomendar_misturando_curtidas",
        None,
        "recomendar_novas",
    ]


def test_ferramentas_registradas(con):
    import asyncio

    pytest.importorskip("mcp")
    nomes = {t.name for t in asyncio.run(m.criar_servidor().list_tools())}
    assert {"recomendar_novas", "recomendar_misturando_curtidas", "historico_recomendacoes"} <= nomes
    assert not any("aplicar" in n for n in nomes)
