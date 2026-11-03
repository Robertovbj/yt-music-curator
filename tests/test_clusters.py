import numpy as np
import pytest

pytest.importorskip("umap")
pytest.importorskip("sklearn")

from curator.audio.analisador import Resultado  # noqa: E402
from curator.audio.persistir import Origem, gravar_analise  # noqa: E402
from curator.motor import clusters as cl  # noqa: E402
from tests.falso_motor import biblioteca_analisada  # noqa: E402

P = cl.Parametros(min_cluster_size=8, n_neighbors=10)


@pytest.fixture
def con(tmp_path):
    return biblioteca_analisada(tmp_path / "c.db", por_estilo=25)


def familias(cs):
    return [{v.rstrip("0123456789") for v in c.nucleo} for c in cs]


def test_tres_estilos_tres_clusters_e_deterministico(con):
    a = cl.calcular(con, p=P)
    b = cl.calcular(con, p=P)
    assert len(a) == 3
    assert sorted(map(sorted, familias(a))) == [
        ["ambient"],
        ["rap"],
        ["rock"],
    ]  # cada núcleo é de um estilo só
    assert [(c.nucleo, round(c.limiar, 6), sorted(c.membros)) for c in a] == [
        (c.nucleo, round(c.limiar, 6), sorted(c.membros)) for c in b
    ]


def test_nucleo_dentro_dos_membros_e_estilo_inteiro_coberto(con):
    for c in cl.calcular(con, p=P):
        assert set(c.nucleo) <= set(c.membros)
        estilo = familias([c])[0].pop()
        assert sum(v.startswith(estilo) for v in c.membros) >= 23  # quase as 25 faixas do estilo


def test_faixa_de_fronteira_entra_em_dois_clusters():
    """Dois clusters espalhados (±40°) a 60° um do outro; o ponto a 30° de cada um passa nos dois limiares."""
    from curator.motor import espacos

    graus = np.radians(np.arange(-40, 41, 5))
    a = [np.array([np.cos(t), 0, np.sin(t)]) for t in graus]
    b = [np.array([np.cos(np.pi / 3) * np.cos(t), np.sin(np.pi / 3) * np.cos(t), np.sin(t)]) for t in graus]
    meio = np.array([np.cos(np.pi / 6), np.sin(np.pi / 6), 0])
    ids = [f"a{i:02d}" for i in range(len(a))] + [f"b{i:02d}" for i in range(len(b))] + ["meio"]
    m = espacos.Matriz(ids, espacos.normalizar_l2(np.array(a + b + [meio], np.float32)))
    rotulos = np.array([0] * len(a) + [1] * len(b) + [-1])  # o ponto do meio é ruído para o HDBSCAN
    cs = cl.sobrepor(m, rotulos, np.ones(len(ids)), cl.Parametros())
    assert all("meio" in c.membros for c in cs)
    assert all(c.membros["meio"] >= c.limiar for c in cs)


def test_salvar_carregar_e_ancoras(con):
    cs = cl.calcular(con, p=P)
    aid = cl.salvar(con, cs, "biblioteca", P)
    lidos = cl.carregar(con, aid)
    assert [c.nome for c in lidos] == [c.nome for c in cs] and [len(c.membros) for c in lidos] == [
        len(c.membros) for c in cs
    ]
    antes = {c.cluster_id: dict(c.membros) for c in lidos}

    # 10 faixas novas de rock analisadas depois: entram pelo centroide, os membros antigos não mudam
    rock = next(c for c in lidos if familias([c])[0] == {"rock"})
    for i in range(25, 35):
        vid = f"rock{i:02d}"
        con.execute(
            "INSERT INTO faixas (video_id, titulo, artistas, curtida, criada_em, atualizada_em) VALUES (?, ?, '[]', 1, 'x', 'x')",
            (vid, vid),
        )
        ruido = np.random.default_rng(i).standard_normal(rock.centroide.size) * 0.05
        emb = (rock.centroide + ruido).astype(np.float32)
        gravar_analise(
            con,
            vid,
            Resultado({}, {"effnet_emb": emb}, {}, 200.0, {}),
            Origem("youtube_music", vid),
            "1",
            "h",
            1.0,
        )
    assert cl.atualizar(con, aid) >= 10
    depois = {c.cluster_id: dict(c.membros) for c in cl.carregar(con, aid)}
    for cid, membros in antes.items():
        assert {v: s for v, s in depois[cid].items() if v in membros} == membros  # antigos intactos
    assert [c.nome for c in cl.carregar(con, aid)] == [c.nome for c in lidos]  # nomes intactos
    assert cl.atualizar(con, aid) == 0  # idempotente


def test_nome_em_cache_pelo_nucleo(con):
    cs = cl.calcular(con, p=P)
    aid = cl.salvar(con, cs, "biblioteca", P)
    cl.renomear(con, aid, cs[0].cluster_id, "Meu nome")
    assert cl.calcular(con, p=P)[0].nome == "Meu nome"  # mesmo núcleo → mesmo nome


def test_descricao_e_nome_automatico(con):
    cs = cl.calcular(con, p=P)
    amb = next(c for c in cs if familias([c])[0] == {"ambient"})
    assert amb.estatisticas["estilos"][0][0] == "Electronic---Ambient"
    assert amb.estatisticas["artistas"][0][0] == "Ambientista"
    assert amb.nome.startswith("Ambient") and "calmo" in amb.nome


def test_poucas_faixas(tmp_path):
    con = biblioteca_analisada(tmp_path / "p.db", por_estilo=3)
    with pytest.raises(cl.PoucasFaixas):
        cl.calcular(con, p=P)
