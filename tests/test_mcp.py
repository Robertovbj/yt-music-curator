import asyncio
import json
from pathlib import Path

import pytest

from curator.config import caminhos
from curator.interfaces import mcp as m
from curator.motor.receita import carregar
from tests.falso_motor import biblioteca_analisada, playlist


@pytest.fixture
def con():
    con = biblioteca_analisada(caminhos().banco, por_estilo=14)
    playlist(con, "PLcalma", "Calma", [f"ambient{i:02d}" for i in range(10)])
    playlist(con, "PLrock", "Rock", [f"rock{i:02d}" for i in range(10)])
    con.execute("UPDATE faixas SET album_nome = 'Genshin Impact - Jadeite' WHERE video_id = 'ambient12'")
    return con


def test_leitura(con):
    arv = {p["titulo"]: p for p in m.arvore_playlists()}
    assert arv["Calma"]["faixas"] == 10 and arv["Calma"]["analisadas"] == 10
    assert m.estatisticas()["biblioteca"] == 42
    assert [a["nome"] for a in m.buscar_artistas("ambient")] == ["Ambientista"]
    assert m.buscar_albuns("genshin")[0]["album"] == "Genshin Impact - Jadeite"
    achadas = m.buscar_faixas("rock 1")
    assert achadas[0]["video_id"].startswith("rock") and achadas[0]["analisada"]


def test_vocabulario(con):
    f = {x["feature"]: x for x in m.descrever_vocabulario("features")["itens"]}
    assert f["energy"]["p10"] < f["energy"]["mediana"] < f["energy"]["p90"]
    pais = {x["nome"]: x["faixas_dominante"] for x in m.descrever_vocabulario("generos_pai")["itens"]}
    assert pais == {"Rock": 14, "Electronic": 14, "Hip Hop": 14}
    assert [x["nome"] for x in m.descrever_vocabulario("estilos", filtro="trap")["itens"]] == [
        "Hip Hop---Trap"
    ]


def test_executar_receita_devolve_erros_para_a_ia_corrigir(con):
    r = m.executar_receita({"nome": "x", "filtros": {"excluir_artistas": ["Ambientisa"]}})
    assert "erros" in r and "quis dizer: Ambientista" in r["erros"][0]
    r = m.executar_receita({"nome": "x", "campo_que_nao_existe": 1})
    assert "erros" in r


def test_fluxo_receita_cache_e_plano(con):
    receita = {"nome": "Dormir", "tamanho": 5, "perfil_alvo": {"relaxed": {"alvo": 0.95}},
               "filtros": {"excluir_albuns_contendo": ["genshin"]}, "diversidade": {"max_por_artista": None}}  # fmt: skip
    r = m.executar_receita(receita)
    assert r["total"] == 5 and all(i["video_id"].startswith("ambient") for i in r["itens"])
    assert "ambient12" not in [i["video_id"] for i in r["itens"]]  # álbum excluído
    assert m.salvar_receita(receita, "Quero músicas para DORMIR") == {
        "nome": "Dormir",
        "hash": carregar(receita).hash(),
    }
    assert m.obter_receita_por_pedido("quero musicas para dormir")["nome"] == "Dormir"
    assert m.obter_receita_por_pedido("outra coisa") is None
    p = m.criar_plano_da_receita("Dormir")
    assert (
        p["status"] == "pendente"
        and "Dormir (nova)" in p["diff"]
        and "curator plano aplicar" in p["como_aplicar"]
    )


def test_classificar_e_plano(con):
    assert "diga em quais playlists" in m.classificar_nao_alocadas()["erros"][0]
    c = m.classificar_nao_alocadas(playlists=["Calma", "Rock"])
    assert c["total"] == 22 and c["com_destino"] >= 4  # rap não tem playlist
    assert "erros" in m.criar_plano_da_classificacao()
    p = m.criar_plano_da_classificacao(playlists=["Calma", "Rock"])
    assert p["plano_id"] and "■" in p["diff"]


def test_gerais_e_renomear(con):
    pytest.importorskip("umap")
    r = m.sugerir_playlists_gerais(min_cluster=6)
    assert r["playlists"] and r["curtidas"] == 42
    cid = r["playlists"][0]["cluster_id"]
    assert m.renomear_cluster(cid, "Nome da IA", gerais=True)["nome"] == "Nome da IA"
    assert "Nome da IA" in {p["nome"] for p in m.sugerir_playlists_gerais(min_cluster=6)["playlists"]}
    p = m.criar_plano_das_gerais(min_cluster=6)
    assert "Nome da IA (nova)" in p["diff"]
    assert "erros" in m.sugerir_reorganizacao(mae="não existe")


def test_exemplos_validam_contra_o_schema():
    exemplos = m.exemplos_de_receitas()
    assert len(exemplos) == 6
    for e in exemplos:
        if "receita" in e:
            carregar(e["receita"])  # levanta se fugir do schema


def test_exemplos_usam_rotulos_reais_do_discogs_e_jamendo():
    pasta = Path.home() / ".local/share/yt-music-curator/models"
    arquivos = ["genre_discogs400-discogs-effnet-1", "mtg_jamendo_moodtheme-discogs-effnet-1"]
    if not all((pasta / f"{a}.json").exists() for a in arquivos):
        pytest.skip("modelos não instalados")
    rotulos = {r for a in arquivos for r in json.loads((pasta / f"{a}.json").read_text())["classes"]}
    pais = {r.split("---")[0] for r in rotulos if "---" in r}
    for e in m.exemplos_de_receitas():
        r = e.get("receita", {})
        for rot in [*r.get("generos_preferidos", {}), *r.get("filtros", {}).get("generos_min", {})]:
            assert rot in rotulos, rot
        for pai in r.get("filtros", {}).get("genero_pai_min", {}):
            assert pai in pais, pai


def test_protocolo_lista_ferramentas_sem_nenhuma_que_aplique(con):
    pytest.importorskip("mcp")
    servidor = m.criar_servidor()
    nomes = {t.name for t in asyncio.run(servidor.list_tools())}
    assert {"executar_receita", "buscar_artistas", "sugerir_reorganizacao", "criar_plano_da_receita"} <= nomes
    assert not nomes & m.PROIBIDAS and not any("aplicar" in n for n in nomes)
    res = asyncio.run(servidor.call_tool("buscar_artistas", {"texto": "mc"}))
    assert "MC Teste" in json.dumps(res.model_dump(), ensure_ascii=False)
