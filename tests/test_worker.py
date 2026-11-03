from datetime import UTC, datetime, timedelta

import pytest

from curator.audio import fila
from curator.audio.worker import Worker
from curator.config import caminhos, carregar_config
from tests.falso_audio import AnalisadorFalso, ProvedorFalso
from tests.falso_yt import biblioteca_exemplo

T0 = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


@pytest.fixture
def con():
    con = biblioteca_exemplo(caminhos().banco)
    fila.enfileirar_biblioteca(con, "1", "hash-teste")
    return con


@pytest.fixture
def cfg():
    c = carregar_config()
    c["fila"]["cota_diaria"] = 100
    return c


class Relogio:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


def worker(con, cfg, provs, analisador=None, relogio=None):
    esperas = []
    w = Worker(
        con, cfg, analisador or AnalisadorFalso(), {p.nome: p for p in provs},
        relogio=relogio or Relogio(), dormir=esperas.append,
    )  # fmt: skip
    return w, esperas


def status(con, vid):
    return con.execute("SELECT status FROM fila_download WHERE video_id = ?", (vid,)).fetchone()[0]


def test_fluxo_feliz_grava_e_apaga_audio(con, cfg):
    yt = ProvedorFalso("youtube_music", ["ok", "ok"])
    an = AnalisadorFalso()
    w, esperas = worker(con, cfg, [yt], an)
    rel = w.rodar(limite=2)
    assert rel.contagem == {"ok": 2} and rel.motivo_parada == "limite de 2 faixa(s)"
    assert status(con, "a") == "ok" and status(con, "b") == "ok"
    assert all(not a.exists() for a in yt.arquivos)  # áudio apagado
    a = con.execute(
        "SELECT status, fonte_audio, bytes_baixados FROM analises WHERE video_id = 'a'"
    ).fetchone()
    assert tuple(a) == ("ok", "youtube_music", 1000)
    assert (
        len(esperas) == 1 and cfg["fila"]["intervalo_min_s"] <= esperas[0] <= cfg["fila"]["intervalo_max_s"]
    )
    assert con.execute("SELECT uso_hoje, sucessos FROM proxies WHERE nome = 'direta'").fetchone()[:] == (2, 2)


def test_bloqueio_quarentena_a_saida_e_nao_gasta_tentativa(con, cfg):
    cfg["proxy"] = [{"nome": "vps", "url": "socks5://10.0.0.1:1080"}]
    yt = ProvedorFalso("youtube_music", ["bloqueio", "ok"])
    w, _ = worker(con, cfg, [yt])
    rel = w.rodar(limite=1)
    assert rel.contagem == {"bloqueio": 1, "ok": 1}
    assert yt.saidas_usadas[0] != yt.saidas_usadas[1]  # trocou de saída
    q = con.execute("SELECT nome FROM proxies WHERE quarentena_ate IS NOT NULL").fetchall()
    assert [r[0] for r in q] == [yt.saidas_usadas[0]]
    assert con.execute("SELECT tentativas FROM fila_download WHERE video_id = 'a'").fetchone()[0] == 0


def test_todas_as_saidas_bloqueadas_para_o_worker(con, cfg):
    yt = ProvedorFalso("youtube_music", ["bloqueio"])
    w, _ = worker(con, cfg, [yt])
    rel = w.rodar()
    assert rel.motivo_parada == "todas as saídas em quarentena ou sem cota"
    assert rel.proxima_liberacao == T0 + timedelta(hours=cfg["disjuntor"]["quarentena_inicial_h"])
    assert status(con, "a") == "pendente"


def test_indisponivel_cai_para_o_proximo_provedor(con, cfg):
    yt = ProvedorFalso("youtube_music", ["indisponivel"])
    busca = ProvedorFalso("youtube_busca", ["ok"], score=0.9, exato=False)
    w, _ = worker(con, cfg, [yt, busca])
    w.rodar(limite=1)
    assert status(con, "a") == "ok"
    assert con.execute("SELECT fonte_audio, score_casamento FROM analises WHERE video_id = 'a'").fetchone()[
        :
    ] == ("youtube_busca", 0.9)


def test_candidato_abaixo_do_limiar_nao_e_analisado(con, cfg):
    busca = ProvedorFalso("youtube_busca", ["ok"], score=0.5, exato=False)
    an = AnalisadorFalso()
    w, _ = worker(con, cfg, [busca], an)
    w.rodar(limite=1)
    assert an.analisados == [] and status(con, "a") == "sem-audio"
    assert con.execute("SELECT status FROM analises WHERE video_id = 'a'").fetchone()[0] == "sem-audio"


def test_duracao_divergente_descarta_candidato_de_busca(con, cfg):
    busca = ProvedorFalso("youtube_busca", ["ok"], score=0.95, exato=False)
    w, _ = worker(con, cfg, [busca], AnalisadorFalso(duracao=400.0))  # faixa tem 200 s
    w.rodar(limite=1)
    assert status(con, "a") == "sem-audio"


def test_erro_transitorio_agenda_nova_tentativa(con, cfg):
    yt = ProvedorFalso("youtube_music", ["erro"])
    rel_ = Relogio()
    w, _ = worker(con, cfg, [yt], relogio=rel_)
    w.rodar(limite=1)
    linha = con.execute(
        "SELECT status, tentativas, tipo_erro, provedores_tentados FROM fila_download WHERE video_id = 'a'"
    ).fetchone()
    assert tuple(linha) == ("erro", 1, "ErroTransitorio", "[]")
    assert con.execute("SELECT falhas FROM proxies WHERE nome = 'direta'").fetchone()[0] == 1


def test_provedor_local_nao_dorme_nem_gasta_cota(con, cfg):
    local = ProvedorFalso("pasta_local", ["ok", "ok"], usa_rede=False)
    w, esperas = worker(con, cfg, [local])
    w.rodar(limite=2)
    assert esperas == []
    assert con.execute("SELECT uso_hoje FROM proxies WHERE nome = 'direta'").fetchone()[0] == 0


def test_parada_por_sinal_entre_faixas(con, cfg):
    yt = ProvedorFalso("youtube_music", [])
    w, _ = worker(con, cfg, [yt])
    w.parar = True
    assert w.rodar().motivo_parada == "interrompido"
    assert status(con, "a") == "pendente"


def test_cota_diaria_respeitada(con, cfg):
    cfg["fila"]["cota_diaria"] = 3
    yt = ProvedorFalso("youtube_music", [])
    w, _ = worker(con, cfg, [yt])
    rel = w.rodar()
    assert rel.contagem == {"ok": 3}
    assert rel.motivo_parada == "todas as saídas em quarentena ou sem cota"
