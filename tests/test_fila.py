import random
from datetime import UTC, datetime, timedelta

import pytest

from curator.audio import fila
from curator.audio.persistir import Origem, gravar_analise, gravar_falha
from curator.config import caminhos, carregar_config
from tests.falso_audio import resultado_falso
from tests.falso_yt import biblioteca_exemplo

T0 = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


@pytest.fixture
def con():
    return biblioteca_exemplo(caminhos().banco)


def status(con):
    return dict(con.execute("SELECT video_id, status FROM fila_download").fetchall())


def test_enfileira_biblioteca_sem_repetir(con):
    assert fila.enfileirar_biblioteca(con, "1", "h") == 7
    assert fila.enfileirar_biblioteca(con, "1", "h") == 0


def test_analise_valida_nao_entra_e_versao_nova_reabre(con):
    res = resultado_falso()
    gravar_analise(con, "a", res, Origem("youtube_music", "x"), "1", "h", 1.0)
    gravar_falha(con, "b", "sem-audio", "nada")
    assert fila.enfileirar_biblioteca(con, "1", "h") == 5  # sem "a" (ok) e "b" (sem-audio)
    fila.marcar(con, "c", "ok")
    gravar_analise(con, "c", res, Origem("youtube_music", "x"), "1", "h", 1.0)
    fila.enfileirar_biblioteca(con, "2", "h")  # versão nova do analisador
    assert status(con)["c"] == "pendente"
    assert "a" in status(con)  # agora entra, porque a análise é da versão antiga


def test_ordem_curtidas_recentes_primeiro(con):
    fila.enfileirar_biblioteca(con, "1", "h")
    assert fila.proximo(con, T0, 6)["video_id"] == "a"  # posicao_curtida 1


def test_retomada_depois_de_interrupcao(con):
    fila.enfileirar_biblioteca(con, "1", "h")
    fila.marcar(con, "a", "baixando")
    fila.marcar(con, "b", "analisando")
    fila.marcar(con, "c", "ok")
    assert fila.recuperar(con) == 2
    s = status(con)
    assert (s["a"], s["b"], s["c"]) == ("pendente", "pendente", "ok")


def test_backoff_com_jitter_e_teto():
    rng = random.Random(0)
    d1 = fila.backoff(1, 300, T0, rng) - T0
    d3 = fila.backoff(3, 300, T0, rng) - T0
    assert timedelta(seconds=240) <= d1 <= timedelta(seconds=360)
    assert timedelta(seconds=960) <= d3 <= timedelta(seconds=1440)
    assert fila.backoff(30, 300, T0, rng) - T0 <= timedelta(hours=24 * 1.2)


def test_falha_respeita_proxima_tentativa_e_maximo(con):
    cfg = carregar_config()
    fila.enfileirar_biblioteca(con, "1", "h")
    con.execute("DELETE FROM fila_download WHERE video_id != 'a'")
    fila.falhar(con, "a", "ErroTransitorio", "timeout", cfg, T0, random.Random(0))
    assert fila.proximo(con, T0, 6) is None  # dentro do backoff
    assert fila.proximo(con, T0 + timedelta(hours=1), 6)["video_id"] == "a"
    assert fila.proximo(con, T0 + timedelta(hours=1), 1) is None  # esgotou as tentativas


def test_resumo(con):
    fila.enfileirar_biblioteca(con, "1", "h")
    fila.falhar(con, "a", "ErroTransitorio", "x", carregar_config(), T0, random.Random(0))
    r = fila.resumo(con, 6)
    assert r["por_status"] == {"pendente": 6, "erro": 1}
    assert r["erros_por_tipo"] == {"ErroTransitorio": 1}
