"""#83: afinidade de gênero-pai e régua relativa, iguais para todos os gêneros."""

import json

import numpy as np
import pytest

from curator.audio.analisador import Resultado
from curator.audio.persistir import Origem, gravar_analise
from curator.banco.conexao import abrir, agora
from curator.motor import generos
from curator.motor.executor import VERSAO_MOTOR, executar
from curator.motor.receita import Receita

# "Poucos" tem 1 estilo; "Muitos" tem 8: o máximo e o noisy-OR dependem disso, a fração não
DISCOGS = ["Poucos---Unico"] + [f"Muitos---E{i}" for i in range(8)] + ["Outro---X"]
JAMENDO = ["rap", "rock", "80s"]


def faixa(con, vid, discogs, jamendo=None, curtida=True):
    con.execute(
        "INSERT INTO faixas (video_id, titulo, artistas, duracao_s, curtida, criada_em, atualizada_em) "
        "VALUES (?, ?, ?, 200, ?, ?, ?)",
        (vid, vid, json.dumps([{"id": f"UC_{vid}", "nome": vid}]), int(curtida), agora(), agora()),
    )
    vet, rot = {"discogs400": np.array(discogs, np.float32)}, {"discogs400": DISCOGS}
    if jamendo is not None:
        vet["jamendo_genre"], rot["jamendo_genre"] = np.array(jamendo, np.float32), JAMENDO
    gravar_analise(
        con, vid, Resultado({"energy": 0.5}, vet, rot, 200.0, {}), Origem("youtube_music", vid), "1", "h", 1
    )


def so(estilos: dict[int, float]) -> list[float]:
    v = [0.0] * len(DISCOGS)
    for i, p in estilos.items():
        v[i] = p
    return v


@pytest.fixture
def con(tmp_path):
    return abrir(tmp_path / "c.db")


def test_fracao_nao_depende_do_numero_de_estilos_do_pai(con):
    faixa(con, "um", so({0: 0.8}))  # toda a massa no único estilo de "Poucos"
    faixa(con, "espalhado", so({i: 0.1 for i in range(1, 9)}))  # 0,1 em cada um dos 8 de "Muitos"
    a = generos.Afinidades(con)
    m = a.afinidade(["um", "espalhado"])
    assert m[0, a.pais.index("Poucos")] == pytest.approx(1.0) and m[
        1, a.pais.index("Muitos")
    ] == pytest.approx(1.0)
    # o critério antigo (maior estilo) via 0,8 contra 0,1: oito vezes menos para o mesmo "quanto é do gênero"


def test_jamendo_entra_na_media_pelos_rotulos_equivalentes_sem_as_decadas(con, monkeypatch):
    monkeypatch.setitem(generos.MAPA_JAMENDO, "Poucos", ["rap"])
    faixa(con, "a", so({0: 0.6, 9: 0.2}), jamendo=[0.3, 0.1, 0.9])  # "80s" fica fora do total do Jamendo
    faixa(con, "b", so({0: 0.6, 9: 0.2}))  # sem Jamendo: só o Discogs
    a = generos.Afinidades(con)
    k = a.pais.index("Poucos")
    m = a.afinidade(["a", "b"])
    assert m[0, k] == pytest.approx((0.75 + 0.3 / 0.4) / 2) and m[1, k] == pytest.approx(0.75)
    assert m[0, a.pais.index("Outro")] == pytest.approx(0.25)  # pai sem equivalente no Jamendo: só o Discogs


def test_regua_relativa_ao_nucleo_e_os_recuos(con):
    for i in range(12):  # núcleo de "Poucos": 1º lugar no Discogs
        faixa(con, f"p{i:02d}", so({0: 0.5 + i * 0.02, 9: 0.2}))
    for i in range(3):  # "Muitos" quase não aparece: sem núcleo, a régua é a global
        faixa(con, f"m{i}", so({1: 0.3, 9: 0.3}))
    a = generos.Afinidades(con)
    rp = a.reguas["Poucos"]
    assert rp.tipo == "nucleo" and rp.nucleo == 12
    rel = a.relativa("Poucos", [f"p{i:02d}" for i in range(12)])
    assert np.median(rel) == pytest.approx(1.0)  # 1,0 = a faixa mediana do núcleo
    assert a.reguas["Muitos"].tipo == "global" and a.reguas["Muitos"].referencia == pytest.approx(
        rp.referencia
    )
    assert a.relativa("Não existe", ["p00"]).tolist() == [0.0]


def test_nucleo_amplo_quando_as_cabecas_discordam(con, monkeypatch):
    monkeypatch.setitem(generos.MAPA_JAMENDO, "Poucos", ["rap"])
    monkeypatch.setitem(generos.MAPA_JAMENDO, "Outro", ["rock"])
    for i in range(12):  # Discogs diz "Poucos", Jamendo diz "Outro": ninguém no núcleo estrito
        faixa(con, f"d{i:02d}", so({0: 0.6, 9: 0.3}), jamendo=[0.1, 0.8, 0.0])
    a = generos.Afinidades(con)
    assert a.reguas["Poucos"].tipo == "nucleo_amplo" and a.reguas["Poucos"].nucleo == 12


def test_executor_usa_a_regua_relativa_e_registra_a_versao(con):
    for i in range(12):
        faixa(con, f"rap{i:02d}", so({0: 0.6, 9: 0.1}))
    # massa de "Poucos" alta, mas espalhada com outro pai: abaixo da régua
    faixa(con, "misto", so({0: 0.3, 9: 0.3}))
    r = Receita(
        nome="x",
        tamanho=50,
        filtros={"genero_pai_min": {"Poucos": 0.8}},
        diversidade={"max_por_artista": None},
    )
    e = executar(con, r)
    assert "misto" not in e.ids and len(e.ids) == 12 and e.versao_motor == VERSAO_MOTOR == "4"
    assert e.descartes["gênero-pai abaixo do mínimo"] == 1


def test_receita_aceita_limite_relativo_acima_de_1():
    Receita(nome="x", filtros={"genero_pai_min": {"Rock": 1.2}})
    with pytest.raises(ValueError):
        Receita(nome="x", filtros={"genero_pai_min": {"Rock": 3.5}})


def test_execucao_salva_guarda_a_versao_do_motor(con):
    from curator.motor import receitas

    for i in range(12):
        faixa(con, f"rap{i:02d}", so({0: 0.6, 9: 0.1}))
    receitas.salvar(con, Receita(nome="Rap", filtros={"genero_pai_min": {"Poucos": 0.8}}), "rap")
    receitas.executar_e_registrar(con, "Rap")
    assert [h["versao_motor"] for h in receitas.execucoes(con, "Rap")] == ["4"]


MAEST = ["Poucos---Unico", "Muitos---E0", "Outro---X"]


def com_maest(con, vid, vetor):
    from curator.audio import extras

    extras.gravar(con, vid, {"maest": extras.ResultadoExtra(
        "h", {"maest519": np.array(vetor, np.float32), "maest519_max": np.zeros(3, np.float32)},
        {"maest519": MAEST, "maest519_max": MAEST}, 1.0)}, vid)  # fmt: skip


def test_regua_com_maest_fica_ao_lado_e_relativa_ao_mesmo_nucleo(con):
    """#88: média da régua do Discogs+Jamendo com a do MAEST; faixa sem MAEST fica só com a primeira. A régua das
    receitas (`relativa`) não muda."""
    for i in range(12):
        faixa(con, f"p{i:02d}", so({0: 0.6, 9: 0.2}))
    a = generos.Afinidades(con)
    assert a.reguas["Poucos"].referencia_maest is None  # sem MAEST ainda: nada muda
    antes = a.relativa("Poucos", ["p00"])[0]
    for i in range(12):
        com_maest(con, f"p{i:02d}", [0.8, 0.0, 0.2])
    faixa(con, "fora", so({0: 0.6, 9: 0.2}))  # o Discogs acha que é do gênero
    com_maest(con, "fora", [0.1, 0.0, 0.9])  # o MAEST discorda
    faixa(con, "sem", so({0: 0.6, 9: 0.2}))  # sem MAEST
    a = generos.Afinidades(con)
    reg = a.reguas["Poucos"]
    assert reg.referencia_maest == pytest.approx(0.8) and reg.nucleo == 14
    assert a.relativa("Poucos", ["p00", "fora", "sem"]).tolist() == pytest.approx([antes] * 3)
    rel = a.relativa_com_maest("Poucos", ["p00", "fora", "sem"])
    assert rel[0] == pytest.approx(antes) and rel[2] == pytest.approx(antes)
    assert rel[1] == pytest.approx((antes + 0.1 / 0.8) / 2)
    assert a.relativa_com_maest("Não existe", ["p00"]).tolist() == [0.0]


def test_maest_nao_muda_a_receita(con):
    for i in range(12):
        faixa(con, f"p{i:02d}", so({0: 0.6, 9: 0.2}))
    r = Receita.model_validate({"nome": "x", "filtros": {"genero_pai_min": {"Poucos": 0.5}}})
    antes = executar(con, r)
    for i in range(12):
        com_maest(con, f"p{i:02d}", [0.1 if i % 2 else 0.8, 0.0, 0.9])  # o MAEST discorda em metade
    depois = executar(con, r)
    assert (depois.ids, depois.versao_banco) == (antes.ids, antes.versao_banco)
