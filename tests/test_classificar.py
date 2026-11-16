import numpy as np
import pytest

from curator.motor import classificar as cl
from tests.falso_motor import biblioteca_analisada, playlist


@pytest.fixture
def con(tmp_path):
    con = biblioteca_analisada(tmp_path / "c.db", por_estilo=14)
    # "Pesado" é agregadora de "Rock" e "Rap"; "Calma" fica na raiz. As faixas 10–13 de cada estilo ficam soltas.
    playlist(
        con,
        "PLpesado",
        "Pesado",
        [f"rock{i:02d}" for i in range(10)] + [f"rap{i:02d}" for i in range(10)],
        tipo="agregadora",
    )
    playlist(con, "PLrock", "Rock", [f"rock{i:02d}" for i in range(10)], mae="PLpesado")
    playlist(con, "PLrap", "Rap", [f"rap{i:02d}" for i in range(10)], mae="PLpesado")
    playlist(con, "PLcalma", "Calma", [f"ambient{i:02d}" for i in range(10)])
    playlist(con, "PLpouca", "Quase vazia", ["rock00", "rap00"])
    return con


ALVOS = ["PLpesado", "PLcalma", "PLpouca"]  # as raízes, nomeadas


def destinos(c, vid):
    return [d.playlist_id for d in next(x for x in c.decisoes if x.video_id == vid).destinos]


def test_nao_alocadas_vao_para_a_familia_certa(con):
    c = cl.classificar(con, em=ALVOS)
    assert len(c.decisoes) == 12  # 4 soltas × 3 estilos
    familia = {"rock": {"PLrock", "PLpesado"}, "rap": {"PLrap", "PLpesado"}, "ambient": {"PLcalma"}}
    folha = {"rock": "PLrock", "rap": "PLrap", "ambient": "PLcalma"}
    na_folha = 0
    for d in c.decisoes:
        estilo = d.video_id.rstrip("0123456789")
        ids = {x.playlist_id for x in d.destinos}
        assert ids <= familia[estilo]  # nunca no estilo errado
        if folha[estilo] in ids:
            na_folha += 1
        elif not ids:  # abaixo de todos os limiares: a própria folha é a sugestão mais próxima
            assert d.proximas[0].playlist_id == folha[estilo]
    # o limiar no percentil 10 deixa alguns legítimos de fora por pouco (com 10 membros, margem ~0,01)
    assert na_folha >= len(c.decisoes) // 2


def test_quem_nao_passa_na_filha_fica_na_mae(con):
    for d in cl.classificar(con, em=ALVOS).decisoes:
        if d.video_id.startswith("rap") and "PLrap" not in {x.playlist_id for x in d.destinos}:
            assert [x.playlist_id for x in d.destinos] in ([], ["PLpesado"])


def test_playlist_com_poucos_membros_fica_de_fora(con):
    _, frias = cl.perfis(con)
    assert ("Quase vazia", 2) in frias


def test_deterministico(con):
    a, b = cl.classificar(con, em=ALVOS), cl.classificar(con, em=ALVOS)
    assert [(d.video_id, [(x.playlist_id, x.score) for x in d.destinos]) for d in a.decisoes] == [
        (d.video_id, [(x.playlist_id, x.score) for x in d.destinos]) for d in b.decisoes
    ]


def test_crossover_entra_em_duas_filhas(con):
    """Duas filhas de estilo parecido: a faixa que cabe nas duas entra nas duas (multi-rótulo)."""
    playlist(con, "PLguitarra", "Guitarra", [f"rock{i:02d}" for i in range(8)], mae="PLpesado")
    c = cl.classificar(con, ids=[f"rock{i:02d}" for i in range(10, 14)], percentil=1, em=ALVOS)
    assert any({"PLrock", "PLguitarra"} <= set(destinos(c, v)) for v in [d.video_id for d in c.decisoes])


def test_sem_destino_sugere_as_mais_proximas(con):
    # restringe a busca às filhas de "Pesado": uma faixa de ambient não cabe em nenhuma
    c = cl.classificar(con, ids=["ambient12"], mae="PLpesado")
    d = c.decisoes[0]
    assert d.destinos == [] and {p.playlist_id for p in d.proximas} == {"PLrock", "PLrap"}


def test_limiar_loo_e_um_percentil_baixo_dos_membros(con):
    ps, _ = cl.perfis(con)
    p = ps["PLrock"]
    assert -1 <= p.limiar <= 1
    # quase todos os membros (≥ 90% pela construção) passam no próprio limiar
    from curator.motor.classificar import _centroide, _score

    passam = sum(
        _score(p.E[i], np.delete(p.E, i, 0), _centroide(np.delete(p.E, i, 0)), cl.K, cl.ALFA) >= p.limiar
        for i in range(len(p.E))
    )
    assert passam >= 0.9 * len(p.E)


def test_avaliacao_leave_one_out(con):
    m = {x.titulo: x for x in cl.avaliar(con)}
    assert m["Rock"].precisao == 1.0 and m["Rock"].revocacao >= 0.8
    assert m["Calma"].precisao == 1.0


def test_sem_alvo_nao_sugere_nada(con):
    with pytest.raises(cl.SemAlvo, match="diga em quais playlists"):
        cl.classificar(con)
    with pytest.raises(cl.SemAlvo, match="nenhuma playlist geral"):
        cl.classificar(con, nas_gerais=True)


def test_so_nas_playlists_nomeadas(con):
    c = cl.classificar(con, em=["PLcalma"])
    assert {x.playlist_id for d in c.decisoes for x in d.destinos + d.proximas} <= {"PLcalma"}
    assert c.frias == []  # "Quase vazia" não foi pedida: nem aparece no aviso


def test_nas_gerais_usa_as_curtidas_fora_das_gerais(con):
    # uma geral com rock00–09; rock10–13 são curtidas fora dela, mesmo as que estão em outra playlist
    playlist(con, "PLgeral", "Geral rock", [f"rock{i:02d}" for i in range(10)])
    con.execute("UPDATE playlists SET geral = 1 WHERE playlist_id = 'PLgeral'")
    playlist(con, "PLminha", "Minha", ["rock12"])
    c = cl.classificar(con, nas_gerais=True)
    ids = {d.video_id for d in c.decisoes}
    assert {"rock10", "rock12"} <= ids and not ids & {f"rock{i:02d}" for i in range(10)}
    assert {x.playlist_id for d in c.decisoes for x in d.destinos} <= {"PLgeral"}
    assert "PLgeral" in destinos(c, "rock11")


# ------------------------------------------------------------------ regra das masters (#105)


def test_no_maximo_duas_entre_as_masters():
    def d(pid, s):
        return cl.Destino(pid, pid, s, 0.5)

    ds = [d("A", 0.90), d("B", 0.89), d("C", 0.885), d("X", 0.6)]
    assert [x.playlist_id for x in cl.no_maximo_duas(ds, {"A", "B", "C"}, 0.02)] == ["A", "B", "X"]
    assert [x.playlist_id for x in cl.no_maximo_duas(ds, {"A", "B", "C"}, 0.0)] == ["A", "X"]  # só a melhor
    assert [x.playlist_id for x in cl.no_maximo_duas(ds, {"C"}, 0.02)] == ["A", "B", "C", "X"]  # uma só: fica
    assert cl.no_maximo_duas([], {"A"}, 0.02) == []


def test_nas_masters_a_faixa_fica_em_no_maximo_duas(con):
    from curator.motor import gerais

    # três gerais de rock: pelo limiar, uma curtida de rock entra nas três
    for nome, faixa in (("G1", range(0, 10)), ("G2", range(0, 7)), ("G3", range(3, 10))):
        playlist(con, f"PL{nome}", nome, [f"rock{i:02d}" for i in faixa])
        con.execute("UPDATE playlists SET geral = 1 WHERE playlist_id = ?", (f"PL{nome}",))
    assert cl.fronteira_das_masters(con) is None  # gerais que não são masters: multi-rótulo, como antes
    antes = next(d for d in cl.classificar(con, nas_gerais=True).decisoes if d.video_id == "rock10")
    assert len(antes.destinos) == 3
    gerais.obter(con, gerais.parametros(quantas=3, fronteira=0.02))  # agora as gerais são masters
    assert cl.fronteira_das_masters(con) == 0.02
    depois = next(d for d in cl.classificar(con, nas_gerais=True).decisoes if d.video_id == "rock10")
    assert 1 <= len(depois.destinos) <= 2
    assert depois.destinos[0].score == max(x.score for x in antes.destinos)
    # fora das gerais, nada muda: playlists nomeadas continuam multi-rótulo
    assert (
        len(
            next(
                d for d in cl.classificar(con, ids=["rock10"], em=["PLG1", "PLG2", "PLG3"]).decisoes
            ).destinos
        )
        == 3
    )
