import json

import pytest
from pydantic import ValidationError

from curator.motor.executor import executar
from curator.motor.receita import Receita, ReceitaInvalida, carregar, validar
from tests.falso_motor import biblioteca_analisada, playlist


@pytest.fixture
def con(tmp_path):
    con = biblioteca_analisada(tmp_path / "c.db")
    playlist(con, "PLrock", "Rock pesado", [f"rock{i:02d}" for i in range(6)])
    return con


# ------------------------------------------------------------------ schema e validação


def test_hash_canonico_ignora_ordem_das_chaves():
    a = carregar({"nome": "x", "perfil_alvo": {"energy": {"alvo": 0.9}, "relaxed": {"alvo": 0.1}}})
    b = carregar(
        json.dumps({"perfil_alvo": {"relaxed": {"alvo": 0.1}, "energy": {"alvo": 0.9}}, "nome": "x"})
    )
    assert a.hash() == b.hash()
    assert a.hash() != carregar({"nome": "x", "tamanho": 10}).hash()


def test_schema_estrito():
    with pytest.raises(ValidationError):
        carregar({"nome": "x", "campo_inventado": 1})
    with pytest.raises(ValidationError, match="mínimo maior"):
        carregar({"nome": "x", "filtros": {"bpm": [150, 90]}})
    assert carregar({"nome": "x", "diversidade": {"lambda": 0.5}}).diversidade.lambda_ == 0.5


@pytest.mark.parametrize(
    ("receita", "trecho"),
    [
        ({"filtros": {"excluir_artistas": ["Banda Roc"]}}, "quis dizer: Banda Rock"),
        ({"filtros": {"generos_min": {"Hip Hop---Trapp": 0.3}}}, "quis dizer: Hip Hop---Trap"),
        ({"perfil_alvo": {"energia": {"alvo": 0.5}}}, "quis dizer: energy"),
        ({"perfil_alvo": {"energy": {"alvo": 3}}}, "fora de 0..1"),
        ({"filtros": {"genero_pai_min": {"Hip-Hop": 0.3}}}, "quis dizer: Hip Hop"),
        ({"universo": "Rock pesadão"}, "quis dizer: Rock pesado"),
        ({"sementes": ["nao_existe"]}, "não existe ou ainda não foi analisada"),
    ],
)
def test_vocabulario_fechado_com_sugestao(con, receita, trecho):
    with pytest.raises(ReceitaInvalida, match=trecho):
        validar(con, carregar({"nome": "x", **receita}))


def test_artista_aceita_variacao_de_caixa_e_acento(con):
    validar(con, carregar({"nome": "x", "filtros": {"excluir_artistas": ["banda ROCK"]}}))


# ------------------------------------------------------------------ executor


def test_mesma_receita_mesmo_resultado_byte_a_byte(con):
    r = Receita(nome="energia", tamanho=10, perfil_alvo={"energy": {"alvo": 0.9}, "relaxed": {"alvo": 0.1}})
    a, b = executar(con, r), executar(con, r)
    dump = lambda e: json.dumps([vars(i) for i in e.itens], sort_keys=True)  # noqa: E731
    assert dump(a) == dump(b)
    assert a.receita_hash == b.receita_hash and a.versao_banco == b.versao_banco


def test_perfil_calmo_traz_ambient(con):
    r = Receita(nome="dormir", tamanho=8, perfil_alvo={"relaxed": {"alvo": 0.95}, "energy": {"alvo": 0.05}},
                diversidade={"max_por_artista": None})  # fmt: skip
    assert all(v.startswith("ambient") for v in executar(con, r).ids)


def test_genero_pai_como_filtro_e_ordenacao(con):
    r = Receita(
        nome="rap",
        tamanho=50,
        filtros={"genero_pai_min": {"Hip Hop": 0.8}},  # régua relativa ao núcleo do gênero (#83)
        diversidade={"max_por_artista": None, "lambda": 1},  # só relevância: a ordem é a da afinidade
    )
    e = executar(con, r)
    assert len(e.ids) == 12 and all(v.startswith("rap") for v in e.ids)
    assert e.descartes["gênero-pai abaixo do mínimo"] == 24
    assert [i.score for i in e.itens] == sorted((i.score for i in e.itens), reverse=True)


def test_limite_por_artista_aparece_nos_descartes(con):
    r = Receita(nome="rap", tamanho=50, filtros={"genero_pai_min": {"Hip Hop": 0.8}},
                diversidade={"max_por_artista": 2})  # fmt: skip
    e = executar(con, r)
    assert len(e.ids) == 2 and e.descartes["limite por artista"] == 10  # 12 raps do mesmo artista
    cheio = executar(con, r.model_copy(update={"tamanho": 2}))
    assert "limite por artista" not in cheio.descartes  # cortou pelo tamanho, não pelo limite


def test_exclusao_de_artista_nao_vaza_nem_por_feat(con):
    con.execute("UPDATE faixas SET titulo = 'Rock 3 (feat. Convidado Especial)' WHERE video_id = 'rock03'")
    r = Receita(nome="sem convidado", tamanho=100, filtros={"excluir_artistas": ["Convidado Especial"]},
                diversidade={"max_por_artista": None})  # fmt: skip
    ids = executar(con, r).ids
    assert "rap00" not in ids  # artista secundário
    assert "rock03" not in ids  # só no título
    assert len(ids) == 34


def test_excluir_album_e_playlist(con):
    r = Receita(nome="x", tamanho=100, filtros={"excluir_albuns_contendo": ["album AMBIENT"], "excluir_playlists": ["Rock pesado"]},
                diversidade={"max_por_artista": None})  # fmt: skip
    ids = executar(con, r).ids
    assert not any(v.startswith("ambient") for v in ids)
    assert not any(v in ids for v in [f"rock{i:02d}" for i in range(6)])


def test_sementes_puxam_o_estilo_das_sementes(con):
    r = Receita(
        nome="parecidas", tamanho=6, sementes=["rap01", "rap02"], diversidade={"max_por_artista": None}
    )
    assert all(v.startswith("rap") for v in executar(con, r).ids)


def test_componente_das_sementes_usa_a_regua_da_biblioteca(con):
    # v4: 0 = as faixas da biblioteca menos parecidas com as sementes, 1 = o centroide; não mais (cos + 1) / 2
    r = Receita(
        nome="parecidas", tamanho=1000, sementes=["rap01", "rap02"],
        diversidade={"lambda": 1.0, "max_por_artista": None},
    )  # fmt: skip
    comp = {i.video_id: i.componentes["sementes"] for i in executar(con, r).itens}
    assert min(comp.values()) == 0.0 and max(comp.values()) > 0.9
    assert min(v for k, v in comp.items() if k.startswith("rap")) > max(
        v for k, v in comp.items() if not k.startswith("rap")
    )


def test_limite_por_artista_e_universo_playlist(con):
    r = Receita(nome="x", tamanho=50, universo="PLrock", diversidade={"max_por_artista": 2})
    e = executar(con, r)
    assert e.universo == 6 and len(e.ids) == 2  # todas do mesmo artista


def test_mmr_diversifica_sem_perder_relevancia(con):
    base = dict(nome="x", tamanho=6, perfil_alvo={"energy": {"alvo": 0.7}})
    so_relevancia = executar(con, Receita(**base, diversidade={"lambda": 1.0, "max_por_artista": None})).ids
    diverso = executar(con, Receita(**base, diversidade={"lambda": 0.3, "max_por_artista": None})).ids
    assert so_relevancia[0] == diverso[0]  # o mais relevante sempre entra primeiro
    estilos = lambda ids: {v.rstrip("0123456789") for v in ids}  # noqa: E731
    assert len(estilos(diverso)) >= len(estilos(so_relevancia))


def test_faixas_sem_analise_sao_contadas(con):
    con.execute("DELETE FROM analises WHERE video_id LIKE 'rock%'")
    e = executar(con, Receita(nome="x", tamanho=100, diversidade={"max_por_artista": None}))
    assert e.sem_analise == 12 and len(e.ids) == 24
