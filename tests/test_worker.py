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


def test_progresso_uma_linha_por_faixa(con, cfg):
    yt = ProvedorFalso("youtube_music", ["ok", "indisponivel"])
    w, _ = worker(con, cfg, [yt])
    linhas = []
    w.rodar(limite=2, ao_processar=lambda p: linhas.append(p.linha()))
    assert len(linhas) == 2
    assert linhas[0].startswith("[1/2] Música a — Banda · ok em ")
    assert linhas[1].startswith("[2/2] Música b — Banda · sem audio em ")


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


def test_download_parcial_de_busca_nao_e_descartado_pela_duracao(con, cfg):
    busca = ProvedorFalso("youtube_busca", ["ok"], score=0.95, exato=False, parcial=True)
    w, _ = worker(con, cfg, [busca], AnalisadorFalso(duracao=400.0))  # só o começo de uma faixa longa
    w.rodar(limite=1)
    assert status(con, "a") == "ok"


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


# ------------------------------------------------------------------ modo paralelo (uma thread por saída)


def test_reservar_nunca_entrega_a_mesma_faixa_a_dois_workers(con):
    from curator.banco.conexao import abrir

    outra = abrir(caminhos().banco)
    a = fila.reservar(con, T0, 6)
    b = fila.reservar(outra, T0, 6)
    assert a["video_id"] != b["video_id"]
    assert {
        r[0]
        for r in con.execute(
            "SELECT status FROM fila_download WHERE video_id IN (?, ?)", (a["video_id"], b["video_id"])
        )
    } == {"baixando"}


class AnalisadorConcorrencia(AnalisadorFalso):
    """Falha o teste se duas análises rodarem ao mesmo tempo."""

    def __init__(self):
        super().__init__()
        import threading

        self.ativos, self.maximo, self._t = 0, 0, threading.Lock()

    def analisar(self, caminho):
        import time

        with self._t:
            self.ativos += 1
            self.maximo = max(self.maximo, self.ativos)
        time.sleep(0.02)
        try:
            return super().analisar(caminho)
        finally:
            with self._t:
                self.ativos -= 1


def test_paralelo_usa_todas_as_saidas_sem_repetir_e_analisa_uma_por_vez(con, cfg):
    from curator.audio.worker import rodar_em_paralelo

    cfg["proxy"] = [
        {"nome": "vpn", "url": "socks5://127.0.0.1:1081"},
        {"nome": "celular", "url": "socks5://127.0.0.1:1082"},
    ]
    provs = []

    def montar(cfg, c):
        p = ProvedorFalso("youtube_music", [])
        provs.append(p)
        return {"youtube_music": p}

    an = AnalisadorConcorrencia()
    rel = rodar_em_paralelo(caminhos().banco, cfg, an, montar, dormir=lambda s: None)
    assert rel.contagem == {"ok": 7} and rel.motivo_parada == "fila vazia"
    assert an.maximo == 1  # análise serializada
    usadas = [s for p in provs for s in p.saidas_usadas]
    assert sorted(set(usadas)) == ["celular", "direta", "vpn"]
    assert len(usadas) == 7  # nenhuma faixa foi buscada duas vezes
    assert con.execute("SELECT count(*) FROM fila_download WHERE status = 'ok'").fetchone()[0] == 7


def test_paralelo_respeita_limite_global_e_cota_por_saida(con, cfg):
    from curator.audio.worker import rodar_em_paralelo

    cfg["fila"]["cota_diaria"] = 2  # direta e vpn: 2 cada
    cfg["proxy"] = [{"nome": "vpn", "url": "socks5://127.0.0.1:1081"}]
    rel = rodar_em_paralelo(
        caminhos().banco,
        cfg,
        AnalisadorFalso(),
        lambda c, k: {"youtube_music": ProvedorFalso("youtube_music", [])},
        dormir=lambda s: None,
    )
    assert rel.contagem == {"ok": 4} and rel.motivo_parada == "todas as saídas em quarentena ou sem cota"
    usos = dict(con.execute("SELECT nome, uso_hoje FROM proxies").fetchall())
    assert usos == {"direta": 2, "vpn": 2}

    cfg["fila"]["cota_diaria"] = 100
    rel = rodar_em_paralelo(caminhos().banco, cfg, AnalisadorFalso(), lambda c, k: {"youtube_music": ProvedorFalso("youtube_music", [])},
                            limite=2, dormir=lambda s: None)  # fmt: skip
    assert rel.contagem == {"ok": 2} and rel.motivo_parada.startswith("limite")


def test_paralelo_bloqueio_numa_saida_nao_para_as_outras(con, cfg):
    from curator.audio.worker import rodar_em_paralelo

    cfg["proxy"] = [{"nome": "vpn", "url": "socks5://127.0.0.1:1081"}]

    class SoDiretaBloqueada(ProvedorFalso):
        def buscar(self, faixa, saida):
            if saida.nome == "direta":
                self.saidas_usadas.append("direta")
                from curator.audio.provedores.base import ErroBloqueio

                raise ErroBloqueio("Sign in to confirm you're not a bot")
            import time

            time.sleep(0.03)  # a vpn demora um pouco: as duas threads disputam a fila de verdade
            return super().buscar(faixa, saida)

    rel = rodar_em_paralelo(caminhos().banco, cfg, AnalisadorFalso(), lambda c, k: {"youtube_music": SoDiretaBloqueada("youtube_music", [])},
                            dormir=lambda s: None)  # fmt: skip
    assert rel.contagem["ok"] == 7  # a vpn processou tudo
    assert (
        con.execute("SELECT quarentena_ate IS NOT NULL FROM proxies WHERE nome = 'direta'").fetchone()[0] == 1
    )


def test_erro_de_analise_nao_poe_a_saida_em_quarentena(con, cfg):
    class AnalisadorQuebrado(AnalisadorFalso):
        def analisar(self, caminho):
            raise RuntimeError(
                "In RhythmExtractor2013.compute: Descriptor name 'internal.bpm' of type Real not found"
            )

    w, _ = worker(con, cfg, [ProvedorFalso("youtube_music", [])], AnalisadorQuebrado())
    rel = w.rodar()
    assert rel.contagem == {"erro": 7}
    p = con.execute(
        "SELECT falhas, falhas_seguidas, quarentena_ate FROM proxies WHERE nome = 'direta'"
    ).fetchone()
    assert tuple(p) == (0, 0, None)  # 7 erros de análise e nenhuma falha atribuída à rede
    assert (
        con.execute("SELECT tipo_erro FROM fila_download WHERE video_id = 'a'").fetchone()[0] == "ErroAnalise"
    )


def test_aguardar_dorme_ate_a_quarentena_acabar_e_continua(con, cfg):
    from curator.audio import proxies

    relogio = Relogio()
    esperas = []

    def dormir(s):  # o tempo "passa" enquanto o worker dorme
        esperas.append(s)
        relogio.t += timedelta(seconds=s)

    yt = ProvedorFalso("youtube_music", ["bloqueio"])
    w = Worker(con, cfg, AnalisadorFalso(), {"youtube_music": yt}, relogio=relogio, dormir=dormir)
    rel = w.rodar(limite=2, aguardar=True)
    assert rel.contagem == {"bloqueio": 1, "ok": 2}
    fim_quarentena = T0 + timedelta(hours=cfg["disjuntor"]["quarentena_inicial_h"])
    assert relogio.t >= fim_quarentena  # dormiu até a quarentena acabar (somando a pausa normal)
    assert max(esperas) > 3600  # e foi uma espera longa, não só a pausa entre downloads
    assert proxies.escolher(con, relogio.t) is not None


def test_sem_aguardar_continua_parando(con, cfg):
    w, _ = worker(con, cfg, [ProvedorFalso("youtube_music", ["bloqueio"])])
    assert w.rodar().motivo_parada == "todas as saídas em quarentena ou sem cota"
