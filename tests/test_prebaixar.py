"""#90: baixar e guardar o áudio da biblioteca sem analisar, com as regras de rede do worker."""

from pathlib import Path

import pytest

from curator.audio import guardados, prebaixar, proxies
from curator.audio.provedores.base import Arquivo, ErroBloqueio, ErroIndisponivel, ErroTransitorio
from curator.audio.worker import Compartilhado
from curator.config import caminhos, carregar_config
from tests.falso_audio import ProvedorFalso
from tests.falso_yt import biblioteca_exemplo
from tests.test_worker import Relogio


@pytest.fixture
def con():
    return biblioteca_exemplo(caminhos().banco)


@pytest.fixture
def cfg():
    c = carregar_config()
    c["fila"]["cota_diaria"] = 100
    return c


class ProvedorBaixa(ProvedorFalso):
    """`falhas`: consumidas a cada download — 'bloqueio', 'indisponivel', 'erro'; vazio = baixa."""

    def __init__(self, nome, falhas=(), **kw):
        super().__init__(nome, [], **kw)
        self.falhas, self.baixados = list(falhas), []

    def baixar(self, candidato, destino, saida):
        acao = self.falhas.pop(0) if self.falhas else "ok"
        if acao == "bloqueio":
            raise ErroBloqueio("Sign in to confirm you're not a bot")
        if acao == "indisponivel":
            raise ErroIndisponivel("Video unavailable")
        if acao == "erro":
            raise ErroTransitorio("timed out")
        self.baixados.append((candidato.ref, saida.nome))
        arq = destino / "audio.webm"
        arq.write_bytes(b"\0" * 500)
        return Arquivo(arq, 500)


def analisada(con, vid, fonte="youtube_music", ref=None, status="ok"):
    con.execute(
        "INSERT OR REPLACE INTO analises (video_id, status, fonte_audio, fonte_ref, score_casamento, analisada_em) "
        "VALUES (?, ?, ?, ?, 0.9, '2026-11-01')",
        (vid, status, fonte, ref or f"https://music.youtube.com/watch?v={vid}"),
    )


def rodar(con, cfg, provs, ids=None, limite=None, aguardar=False, relogio=None):
    proxies.sincronizar_config(con, cfg)
    comp = Compartilhado(limite)
    esperas = []
    b = prebaixar.Baixador(
        con, cfg, {p.nome: p for p in provs}, "direta", prebaixar.Trabalho(ids or prebaixar.pendentes(con)),
        comp, relogio=relogio or Relogio(), dormir=esperas.append,
    )  # fmt: skip
    return b.rodar(aguardar=aguardar), esperas


def test_pendentes_tira_guardadas_e_sem_audio_e_poe_as_pedidas_na_frente(con, cfg):
    todas = prebaixar.pendentes(con)
    assert set(todas) == {"a", "b", "c", "d", "e", "f", "x"}
    analisada(con, "e", status="sem-audio", fonte=None)
    analisada(con, "f", fonte="pasta_local", ref="/musica/f.flac")
    rodar(con, cfg, [ProvedorBaixa("youtube_music")], ids=["a"])
    assert prebaixar.pendentes(con) == [v for v in todas if v not in "aef"]
    assert prebaixar.pendentes(con, ["x", "zz", "c", "x"])[:2] == ["x", "c"]  # desconhecida ignorada


def test_baixa_a_fonte_da_analise_sem_buscar_e_guarda(con, cfg):
    analisada(con, "a", fonte="youtube_busca", ref="https://www.youtube.com/watch?v=outro")
    ym, busca = ProvedorBaixa("youtube_music"), ProvedorBaixa("youtube_busca")
    cont, esperas = rodar(con, cfg, [ym, busca], ids=["a", "b"])
    assert cont == {"guardado": 2}
    assert (
        busca.baixados == [("https://www.youtube.com/watch?v=outro", "direta")] and busca.saidas_usadas == []
    )
    assert ym.baixados == [("youtube_music:b", "direta")]  # sem análise: o próprio video_id
    g = con.execute("SELECT * FROM audios_guardados WHERE video_id = 'a'").fetchone()
    assert (g["provedor"], g["exato"], g["score"]) == ("youtube_busca", 0, 0.9)
    assert Path(g["caminho"]).is_file() and Path(g["caminho"]).parent.parent == guardados.pasta(cfg)
    assert tuple(con.execute("SELECT uso_hoje, sucessos FROM proxies WHERE nome = 'direta'").fetchone()) == (
        2,
        2,
    )
    assert len(esperas) == 1  # pausa entre os dois downloads, não depois do último
    assert con.execute("SELECT count(*) FROM fila_download").fetchone()[0] == 0  # não mexe na fila de análise
    assert con.execute("SELECT count(*) FROM features").fetchone()[0] == 0  # nem analisa


def test_bloqueio_quarentena_a_saida_e_devolve_a_faixa(con, cfg):
    yt = ProvedorBaixa("youtube_music", ["bloqueio"])
    trabalho = prebaixar.Trabalho(["a", "b"])
    proxies.sincronizar_config(con, cfg)
    b = prebaixar.Baixador(con, cfg, {"youtube_music": yt}, "direta", trabalho, Compartilhado(),
                           relogio=Relogio(), dormir=lambda s: None)  # fmt: skip
    cont = b.rodar()
    assert cont == {"bloqueio": 1, "parada: sem cota ou em quarentena": 1}
    assert trabalho.pegar() == "a"  # volta para a frente
    assert con.execute("SELECT quarentena_ate FROM proxies WHERE nome = 'direta'").fetchone()[0]


def test_erro_transitorio_tenta_de_novo_no_fim_e_desiste_depois_de_3(con, cfg):
    yt = ProvedorBaixa("youtube_music", ["erro", "erro", "erro"])
    cont, _ = rodar(con, cfg, [yt], ids=["a"])
    assert cont == {"erro": 2, "erro (desistiu nesta rodada)": 1} and yt.baixados == []
    assert con.execute("SELECT falhas FROM proxies WHERE nome = 'direta'").fetchone()[0] == 3


def test_indisponivel_fica_para_o_worker_normal(con, cfg):
    cont, _ = rodar(con, cfg, [ProvedorBaixa("youtube_music", ["indisponivel"])], ids=["a", "b"])
    assert cont == {"indisponivel": 1, "guardado": 1}
    assert prebaixar.pendentes(con)[0] == "a"


def test_respeita_cota_e_limite(con, cfg):
    cfg["fila"]["cota_diaria"] = 2
    cont, _ = rodar(con, cfg, [ProvedorBaixa("youtube_music")])
    assert cont == {"guardado": 2, "parada: sem cota ou em quarentena": 1}
    cfg["fila"]["cota_diaria"] = 100
    cont, _ = rodar(con, cfg, [ProvedorBaixa("youtube_music")], limite=1)
    assert cont == {"guardado": 1}


def test_aguardar_dorme_ate_a_cota_virar(con, cfg):
    cfg["fila"]["cota_diaria"] = 1
    relogio = Relogio()
    proxies.sincronizar_config(con, cfg)
    esperas = []

    def dormir(s):
        esperas.append(s)
        if s > 3600:  # a espera pela cota, não a pausa entre downloads
            relogio.t = relogio.t.replace(day=relogio.t.day + 1)

    b = prebaixar.Baixador(con, cfg, {"youtube_music": ProvedorBaixa("youtube_music")}, "direta",
                           prebaixar.Trabalho(["a", "b"]), Compartilhado(), relogio=relogio, dormir=dormir)  # fmt: skip
    assert b.rodar(aguardar=True) == {"guardado": 2}
    assert len(esperas) == 2  # a pausa depois do 1º e a espera pela cota


def test_em_paralelo_cada_saida_baixa_uma_parte(con, cfg, monkeypatch):
    cfg["proxy"] = [{"nome": "vpn", "url": "socks5://127.0.0.1:1"}]
    cfg["fila"]["intervalo_min_s"] = cfg["fila"]["intervalo_max_s"] = 0
    yt = ProvedorBaixa("youtube_music")
    c = prebaixar.baixar_em_paralelo(
        caminhos().banco,
        cfg,
        prebaixar.pendentes(con),
        lambda cfg, con: {"youtube_music": yt},
        Compartilhado(),
    )
    assert c == {"guardado": 7, "restantes": 0}
    assert {s for _, s in yt.baixados} <= {"direta", "vpn"}
    assert guardados.resumo(con) == (7, 3500)
