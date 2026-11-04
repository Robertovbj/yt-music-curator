"""#67: recomendação lenta, com download e análise das candidatas (worker simulado)."""

from datetime import UTC, datetime

import numpy as np
import pytest

from curator.audio import fila
from curator.audio.analisador import Resultado
from curator.audio.persistir import Origem, gravar_analise
from curator.coleta.cliente import ClienteYT
from curator.config import caminhos, carregar_config
from curator.motor import lento
from curator.motor import recomendacoes as rc
from curator.motor import recomendar as rr
from tests.falso_motor import ROTULOS, biblioteca_analisada
from tests.falso_yt import FalsoYT, faixa


@pytest.fixture(autouse=True)
def versao_fixa(monkeypatch):
    monkeypatch.setattr(lento, "_versao", lambda: ("1", "h"))


@pytest.fixture
def con():
    return biblioteca_analisada(caminhos().banco, por_estilo=6)


@pytest.fixture
def yt():
    y = FalsoYT()
    y.radios = {"rock00": [faixa(f"n{i}", artistas=(f"Art{i}",)) for i in range(6)]}
    return y


def pedir(con, yt, **kw):
    p = rr.Pedido(**{"modo": "lento", "escopo": "novas", "faixas": ["rock00"], "fontes": ["yt_radio"], **kw})
    return rr.recomendar(con, ClienteYT(yt, intervalo_s=0, dormir=lambda s: None), None, p, carregar_config())


def analisar_como(con, vid, estilo_de):
    """Simula o worker: a candidata recebe embedding e features iguais às de uma faixa da biblioteca."""
    emb = con.execute(
        "SELECT dim, dados FROM vetores WHERE video_id = ? AND tipo = 'effnet_emb'", (estilo_de,)
    ).fetchone()
    e = np.frombuffer(emb["dados"], np.float32).copy()
    probs = np.full(len(ROTULOS), 0.01, np.float32)
    r = Resultado({"energy": 0.5}, {"effnet_emb": e, "discogs400": probs}, {"discogs400": ROTULOS}, 200.0, {})
    gravar_analise(con, vid, r, Origem("youtube_music", vid), "1", "h", 1.0)
    fila.marcar(con, vid, "ok")


def test_candidatas_entram_na_fila_depois_da_biblioteca(con, yt):
    con.execute(
        "INSERT INTO faixas (video_id, titulo, curtida, posicao_curtida, criada_em, atualizada_em) VALUES ('nova_curtida', 'x', 1, 999, 'x', 'x')"
    )
    res = pedir(con, yt)
    assert res.status == "aguardando_analise" and res.pendentes == 6
    assert fila.enfileirar_biblioteca(con, "1", "h") == 1  # a curtida nova
    ordem = []
    while item := fila.reservar(con, datetime.now(UTC), 6):
        ordem.append(item["video_id"])
    assert ordem[0] == "nova_curtida" and set(ordem[1:]) == {f"n{i}" for i in range(6)}
    assert fila.resumo(con, 6)["candidatas_pendentes"] == 0  # todas reservadas


def test_conclui_quando_as_analises_terminam_e_ordena_pelo_motor(con, yt):
    res = pedir(con, yt)
    rid = res.recomendacao_id
    for i in range(6):
        analisar_como(con, f"n{i}", "rock01" if i in (4, 5) else "ambient01")  # n4/n5 soam como a semente
        if i == 2:
            assert lento.concluir(con, rid, carregar_config()).status == "aguardando_analise"
    fim = lento.concluir(con, rid, carregar_config())
    assert fim.status == "pronta" and rc.obter(con, rid)["status"] == "pronta"
    assert [i.video_id for i in fim.itens][:2] == ["n4", "n5"]
    assert [i["video_id"] for i in rc.itens(con, rid)] == [i.video_id for i in fim.itens]
    assert [i.video_id for i in rr.refazer(con, rid, carregar_config())] == [i.video_id for i in fim.itens]


def test_parcial_fecha_com_o_que_ja_foi_analisado(con, yt):
    res = pedir(con, yt)
    analisar_como(con, "n0", "rock01")
    fim = lento.concluir(con, res.recomendacao_id, carregar_config(), parcial=True)
    assert [i.video_id for i in fim.itens] == ["n0"] and fim.pendentes == 5


def test_candidata_ja_analisada_nao_volta_para_a_fila(con, yt):
    pedir(con, yt)  # grava as candidatas e enfileira
    for i in range(6):
        analisar_como(con, f"n{i}", "rock01")
    con.execute("DELETE FROM fila_download")
    res = pedir(con, yt)
    assert res.status == "pronta" and res.pendentes == 0
    assert con.execute("SELECT count(*) FROM fila_download").fetchone()[0] == 0


def test_sem_audio_nao_trava_a_recomendacao(con, yt):
    res = pedir(con, yt)
    for i in range(6):
        fila.marcar(con, f"n{i}", "sem-audio")
    analisar_como(con, "n0", "rock01")
    assert lento.concluir(con, res.recomendacao_id, carregar_config()).status == "pronta"
