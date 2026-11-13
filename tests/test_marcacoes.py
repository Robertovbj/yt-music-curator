"""#107: marcações por artista (k-pop, vtuber…), com exceção por faixa. Dados para filtro e fator nas receitas; as
regras de destino (etiqueta → master) ficam no config de cada pessoa e valem depois do áudio."""

import json
import sqlite3
import zipfile

import pytest
from typer.testing import CliRunner

from curator.banco import compartilhar, consultas
from curator.config import caminhos
from curator.interfaces import mcp as m
from curator.interfaces.cli import app
from curator.motor import classificar as cl
from curator.motor import marcacoes as mc
from curator.motor.executor import executar
from curator.motor.receita import ReceitaInvalida, carregar
from tests.falso_motor import biblioteca_analisada, playlist

# rap00 é parceria de "MC Teste" com "Convidado Especial" (ver falso_motor)
PESADO, CALMA, KPOP = "PLpesado", "PLcalma", "PLkpop"


@pytest.fixture
def con():
    c = biblioteca_analisada(caminhos().banco, por_estilo=14)
    # masters: as faixas 10–13 de cada estilo ficam de fora (curtidas novas)
    playlist(c, PESADO, "Pesado", [f"rock{i:02d}" for i in range(10)] + [f"rap{i:02d}" for i in range(10)])
    playlist(c, CALMA, "Calma", [f"ambient{i:02d}" for i in range(10)])
    playlist(c, KPOP, "K-pop", [f"rap{i:02d}" for i in range(5)])
    playlist(c, "PLminha", "Minha lógica", ["rap11"])  # não é master: nenhuma regra mexe nela
    c.execute(f"UPDATE playlists SET geral = 1 WHERE playlist_id IN ('{PESADO}', '{CALMA}', '{KPOP}')")
    return c


def regras_cfg(**extra) -> dict:
    return {
        "marcacoes": {
            "regras": [{"etiqueta": "k-pop", "entrar_em": ["K-pop"], "sair_de": ["Pesado"], **extra}]
        }
    }


# ------------------------------------------------------------------ dados


def test_etiqueta_canonica():
    assert mc.etiqueta("Pop ocidental") == "pop-ocidental"
    assert mc.etiqueta("K-pop") == "k-pop" and mc.etiqueta("VTuber") == "vtuber"
    with pytest.raises(mc.MarcacaoInvalida):
        mc.etiqueta("!!")


def test_artista_marca_todas_as_faixas_e_a_faixa_vence(con):
    assert mc.marcar_artista(con, "mc teste", "K-pop") == [("UC_rap", "MC Teste")]  # sem acento/caixa
    marcas = mc.da_faixa(con)
    assert {v for v, es in marcas.items() if "k-pop" in es} == {f"rap{i:02d}" for i in range(14)}
    mc.marcar_faixa(con, "rap03", "k-pop", False)  # exceção: esta não é
    mc.marcar_faixa(con, "rock00", "k-pop", True)  # e esta é, sem o artista
    marcas = mc.da_faixa(con)
    assert "rap03" not in marcas and marcas["rock00"] == {"k-pop"}
    mc.marcar_faixa(con, "rap03", "k-pop", None)  # apagar a exceção devolve o artista
    assert mc.da_faixa(con, ["rap03"]) == {"rap03": {"k-pop"}}
    mc.marcar_artista(con, "MC Teste", "k-pop", desfazer=True)
    assert set(mc.da_faixa(con)) == {"rock00"}
    assert mc.existentes(con) == ["k-pop"]


def test_parceria_fica_com_as_duas_etiquetas(con):
    mc.marcar_artista(con, "MC Teste", "vtuber")
    mc.marcar_artista(con, "Convidado Especial", "k-pop")
    assert mc.da_faixa(con, ["rap00", "rap01"]) == {"rap00": {"vtuber", "k-pop"}, "rap01": {"vtuber"}}


def test_artista_desconhecido_sugere(con):
    with pytest.raises(mc.MarcacaoInvalida, match="MC Teste"):
        mc.marcar_artista(con, "MC Tste", "k-pop")
    with pytest.raises(mc.MarcacaoInvalida):
        mc.marcar_faixa(con, "nao-existe", "k-pop", True)


def test_resumo_nao_conta_a_lista_negra(con):
    mc.marcar_artista(con, "MC Teste", "k-pop")
    consultas.ignorar_faixas(con, ["rap01"], "meme")
    (r,) = mc.resumo(con)
    assert r["etiqueta"] == "k-pop" and r["faixas"] == 13 and r["artistas"] == ["MC Teste"]


def test_fora_do_pacote_compartilhado(con, tmp_path):
    mc.marcar_artista(con, "MC Teste", "k-pop")
    mc.marcar_faixa(con, "rock00", "k-pop", True)
    ex = compartilhar.exportar(con, tmp_path / "p.zip", versao_analisador="1", hash_modelos="h")
    with zipfile.ZipFile(ex.caminho) as z:
        (tmp_path / "p.db").write_bytes(z.read(compartilhar.BANCO))
    tabelas = {r[0] for r in sqlite3.connect(tmp_path / "p.db").execute("SELECT name FROM sqlite_master")}
    assert not {"marcacoes_artista", "marcacoes_faixa"} & tabelas


# ------------------------------------------------------------------ regras de destino


def test_regras_resolvem_so_masters(con):
    (r,) = mc.regras(con, regras_cfg())
    assert (r.etiqueta, r.entrar_em, r.sair_de) == ("k-pop", [KPOP], [PESADO])
    assert mc.regras(con, {}) == [] and mc.regras(con, {"marcacoes": {"regras": []}}) == []
    with pytest.raises(mc.MarcacaoInvalida, match="não é uma playlist master"):
        mc.regras(con, {"marcacoes": {"regras": [{"etiqueta": "k-pop", "entrar_em": ["Minha lógica"]}]}})
    with pytest.raises(mc.MarcacaoInvalida, match="desconhecido"):
        mc.regras(con, regras_cfg(tirar_de=["Calma"]))
    with pytest.raises(mc.MarcacaoInvalida, match="sem entrar_em nem sair_de"):
        mc.regras(con, {"marcacoes": {"regras": [{"etiqueta": "k-pop"}]}})


def test_entrar_vence_sair():
    rs = [mc.Regra("vtuber", entrar_em=["VT"]), mc.Regra("k-pop", entrar_em=["K"], sair_de=["VT", "JR"])]
    assert mc.destinos_por_regra({"vtuber", "k-pop"}, rs) == ({"VT": "vtuber", "K": "k-pop"}, {"JR": "k-pop"})
    assert mc.destinos_por_regra({"outra"}, rs) == ({}, {})


def test_classificar_aplica_regras_depois_do_audio(con):
    sem = cl.classificar(con, nas_gerais=True)
    mc.marcar_artista(con, "MC Teste", "k-pop")
    assert _resumo(cl.classificar(con, nas_gerais=True)) == _resumo(sem)  # sem regra, nada muda
    com = cl.classificar(con, nas_gerais=True, regras=mc.regras(con, regras_cfg()))
    antes = {d.video_id: d for d in sem.decisoes}
    for d in com.decisoes:
        audio = {x.playlist_id: x for x in antes[d.video_id].destinos}
        if d.video_id.startswith("rap"):
            assert KPOP in {x.playlist_id for x in d.destinos}  # pelo áudio ou pela regra
            if KPOP not in audio:
                assert next(x for x in d.destinos if x.playlist_id == KPOP).regra == "k-pop"
            assert PESADO not in {x.playlist_id for x in d.destinos}
            assert {x.playlist_id for x, _ in d.barradas} == {PESADO} & set(audio)
        else:  # sem etiqueta: idêntico ao áudio, com os mesmos scores
            assert d.destinos == antes[d.video_id].destinos and not d.barradas
        for x in d.destinos:  # o áudio não muda: o score é o mesmo com ou sem regra
            if x.playlist_id in audio:
                assert x.score == audio[x.playlist_id].score


def test_regra_poe_onde_o_audio_nao_poria(con):
    from curator.motor import planos

    mc.marcar_artista(con, "MC Teste", "vtuber")
    cfg = {"marcacoes": {"regras": [{"etiqueta": "vtuber", "entrar_em": ["Calma"]}]}}
    c = cl.classificar(con, nas_gerais=True, regras=mc.regras(con, cfg))
    for d in c.decisoes:
        if d.video_id.startswith("rap"):
            x = next(x for x in d.destinos if x.playlist_id == CALMA)
            assert x.regra == "vtuber" and x.score < x.limiar  # o score fica, para informação
            assert d.destinos[-1] is x and not d.proximas  # o áudio vem antes
    pid = planos.de_classificacao(con, c.decisoes)
    motivos = {o.playlist: o.motivo for o in planos.obter(con, pid)[1]}
    assert motivos[CALMA]["rap10"] == "marcação vtuber"


def _resumo(c):
    return [(d.video_id, [(x.playlist_id, x.score) for x in d.destinos]) for d in c.decisoes]


def test_regra_so_vale_nas_playlists_pedidas(con):
    mc.marcar_artista(con, "MC Teste", "k-pop")
    c = cl.classificar(con, em=[CALMA], regras=mc.regras(con, regras_cfg()))
    assert all(x.playlist_id == CALMA for d in c.decisoes for x in d.destinos)


def test_plano_corrige_so_o_que_contraria(con):
    from curator.motor import planos

    mc.marcar_artista(con, "MC Teste", "k-pop")
    con.execute("UPDATE faixas SET curtida = 0 WHERE video_id = 'rap12'")  # não curtida: não entra
    consultas.ignorar_faixas(con, ["rap13"], "meme")  # lista negra: não entra
    pid = mc.plano(con, mc.regras(con, regras_cfg()))
    ops = {(o.tipo, o.playlist): set(o.video_ids) for o in planos.obter(con, pid)[1]}
    assert ops[("remover", PESADO)] == {f"rap{i:02d}" for i in range(10)}
    assert ops[("adicionar", KPOP)] == {f"rap{i:02d}" for i in (*range(5, 12),)}
    assert set(ops) == {("remover", PESADO), ("adicionar", KPOP)}  # Calma e "Minha lógica" intocadas
    # aplicado, não sobra nada a corrigir
    con.execute(f"DELETE FROM playlist_faixas WHERE playlist_id = '{PESADO}' AND video_id LIKE 'rap%'")
    con.executemany(f"INSERT INTO playlist_faixas (playlist_id, posicao, video_id) VALUES ('{KPOP}', ?, ?)",
                    [(100 + i, f"rap{i:02d}") for i in range(5, 12)])  # fmt: skip
    with pytest.raises(ValueError, match="nada a corrigir"):
        mc.plano(con, mc.regras(con, regras_cfg()))


# ------------------------------------------------------------------ receitas


def test_receita_filtra_e_pontua_por_marcacao(con):
    mc.marcar_artista(con, "MC Teste", "k-pop")
    mc.marcar_artista(con, "Ambientista", "vtuber")
    base = {"nome": "x", "tamanho": 100, "diversidade": {"lambda": 1, "max_por_artista": None}}
    todos = executar(con, carregar(base))
    sem_k = executar(con, carregar({**base, "filtros": {"excluir_marcacoes": ["k-pop"]}}))
    assert not any(v.startswith("rap") for v in sem_k.ids) and sem_k.descartes["marcação excluída"] == 14
    so = executar(con, carregar({**base, "filtros": {"exigir_marcacoes": ["k-pop", "vtuber"]}}))
    assert {v.rstrip("0123456789") for v in so.ids} == {"rap", "ambient"}
    pref = executar(con, carregar({**base, "marcacoes_preferidas": {"vtuber": 1}}))
    assert all(v.startswith("ambient") for v in pref.ids[:14])
    assert "+marcacoes:" in pref.versao_banco and "+marcacoes:" not in todos.versao_banco


def test_receita_recusa_marcacao_inexistente(con):
    mc.marcar_artista(con, "MC Teste", "k-pop")
    with pytest.raises(ReceitaInvalida, match="k-pop"):
        executar(con, carregar({"nome": "x", "filtros": {"excluir_marcacoes": ["kpop"]}}))


def test_hash_das_receitas_antigas_nao_muda():
    # os mesmos hashes do master antes da #107: os campos novos só entram no hash quando usados
    assert carregar({"nome": "x"}).hash() == "1af3e30e91c09662"
    assert carregar({"nome": "x", "marcacoes_preferidas": {"k-pop": 1}}).hash() != "1af3e30e91c09662"


# ------------------------------------------------------------------ MCP e CLI


def test_mcp_marca_lista_e_mostra_nas_buscas(con):
    assert m.marcar_artista("MC Tste", "k-pop")["erros"]
    assert m.marcar_artista("MC Teste", "K-pop")["etiqueta"] == "k-pop"
    assert m.marcar_faixa("rap01", "k-pop", False)["etiquetas_da_faixa"] == []
    r = m.listar_marcacoes()
    assert r["marcacoes"][0]["faixas"] == 13 and r["regras"] == []
    achadas = {f["video_id"]: f for f in m.buscar_faixas("rap", limite=50)}
    assert achadas["rap00"]["marcacoes"] == ["k-pop"] and "marcacoes" not in achadas["rap01"]
    assert m.descrever_vocabulario("marcacoes")["itens"] == [{"nome": "k-pop", "faixas": 13, "artistas": 1}]
    assert {"listar_marcacoes", "marcar_artista", "marcar_faixa", "criar_plano_das_marcacoes"} <= {
        f.__name__ for f in m.FERRAMENTAS
    }


def test_mcp_classifica_com_as_regras_do_config(con):
    mc.marcar_artista(con, "MC Teste", "k-pop")
    caminhos().config_toml.parent.mkdir(parents=True, exist_ok=True)
    caminhos().config_toml.write_text(
        '[[marcacoes.regras]]\netiqueta = "k-pop"\nentrar_em = ["K-pop"]\nsair_de = ["Pesado"]\n',
        encoding="utf-8",
    )
    r = m.classificar_nao_alocadas(nas_gerais=True)
    raps = [d for d in r["decisoes"] if d["video_id"].startswith("rap")]
    assert raps and all("K-pop" in [x["titulo"] for x in d["destinos"]] for d in raps)
    assert all("Pesado" not in [x["titulo"] for x in d["destinos"]] for d in raps)
    assert m.listar_marcacoes()["regras"] == [
        {"etiqueta": "k-pop", "entrar_em": ["K-pop"], "sair_de": ["Pesado"]}
    ]
    assert "marcação k-pop" in m.criar_plano_das_marcacoes()["diff"]


def test_cli_grava_tudo_ou_nada(con):
    run = CliRunner().invoke
    r = run(app, ["marcacoes", "artista", "vtuber", "MC Teste", "Ninguém"])
    assert r.exit_code == 1 and mc.da_faixa(con) == {}  # o segundo nome não existe: nada gravado
    r = run(app, ["marcacoes", "artista", "vtuber", "MC Teste", "Ambientista"])
    assert r.exit_code == 0, r.output
    r = run(app, ["marcacoes", "faixa", "vtuber", "--nao", "--", "rap02"])
    assert r.exit_code == 0 and "sem 'vtuber'" in r.output
    r = run(app, ["marcacoes", "listar"])
    assert "vtuber: 27 faixa(s) · 2 artista(s)" in r.output and "rap02" in r.output
    assert "nenhuma regra" in run(app, ["marcacoes", "regras"]).output
    r = run(app, ["classificar", "--nas-gerais", "--json"])
    assert r.exit_code == 0 and json.loads(r.output)
