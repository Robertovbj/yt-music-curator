from datetime import UTC, datetime, timedelta

import pytest

from curator.audio import proxies
from curator.banco.conexao import abrir
from curator.config import carregar_config

T0 = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


@pytest.fixture
def cfg():
    c = carregar_config()
    c["fila"]["cota_diaria"] = 2
    c["proxy"] = [{"nome": "vps", "url": "socks5://user:senha@10.0.0.1:1080", "cota_diaria": 3}]
    return c


@pytest.fixture
def con(tmp_path, cfg):
    con = abrir(tmp_path / "c.db")
    proxies.sincronizar_config(con, cfg)
    return con


def test_direta_primeiro_depois_rodizio_por_uso(con):
    s = proxies.escolher(con, T0)
    assert s.nome in ("direta", "vps")
    proxies.registrar_uso(con, s.nome, T0)
    outra = proxies.escolher(con, T0)
    assert outra.nome != s.nome  # a menos usada


def test_cota_diaria_e_virada_do_dia(con):
    for _ in range(2):
        proxies.registrar_uso(con, "direta", T0)
    for _ in range(3):
        proxies.registrar_uso(con, "vps", T0)
    assert proxies.escolher(con, T0) is None
    assert proxies.escolher(con, T0 + timedelta(days=1)).nome in ("direta", "vps")


def test_quarentena_desvia_para_outra_saida(con, cfg):
    fim = proxies.quarentenar(con, "direta", cfg, T0)
    assert fim == T0 + timedelta(hours=cfg["disjuntor"]["quarentena_inicial_h"])
    assert proxies.escolher(con, T0).nome == "vps"
    assert proxies.escolher(con, fim + timedelta(seconds=1)).nome == "direta"


def test_quarentena_cresce_e_zera_com_sucesso(con, cfg):
    d = cfg["disjuntor"]
    assert proxies.quarentenar(con, "vps", cfg, T0) - T0 == timedelta(hours=d["quarentena_inicial_h"])
    assert proxies.quarentenar(con, "vps", cfg, T0) - T0 == timedelta(hours=d["quarentena_inicial_h"] * 2)
    for _ in range(10):
        proxies.quarentenar(con, "vps", cfg, T0)
    assert proxies.quarentenar(con, "vps", cfg, T0) - T0 == timedelta(hours=d["quarentena_max_h"])
    proxies.registrar_sucesso(con, "vps")
    assert proxies.quarentenar(con, "vps", cfg, T0) - T0 == timedelta(hours=d["quarentena_inicial_h"])


def test_falhas_seguidas_rebaixam_a_saida(con, cfg):
    for _ in range(proxies.FALHAS_PARA_QUARENTENA - 1):
        assert proxies.registrar_falha(con, "vps", cfg, T0) is None
    assert proxies.registrar_falha(con, "vps", cfg, T0) is not None
    assert all(s.nome != "vps" for s in [proxies.escolher(con, T0)])


def test_proxy_removido_do_config_fica_inativo(con, cfg):
    cfg["proxy"] = []
    proxies.sincronizar_config(con, cfg)
    assert [s["nome"] for s in proxies.estado(con, T0)] == ["direta"]


def test_credenciais_nunca_aparecem(con):
    assert proxies.mascarar("socks5://user:senha@10.0.0.1:1080") == "socks5://***@10.0.0.1:1080"
    assert all("senha" not in s["url"] for s in proxies.estado(con, T0))


def test_proxima_liberacao(con, cfg):
    proxies.quarentenar(con, "direta", cfg, T0)
    assert proxies.proxima_liberacao(con, T0) == T0 + timedelta(
        hours=cfg["disjuntor"]["quarentena_inicial_h"]
    )


def test_cota_vira_no_dia_local_e_nao_em_utc(con, cfg, monkeypatch):
    monkeypatch.setenv("TZ", "America/Sao_Paulo")
    import time

    time.tzset()
    try:
        noite = datetime(2026, 11, 4, 0, 30, tzinfo=UTC)  # 21:30 de 03/11 em São Paulo
        for _ in range(2):
            proxies.registrar_uso(con, "direta", T0)  # 09:00 de 03/11 em São Paulo
        assert proxies.escolher(con, noite).nome == "vps"  # direta já gastou a cota do dia 03 local
        assert proxies.proxima_liberacao(con, noite) == datetime(2026, 11, 4, 3, 0, tzinfo=UTC)  # 00:00 local
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
