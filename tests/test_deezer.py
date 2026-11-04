"""#65: Deezer como fonte extra, casando cada faixa com o YT Music (sem rede)."""

import pytest

from curator.coleta import deezer
from curator.coleta.cliente import ClienteYT
from curator.config import caminhos, carregar_config
from curator.motor import recomendacoes as rc
from tests.falso_motor import biblioteca_analisada
from tests.falso_yt import FalsoYT


class FalsoDeezer:
    def __init__(self):
        self.chamadas = []
        self.respostas = {
            ("/search/artist", "Banda Rock"): {"data": [{"id": 9, "name": "Banda Rockk"}, {"id": 1, "name": "BANDA ROCK"}]},
            ("/artist/1/related", None): {"data": [{"id": 2, "name": "Vizinha"}, {"id": 3, "name": "Prima"}]},
            ("/artist/2/top", None): {"data": [
                {"id": 21, "title": "Canção A", "duration": 200, "artist": {"name": "Vizinha"}},
                {"id": 22, "title": "Canção B", "duration": 180, "artist": {"name": "Vizinha"}},
            ]},
            ("/artist/3/top", None): {"data": [{"id": 31, "title": "Canção C", "duration": 240, "artist": {"name": "Prima"}}]},
        }  # fmt: skip

    def get(self, caminho, **params):
        self.chamadas.append(caminho)
        return self.respostas.get((caminho, params.get("q")), {"data": []})


def resultado(vid, titulo, artista, dur):
    return {"videoId": vid, "title": titulo, "artists": [{"name": artista, "id": f"UC_{artista}"}],
            "duration_seconds": dur, "album": None}  # fmt: skip


@pytest.fixture
def con():
    return biblioteca_analisada(caminhos().banco, por_estilo=2)


@pytest.fixture
def yt():
    y = FalsoYT()
    y.buscas = {
        "Vizinha Canção A": [resultado("ytA", "Canção A", "Vizinha", 201)],
        # mesma música, mas cover e duração errada: nenhum casamento confiável
        "Vizinha Canção B": [resultado("ytB1", "Canção B (Cover)", "Vizinha", 180), resultado("ytB2", "Canção B", "Vizinha", 300)],
        "Prima Canção C": [resultado("ytC", "Canção C", "Prima", 240)],
    }  # fmt: skip
    return y


def coletar(con, yt, dz):
    cli = ClienteYT(yt, intervalo_s=0, dormir=lambda s: None)
    return deezer.coletar(con, cli, dz, ["rock00"], carregar_config())


def test_casa_por_titulo_artista_e_duracao_e_descarta_sem_casamento(con, yt):
    [cid] = coletar(con, yt, FalsoDeezer())
    c = rc.ler_coletas(con, [cid])[0]
    assert c["fonte"] == "deezer" and c["itens"] == ["ytA", "ytC"]
    nulos = con.execute("SELECT deezer_id FROM deezer_casamentos WHERE video_id IS NULL").fetchall()
    assert [r[0] for r in nulos] == [22]


def test_casamentos_ficam_em_cache(con, yt):
    coletar(con, yt, FalsoDeezer())
    buscas = yt.chamadas["search"]
    con.execute("UPDATE coletas SET coletada_em = '2000-01-01T00:00:00Z'")  # coleta vencida, casamentos não
    coletar(con, yt, FalsoDeezer())
    assert yt.chamadas["search"] == buscas


def test_artista_com_nome_diferente_nao_e_usado(con, yt):
    dz = FalsoDeezer()
    dz.respostas[("/search/artist", "Banda Rock")] = {"data": [{"id": 9, "name": "Banda Rockk"}]}
    [cid] = coletar(con, yt, dz)
    assert rc.ler_coletas(con, [cid])[0]["itens"] == []
    assert "/artist/9/related" not in dz.chamadas


def test_deezer_fora_do_ar_nao_derruba(con, yt):
    class Quebrado:
        def get(self, *a, **k):
            raise deezer.ErroDeezer("quota")

    assert coletar(con, yt, Quebrado()) == []


def test_cliente_deezer_trata_erro_da_api():
    import io
    import json

    def abrir(req, timeout):
        return io.BytesIO(json.dumps({"error": {"type": "Exception", "code": 4}}).encode())

    with pytest.raises(deezer.ErroDeezer):
        deezer.ClienteDeezer(intervalo_s=0, abrir=abrir).get("/artist/1")
