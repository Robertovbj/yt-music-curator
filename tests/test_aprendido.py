"""#93: gênero-pai aprendido com os rótulos do usuário (nunca com as playlists)."""

import json
import zipfile

import numpy as np
import pytest

from curator.banco import vetores
from curator.config import caminhos
from curator.motor import aprendido, espacos, generos
from curator.motor.executor import VERSAO_MOTOR, executar
from curator.motor.receita import carregar
from tests.falso_motor import ROTULOS, biblioteca_analisada, playlist

DISFARCADAS = [f"rock{i:02d}" for i in range(24, 30)]


@pytest.fixture
def con():
    """30 faixas por estilo, 3 artistas por estilo. As rock24–29 soam como rock (embedding), mas o Discogs as põe
    no Electronic: o caso do rock japonês que o Discogs chama de J-pop."""
    c = biblioteca_analisada(caminhos().banco, por_estilo=30)
    for (vid,) in c.execute("SELECT video_id FROM faixas").fetchall():
        estilo, i = vid.rstrip("0123456789"), int(vid[-2:])
        a = [{"id": f"UC_{estilo}{i % 3}", "nome": f"{estilo.title()} {i % 3}"}]
        c.execute("UPDATE faixas SET artistas = ? WHERE video_id = ?", (json.dumps(a), vid))
    eletronico = np.full(len(ROTULOS), 0.01, np.float32)
    eletronico[ROTULOS.index("Electronic---Ambient")] = 0.7
    for vid in DISFARCADAS:
        vetores.gravar(c, vid, "discogs400", eletronico)
    for i in range(24):
        aprendido.rotular(c, f"rock{i:02d}", "Rock", True, "gabarito_artista")
        aprendido.rotular(c, f"rap{i:02d}", "Hip Hop", True, "gabarito_artista")
        aprendido.rotular(c, f"ambient{i:02d}", "Electronic", True, "gabarito_artista")
    espacos._cache.clear()
    return c


def test_so_tem_modelo_quem_tem_rotulos_suficientes(con):
    ap = aprendido.obter(con)
    assert set(ap.modelos) == {"Rock", "Hip Hop", "Electronic"}
    m = ap.modelos["Rock"]
    assert (m.positivos, m.negativos, m.artistas) == (
        24,
        48,
        3,
    )  # os negativos: os marcados como de outro pai
    con.execute("DELETE FROM rotulos_genero WHERE video_id IN ('rap00', 'rap01', 'rap02', 'rap03', 'rap04')")
    assert "Hip Hop" not in aprendido.obter(con).modelos  # 19 positivos: fica com a régua do ADR-14


def test_a_aprendida_poe_o_rock_disfarcado_no_rock_e_a_v2_nao(con):
    afin = generos.Afinidades(con)
    assert (afin.relativa("Rock", DISFARCADAS) < 0.8).all()
    assert (afin.aprendido.relativa("Rock", DISFARCADAS) >= 0.8).all()  # o modelo, sem portão nem guarda
    # a guarda (#101) corta no percentil 5 das rotuladas: a disfarçada mais longe de todas fica com a v2
    dentro = afin.aprendido.dentro_da_guarda(DISFARCADAS)
    assert dentro.sum() == 5 and not dentro[DISFARCADAS.index("rock29")]
    rel = afin.relativa_aprendida("Rock", DISFARCADAS)
    assert (rel[dentro] >= 0.8).all() and rel[~dentro] == afin.relativa("Rock", ["rock29"])
    assert (afin.relativa_aprendida("Rock", ["ambient25", "rap25"]) < 0.5).all()
    # o Discogs dizia Electronic; nas que o modelo julga, deixa de dizer
    assert (afin.relativa_aprendida("Electronic", DISFARCADAS)[dentro] < 0.5).all()
    assert np.isnan(afin.relativa_aprendida("Jazz", DISFARCADAS)).all()  # sem rótulos: sem régua aprendida
    assert np.array_equal(
        afin.relativa_receita("Rock", DISFARCADAS), afin.relativa("Rock", DISFARCADAS)
    )  # receita: v2


def test_os_rotulos_vencem_o_modelo(con):
    aprendido.rotular(con, "rock25", "Rock", False, "cli")
    aprendido.rotular(con, "ambient03", "Rock", True, "mcp")
    afin = generos.Afinidades(con)
    r = dict(
        zip(["rock25", "ambient03"], afin.relativa_aprendida("Rock", ["rock25", "ambient03"]), strict=True)
    )
    assert r["rock25"] == 0.0 and r["ambient03"] >= 1.0


def test_mesmos_rotulos_e_mesma_biblioteca_dao_a_mesma_afinidade(con):
    a = aprendido.Aprendido(con).relativa("Rock", DISFARCADAS)
    b = aprendido.Aprendido(con).relativa("Rock", DISFARCADAS)
    assert np.array_equal(a, b)
    v = aprendido.hash_rotulos(con)
    aprendido.rotular(con, "ambient00", "Rock", False, "cli")
    assert aprendido.hash_rotulos(con) != v and aprendido.obter(con).versao != v


def test_nenhum_rotulo_vem_das_playlists(con):
    antes = aprendido.Aprendido(con).relativa("Rock", ["ambient00", "ambient01"])
    playlist(con, "PLr", "Rock", ["ambient00", "ambient01"])  # uma playlist "Rock" com outra coisa dentro
    assert (
        con.execute(
            "SELECT count(*) FROM rotulos_genero WHERE pai = 'Rock' AND video_id LIKE 'ambient%'"
        ).fetchone()[0]
        == 0
    )
    assert np.array_equal(aprendido.Aprendido(con).relativa("Rock", ["ambient00", "ambient01"]), antes)


def test_receita_fica_na_v2_ate_a_aprendida_ser_adotada(con, monkeypatch):
    receita = carregar({"nome": "rock", "tamanho": 100, "filtros": {"genero_pai_min": {"Rock": 0.8}},
                        "diversidade": {"lambda": 1.0, "max_por_artista": None}})  # fmt: skip
    hoje = executar(con, receita)
    assert not set(DISFARCADAS) & set(hoje.ids) and "+rotulos:" not in hoje.versao_banco
    assert hoje.versao_motor == VERSAO_MOTOR == "4"
    monkeypatch.setattr(generos, "REGUA_APRENDIDA", frozenset({"Rock"}))
    ex = executar(con, carregar({"nome": "rock", "tamanho": 100, "filtros": {"genero_pai_min": {"Rock": 0.8}},
                                 "diversidade": {"lambda": 1.0, "max_por_artista": None}}))  # fmt: skip
    julgadas = {
        v for v, d in zip(DISFARCADAS, aprendido.obter(con).dentro_da_guarda(DISFARCADAS), strict=True) if d
    }
    assert julgadas and julgadas <= set(ex.ids) and "rock29" not in ex.ids
    assert not any(v.startswith(("rap", "ambient")) for v in ex.ids)
    assert ex.versao_banco.endswith("+rotulos:" + aprendido.hash_rotulos(con))
    sem_genero = executar(con, carregar({"nome": "x", "tamanho": 5}))
    assert "+rotulos:" not in sem_genero.versao_banco


def test_importar_gabarito_nao_troca_o_rotulo_da_faixa(con):
    aprendido.rotular(con, "rock29", "Rock", False, "cli")
    gab = {
        "artistas": {"Rock": ["Rock 2"]},
        "faixas": {"Hip Hop": {"sim": ["rap27"], "nao": ["rock00", "xyz"]}},
    }
    c = aprendido.importar_gabarito(con, gab)
    assert c["faixa"] == 2 and c["faixa_desconhecida"] == 1
    linha = con.execute(
        "SELECT sim, origem FROM rotulos_genero WHERE video_id = 'rock29' AND pai = 'Rock'"
    ).fetchone()
    assert tuple(linha) == (0, "cli")
    assert (
        con.execute("SELECT sim FROM rotulos_genero WHERE video_id = 'rock26' AND pai = 'Rock'").fetchone()[0]
        == 1
    )


def test_duvidosas_primeiro_as_que_as_reguas_discordam(con):
    lista = aprendido.duvidosas(con, "Rock", n=6)
    assert lista[0]["motivo"] == "as réguas discordam"
    assert {d["video_id"] for d in lista[:4]} <= set(DISFARCADAS)
    assert all(not d["video_id"].startswith(("rock0", "rock1")) for d in lista)  # nunca uma já rotulada
    artistas = [
        con.execute("SELECT artistas FROM faixas WHERE video_id = ?", (d["video_id"],)).fetchone()[0]
        for d in lista
    ]
    assert max(artistas.count(a) for a in artistas) <= 2
    assert aprendido.duvidosas(con, "Rock", n=6) == lista  # reprodutível


def test_validar_deixa_o_artista_de_fora(con):
    v = aprendido.validar(con, "Rock")
    assert v["artistas"] == 9 and v["positivos"] == 24 and v["negativos"] == 48
    assert v["auc"] > 0.9
    assert aprendido.validar(con, "Jazz") is None


def test_rotulos_nao_vao_no_pacote_compartilhado(con, tmp_path):
    from curator.banco import compartilhar

    ex = compartilhar.exportar(con, tmp_path / "p.zip", versao_analisador="1", hash_modelos="h")
    with zipfile.ZipFile(ex.caminho) as z:
        (tmp_path / "p.db").write_bytes(z.read(compartilhar.BANCO))
    import sqlite3

    tabelas = {r[0] for r in sqlite3.connect(tmp_path / "p.db").execute("SELECT name FROM sqlite_master")}
    assert "rotulos_genero" not in tabelas


def test_mcp_examina_rotula_e_pergunta(con):
    from curator.interfaces import mcp

    r = mcp.examinar_genero("Rock", n=40)
    assert r["regua"]["tipo"] != "aprendida" and r["regua"]["aprendida"]["na_receita"] is False
    itens = {i["video_id"]: i for i in r["acima"] + r["abaixo"]}
    assert "afinidade_aprendida" in itens["rock00"]
    assert itens["rock00"]["rotulo_do_usuario"] == "sim"
    pergunta = mcp.faixas_para_rotular("Rock", 3)
    assert len(pergunta["faixas"]) == 3 and "titulo" in pergunta["faixas"][0]
    assert mcp.rotular_genero("rock26", "Rock", True)["ok"]
    assert (
        con.execute(
            "SELECT origem FROM rotulos_genero WHERE video_id = 'rock26' AND pai = 'Rock'"
        ).fetchone()[0]
        == "mcp"
    )
    assert "erros" in mcp.rotular_genero("rock26", "Rok", True)


def test_cli_status_e_rotulo(con):
    from typer.testing import CliRunner

    from curator.interfaces.cli import app

    run = CliRunner()
    r = run.invoke(app, ["generos", "status", "--validar"])
    assert r.exit_code == 0, r.output
    assert "aprendido" in r.output and "com a guarda  AUC" in r.output and "(artista de fora;" in r.output
    r = run.invoke(app, ["generos", "rotulo", "rock27", "Rock", "nao"])
    assert r.exit_code == 0 and "Rock = nao" in r.output
    assert run.invoke(app, ["generos", "rotulo", "rock27", "Rock", "talvez"]).exit_code != 0
    r = run.invoke(app, ["generos", "rotular", "Rock", "-n", "2"], input="s\nq\n")
    assert r.exit_code == 0 and "1 rótulo(s)" in r.output


def test_portao_o_modelo_nao_julga_o_que_a_v2_poe_num_pai_sem_rotulos(con):
    """Sem rótulos de Electronic, as disfarçadas (Electronic na v2) ficam com a v2 em todos os pais, mas um rótulo
    do usuário ainda vence."""
    con.execute("DELETE FROM rotulos_genero WHERE pai = 'Electronic'")
    afin = generos.Afinidades(con)
    assert "Electronic" not in afin.aprendido.modelos
    assert afin.fora_do_aprendido(DISFARCADAS).all() and not afin.fora_do_aprendido(["rock00", "rap00"]).any()
    assert np.array_equal(afin.relativa_aprendida("Rock", DISFARCADAS), afin.relativa("Rock", DISFARCADAS))
    aprendido.rotular(con, "rock25", "Rock", True, "cli")
    afin = generos.Afinidades(con)
    assert afin.relativa_aprendida("Rock", ["rock25"])[0] >= 1.0


# ------------------------------------------------------------------ guarda (#101)


def _trilhas(con, n=6):
    """ambient24–29 viram faixas que não se parecem com nada rotulado (embedding longe de tudo), mas que a v2 ainda
    põe no Electronic, um pai com modelo: o caso das trilhas de jogo. O portão não as barra; a guarda, sim."""
    rng = np.random.default_rng(7)
    longe = rng.standard_normal(32) * 3
    ids = [f"ambient{i:02d}" for i in range(24, 24 + n)]
    for vid in ids:
        vetores.gravar(con, vid, "effnet_emb", (longe + rng.standard_normal(32)).astype(np.float32))
    espacos._cache.clear()
    return ids


def test_guarda_barra_o_que_nao_se_parece_com_nada_rotulado(con):
    trilhas = _trilhas(con)
    afin = generos.Afinidades(con)
    assert not afin.fora_do_aprendido(trilhas).any()  # o portão deixa passar
    assert not afin.aprendido.dentro_da_guarda(trilhas).any() and afin.nao_julgadas(trilhas).all()
    for pai in ("Rock", "Hip Hop", "Electronic"):
        assert np.array_equal(afin.relativa_aprendida(pai, trilhas), afin.relativa(pai, trilhas))
    perto = [f"rock{i:02d}" for i in range(24, 29)]
    assert afin.aprendido.dentro_da_guarda(perto).all()
    aprendido.rotular(con, trilhas[0], "Electronic", False, "mcp")  # o rótulo ainda vence
    assert generos.Afinidades(con).relativa_aprendida("Electronic", trilhas[:1])[0] == 0.0


def test_guarda_e_deterministica_e_nao_conta_a_propria_faixa(con):
    a, b = aprendido.Aprendido(con), aprendido.Aprendido(con)
    ids = espacos.da_biblioteca(con)
    assert np.array_equal(a.proximidade(ids), b.proximidade(ids)) and a.corte_guarda == b.corte_guarda
    s = a.proximidade(["rock00"])[0]  # rotulada: medida contra as outras, então abaixo de 1
    assert 0 < s < 0.99
    assert np.isclose(a.corte_guarda, np.percentile(a.proximidade(a._rotuladas), aprendido.PERCENTIL_GUARDA))


def test_validar_mede_a_guarda_com_o_artista_de_fora(con):
    _trilhas(con)
    v = aprendido.validar(con, "Rock")
    assert (
        set(v["com_guarda"]) == {"auc", "revocacao_0_8", "falsos_positivos_0_8"}
        and v["com_guarda"]["auc"] > 0.9
    )
    assert v["v2"]["auc"] is not None and 0.5 < v["julgadas_pela_guarda"] <= 1.0
    r = aprendido.fora_da_amostra(con, "Rock")
    assert np.array_equal(r["com_guarda"][~r["dentro"]], r["v2"][~r["dentro"]])


def test_mcp_diz_quando_a_faixa_ficou_fora_da_guarda(con):
    from curator.interfaces import mcp

    trilhas = _trilhas(con)
    r = mcp.examinar_genero("Electronic", limite=0.8, n=60)
    itens = {i["video_id"]: i for i in r["acima"] + r["abaixo"]}
    vistas = [v for v in trilhas if v in itens]
    assert vistas and all(itens[v].get("aprendida_nao_julga") for v in vistas)
    assert not any(itens[v].get("aprendida_nao_julga") for v in itens if v.startswith("rap"))


def test_hip_hop_e_pop_usam_a_aprendida_e_os_outros_a_v2(con):
    """Decisão do usuário em 09/11 (#101): a régua aprendida entra nas receitas só no Hip Hop e no Pop."""
    assert {"Hip Hop", "Pop"} == generos.REGUA_APRENDIDA
    afin = generos.Afinidades(con)
    assert afin.usa_aprendida("Hip Hop") and not afin.usa_aprendida("Rock")
    assert not afin.usa_aprendida("Pop")  # sem rótulos de Pop no teste: sem modelo, fica na v2
    ids = espacos.da_biblioteca(con)
    assert np.array_equal(afin.relativa_receita("Hip Hop", ids), afin.relativa_aprendida("Hip Hop", ids))
    assert np.array_equal(afin.relativa_receita("Rock", ids), afin.relativa("Rock", ids))
    rap = executar(
        con, carregar({"nome": "rap", "tamanho": 50, "filtros": {"genero_pai_min": {"Hip Hop": 0.8}}})
    )
    assert rap.versao_banco.endswith("+rotulos:" + aprendido.hash_rotulos(con)) and rap.versao_motor == "4"
