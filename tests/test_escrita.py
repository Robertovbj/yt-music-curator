"""#111: escrita do título/álbum/artistas, o sinal de idioma e mercado que o áudio não dá."""

import json

import pytest

from curator.banco.conexao import agora
from curator.coleta.cliente import ClienteYT
from curator.config import caminhos, carregar_config
from curator.interfaces import mcp as m
from curator.motor import clusters
from curator.motor import escrita as es
from curator.motor import recomendar as rr
from curator.motor.executor import executar
from curator.motor.receita import carregar
from tests.falso_motor import biblioteca_analisada, playlist
from tests.falso_yt import FalsoYT, faixa


def nova(con, vid, titulo, artistas, album=None):
    con.execute(
        "INSERT INTO faixas (video_id, titulo, artistas, album_nome, duracao_s, curtida, criada_em, atualizada_em) "
        "VALUES (?, ?, ?, ?, 200, 0, ?, ?)",
        (vid, titulo, json.dumps([{"id": f"UC_{a}", "nome": a} for a in artistas]), album, agora(), agora()),
    )


@pytest.fixture
def con():
    c = biblioteca_analisada(caminhos().banco, por_estilo=6)
    # os títulos do rap viram japoneses (kana), os do ambient, só kanji, de um artista sem kana
    for i in range(6):
        c.execute("UPDATE faixas SET titulo = ? WHERE video_id = ?", (f"夜に駆ける {i}", f"rap{i:02d}"))
        c.execute("UPDATE faixas SET titulo = ? WHERE video_id = ?", (f"混沌 {i}", f"ambient{i:02d}"))
    c.execute("UPDATE faixas SET titulo = 'English Title' WHERE video_id = 'rap05'")
    return c


# ------------------------------------------------------------------ detecção


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("アイドル", {"japonesa"}),
        ("夜に駆ける", {"japonesa"}),  # kanji com kana: japonês, sem "han" em aberto
        ("怪物", {"han"}),  # só kanji: decide o artista
        ("락 (樂) LALALALA", {"coreana", "han", "latina"}),
        ("Под Новый Год", {"cirilica"}),
        ("Pokémon", {"latina"}),
        ("・ー～「」123", set()),  # pontuação e números não contam
    ],
)
def test_escritas_do_texto(texto, esperado):
    assert es.do_texto(texto) == esperado


def test_kanji_sozinho_segue_o_artista_e_titulo_latino_herda(con):
    e = es.calcular(con)
    assert e["rap00"] == {"japonesa"}
    assert e["rap05"] == {"japonesa"}  # "English Title" de artista japonês
    assert e["ambient00"] == {"chinesa"}  # só kanji, artista sem kana
    assert e["rock00"] == {"latina"}


def test_kanji_sozinho_de_artista_japones_e_japones(con):
    nova(con, "x1", "怪物", ["YOASOBI"])
    nova(con, "x2", "アイドル", ["YOASOBI"])
    assert es.calcular(con, ["x1"]) == {"x1": {"japonesa"}}


def test_uma_faixa_japonesa_nao_torna_o_artista_japones(con):
    # 1 de 6 faixas com kana (< LIMIAR_ARTISTA): as outras continuam latinas
    con.execute("UPDATE faixas SET titulo = 'さくら' WHERE video_id = 'rock00'")
    e = es.calcular(con)
    assert e["rock00"] == {"japonesa"} and e["rock01"] == {"latina"}


def test_parceria_tem_as_duas_escritas(con):
    nova(con, "k1", "사랑", ["Grupo K"])
    nova(con, "k2", "Love", ["Grupo K"])
    nova(con, "j1", "あい", ["Cantora J"])
    nova(con, "par", "Together", ["Cantora J", "Grupo K"])
    assert es.calcular(con, ["k2", "par"]) == {"k2": {"coreana"}, "par": {"coreana", "japonesa"}}


def test_proporcoes_e_versao(con):
    e = es.calcular(con)
    ids = ["rap00", "rap01", "rock00", "ambient00"]
    assert es.proporcoes(e, ids) == {"japonesa": 0.5, "chinesa": 0.25, "latina": 0.25}
    assert es.versao(e).startswith(f"v{es.VERSAO}:")


# ------------------------------------------------------------------ receitas


def test_receita_filtra_e_pontua_pela_escrita(con):
    base = {"nome": "x", "tamanho": 100, "diversidade": {"lambda": 1, "max_por_artista": None}}
    todos = executar(con, carregar(base))
    so_ja = executar(con, carregar({**base, "filtros": {"exigir_escritas": ["japonesa"]}}))
    assert set(so_ja.ids) == {f"rap{i:02d}" for i in range(6)}
    assert so_ja.descartes["sem a escrita exigida"] == 12
    sem_ja = executar(con, carregar({**base, "filtros": {"excluir_escritas": ["japonesa"]}}))
    assert not any(v.startswith("rap") for v in sem_ja.ids) and sem_ja.descartes["escrita excluída"] == 6
    pref = executar(con, carregar({**base, "escritas_preferidas": {"chinesa": 1}}))
    assert all(v.startswith("ambient") for v in pref.ids[:6])
    assert "+escrita:v1:" in pref.versao_banco and "+escrita:" not in todos.versao_banco


def test_receita_recusa_escrita_inexistente():
    with pytest.raises(ValueError, match="japonesa"):
        carregar({"nome": "x", "filtros": {"exigir_escritas": ["japones"]}})


def test_hash_das_receitas_antigas_nao_muda():
    assert carregar({"nome": "x"}).hash() == "1af3e30e91c09662"  # o mesmo de antes da #107 e da #111
    assert carregar({"nome": "x", "escritas_preferidas": {"japonesa": 1}}).hash() != "1af3e30e91c09662"


# ------------------------------------------------------------------ recomendações


def test_recomendacao_exige_ou_exclui_escrita(con):
    yt = FalsoYT()
    yt.radios = {"rap00": [faixa("n_ja", titulo="さよなら", artistas=("Nova J",)),
                           faixa("n_en", titulo="Goodbye", artistas=("Nova E",))]}  # fmt: skip
    cli = ClienteYT(yt, intervalo_s=0, dormir=lambda s: None)

    def pedir(**kw):
        p = rr.Pedido(modo="rapido", escopo="novas", faixas=["rap00"], fontes=["yt_radio"], **kw)
        return rr.recomendar(con, cli, None, p, carregar_config())

    so = pedir(exigir_escritas=["japonesa"])
    assert [i.video_id for i in so.itens] == ["n_ja"] and so.descartes["sem a escrita exigida"] == 1
    assert [i.video_id for i in pedir(excluir_escritas=["japonesa"]).itens] == ["n_en"]


def test_pedido_antigo_mantem_o_spec():
    p = rr.Pedido(modo="rapido", escopo="novas", faixas=["a"])
    assert "exigir_escritas" not in p.spec() and "excluir_escritas" not in p.spec()
    assert rr.Pedido(modo="rapido", escopo="novas", faixas=["a"], exigir_escritas=["coreana"]).spec()[
        "exigir_escritas"
    ] == ["coreana"]


# ------------------------------------------------------------------ nome dos clusters e MCP


def test_estilo_de_mercado_so_com_a_escrita_sustentando():
    est = {"estilos": [["Pop---J-pop", 0.4], ["Electronic---House", 0.3]], "energy": 0.5, "valence": 0.5}
    assert clusters.nome_automatico({**est, "escritas": {"latina": 0.9, "japonesa": 0.1}}).startswith("House")
    assert clusters.nome_automatico({**est, "escritas": {"japonesa": 0.7}}).startswith("J-pop")
    assert clusters.nome_automatico(est).startswith("J-pop")  # estatística antiga, sem escritas: como antes


def test_descricao_do_cluster_traz_as_escritas(con):
    est = clusters.descrever(con, ["rap00", "rap01", "rock00", "rock01"])
    assert est["escritas"] == {"japonesa": 0.5, "latina": 0.5}


def test_mcp_mostra_a_escrita(con):
    playlist(con, "PLx", "X", ["rap00"])
    achadas = {f["video_id"]: f for f in m.buscar_faixas("rap", limite=3)}
    assert achadas["rap00"]["escritas"] == ["japonesa"]
    voc = {i["nome"]: i for i in m.descrever_vocabulario("escritas")["itens"]}
    assert voc["japonesa"]["faixas"] == 6 and voc["chinesa"]["faixas"] == 6 and voc["coreana"]["faixas"] == 0
