"""#87: popularidade, novidade e tipo do vídeo nas receitas e nas recomendações (sem a parte da explícita)."""

import numpy as np
import pytest

from curator.coleta.cliente import ClienteYT
from curator.config import caminhos, carregar_config
from curator.motor import popularidade
from curator.motor import recomendar as rr
from curator.motor.executor import executar
from curator.motor.receita import ReceitaInvalida, carregar
from tests.falso_motor import biblioteca_analisada
from tests.falso_yt import FalsoYT, faixa


def _vistas(con, valores: dict[str, int | None]) -> None:
    for vid, n in valores.items():
        con.execute("UPDATE faixas SET visualizacoes = ? WHERE video_id = ?", (n, vid))


@pytest.fixture
def con():
    c = biblioteca_analisada(caminhos().banco, por_estilo=4)
    # rock00 < rock01 < rock02 em visualizações; rock03 sem o dado
    _vistas(c, {"rock00": 1_000, "rock01": 100_000, "rock02": 10_000_000, "ambient00": 100_000})
    return c


def ids(ex):
    return [i.video_id for i in ex.itens]


# ------------------------------------------------------------------ percentil


def test_percentil_na_regua_da_biblioteca_com_empate_no_meio(con):
    pct = dict(zip(["rock00", "rock01", "rock02", "rock03"], popularidade.percentis(
        con, ["rock00", "rock01", "rock02", "rock03"]), strict=True))  # fmt: skip
    assert pct["rock00"] == pytest.approx(0.5 / 4)  # a menor das 4 com dado
    assert pct["rock01"] == pytest.approx((1 + 0.5 * 2) / 4)  # empatada com ambient00
    assert pct["rock02"] == pytest.approx(3.5 / 4)
    assert np.isnan(pct["rock03"])


def test_faixa_de_fora_e_medida_na_regua_da_biblioteca_sem_mudar_a_regua(con):
    con.execute(
        "INSERT INTO faixas (video_id, titulo, artistas, visualizacoes, criada_em, atualizada_em) "
        "VALUES ('fora', 'Fora', '[]', 1000000000, '2026', '2026')"
    )
    antes = popularidade.regua(con)
    assert popularidade.percentis(con, ["fora"])[0] == 1.0
    assert np.array_equal(popularidade.regua(con), antes)


def test_dislike_nao_entra_na_regua(con):
    con.execute("UPDATE faixas SET avaliacao = 'dislike' WHERE video_id = 'rock02'")
    assert len(popularidade.regua(con)) == 3


# ------------------------------------------------------------------ receita


def test_filtro_de_tipo_de_video_mantem_as_sem_informacao(con):
    con.execute("UPDATE faixas SET tipo_video = 'OMV' WHERE video_id = 'rock00'")
    con.execute("UPDATE faixas SET tipo_video = 'UGC' WHERE video_id = 'rock01'")
    con.execute("UPDATE faixas SET tipo_video = 'ATV' WHERE video_id = 'rock02'")
    ex = executar(con, carregar({"nome": "estúdio", "tamanho": 50, "filtros": {
        "tipos_video": ["faixa"], "exigir_artistas": ["Banda Rock"]}}))  # fmt: skip
    assert sorted(ids(ex)) == ["rock02", "rock03"]  # rock03 sem tipo: fica
    assert ex.descartes["tipo de vídeo"] == 2


def test_tipo_de_video_desconhecido_e_recusado():
    with pytest.raises(ValueError, match="tipos_video"):
        carregar({"nome": "x", "filtros": {"tipos_video": ["video"]}})


def test_popularidade_no_perfil_alvo_ordena_das_mais_vistas_ao_lado_b(con):
    base = {"nome": "rock", "tamanho": 4, "filtros": {"exigir_artistas": ["Banda Rock"]},
            "diversidade": {"lambda": 1.0, "max_por_artista": None}}  # fmt: skip
    hits = executar(con, carregar({**base, "perfil_alvo": {"popularidade": {"alvo": 1.0}}}))
    lado_b = executar(con, carregar({**base, "perfil_alvo": {"popularidade": {"alvo": 0.0}}}))
    # percentis 0,875 / 0,5 / 0,125; rock03 sem o dado custa um desvio (≈ 0,29), como uma feature ausente
    assert ids(hits) == ["rock02", "rock03", "rock01", "rock00"]
    assert ids(lado_b) == ["rock00", "rock03", "rock01", "rock02"]


def test_popularidade_em_min_e_max_tira_as_sem_dado(con):
    ex = executar(con, carregar({"nome": "conhecidas", "filtros": {
        "exigir_artistas": ["Banda Rock"], "min": {"popularidade": 0.5}}}))  # fmt: skip
    assert sorted(ids(ex)) == ["rock01", "rock02"]
    assert ex.descartes["popularidade < 0.5"] == 2


def test_popularidade_fora_de_0_a_1_e_recusada(con):
    with pytest.raises(ReceitaInvalida, match="popularidade"):
        executar(con, carregar({"nome": "x", "perfil_alvo": {"popularidade": {"alvo": 2.0}}}))


def test_mesma_receita_e_mesmo_banco_dao_a_mesma_lista(con):
    r = carregar({"nome": "x", "tamanho": 8, "perfil_alvo": {"popularidade": {"alvo": 0.7}, "energy": {"alvo": 0.8}},
                  "filtros": {"tipos_video": ["faixa", "clipe"]}})  # fmt: skip
    a, b = executar(con, r), executar(con, r)
    assert [(i.video_id, i.score) for i in a.itens] == [(i.video_id, i.score) for i in b.itens]
    assert a.receita_hash == b.receita_hash


# ------------------------------------------------------------------ recomendação


@pytest.fixture
def yt():
    y = FalsoYT()
    y.radios = {
        "rock00": [faixa("velha", artistas=("A",)), faixa("nova", artistas=("B",)), faixa("hit", artistas=("C",)),
                   faixa("semdata", artistas=("D",))],
    }  # fmt: skip
    vistas = {"velha": "500", "nova": "50000", "hit": "900000000"}
    datas = {"velha": "2010-01-01", "nova": "2026-10-01", "hit": "2026-09-15"}
    y.musicas = {
        v: {"videoDetails": {"videoId": v, "viewCount": vistas[v]},
            "microformat": {"microformatDataRenderer": {"publishDate": datas[v] + "T00:00:00-07:00"}}}
        for v in vistas
    }  # fmt: skip
    y.musicas["semdata"] = {"videoDetails": {"videoId": "semdata"}}
    return y


def pedir(con, yt, config=None, **kw):
    p = rr.Pedido(**{"modo": "rapido", "escopo": "novas", "faixas": ["rock00"], "fontes": ["yt_radio"], **kw})
    c = config or carregar_config()
    c["recomendacoes"]["conferir_avaliacao"] = False
    return rr.recomendar(con, ClienteYT(yt, intervalo_s=0, dormir=lambda s: None), None, p, c)


def test_sem_popularidade_nem_data_nao_busca_estatisticas(con, yt):
    res = pedir(con, yt)
    assert yt.chamadas["get_song"] == 0
    assert [i.video_id for i in res.itens][0] == "velha"  # só a ordem do rádio


def test_publicadas_desde_deixa_so_os_lancamentos(con, yt):
    res = pedir(con, yt, publicadas_desde="2026-09-01")
    assert sorted(i.video_id for i in res.itens) == ["hit", "nova"]
    assert res.descartes["publicada antes"] == 1 and res.descartes["sem data de publicação"] == 1
    assert yt.chamadas["get_song"] == 4  # as 4 candidatas, uma vez cada


def test_popularidade_reordena_e_o_dado_fica_guardado(con, yt):
    res = pedir(con, yt, popularidade={"alvo": 1.0, "peso": 3})
    assert [i.video_id for i in res.itens][0] == "hit"
    assert "popularidade" in res.itens[0].componentes
    antes = yt.chamadas["get_song"]
    pedir(con, yt, popularidade={"alvo": 0.0})  # dentro da validade: nada de novo
    assert yt.chamadas["get_song"] == antes


def test_estatisticas_so_das_melhores_candidatas(con, yt):
    c = carregar_config()
    c["recomendacoes"]["estatisticas_top"] = 2
    pedir(con, yt, config=c, popularidade={"alvo": 1.0})
    assert yt.chamadas["get_song"] == 2


def test_refazer_a_recomendacao_da_a_mesma_lista_sem_rede(con, yt):
    res = pedir(con, yt, popularidade={"alvo": 0.2}, publicadas_desde="2000-01-01")
    chamadas = yt.chamadas["get_song"]
    refeita = rr.refazer(con, res.recomendacao_id, carregar_config())
    assert [(i.video_id, i.score) for i in refeita] == [(i.video_id, i.score) for i in res.itens]
    assert yt.chamadas["get_song"] == chamadas


def test_popularidade_do_pedido_fora_de_0_a_1_e_recusada():
    with pytest.raises(ValueError, match="popularidade"):
        rr.Pedido(modo="rapido", escopo="novas", faixas=["x"], popularidade={"alvo": 1.5})


# ------------------------------------------------------------------ MCP


def test_buscar_faixas_e_vocabulario_mostram_os_dados_de_uso(con, monkeypatch):
    from curator.interfaces import mcp

    con.execute("UPDATE faixas SET tipo_video = 'ATV', publicada_em = '2024-05-01' WHERE video_id = 'rock02'")
    r = {f["video_id"]: f for f in mcp.buscar_faixas("rock")}
    assert r["rock02"]["tipo_video"] == "faixa" and r["rock02"]["visualizacoes"] == 10_000_000
    assert r["rock02"]["publicada_em"] == "2024-05-01" and r["rock02"]["popularidade"] == pytest.approx(0.875)
    assert "popularidade" not in r["rock03"]
    voc = mcp.descrever_vocabulario("features")
    pop = next(i for i in voc["itens"] if i["feature"] == "popularidade")
    assert pop["cobertura"]["com_visualizacoes"] == 4
    assert voc["tipos_video"]["faixa"] == "ATV"
