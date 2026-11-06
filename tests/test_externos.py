"""#88 item 5: metadados externos (Deezer, MusicBrainz) com cache, e erro que não vira cache."""

import io
import json
import urllib.error
from datetime import UTC, datetime, timedelta

import pytest

from curator.coleta import externos
from curator.config import caminhos, carregar_config
from tests.falso_yt import biblioteca_exemplo

T0 = datetime(2026, 11, 5, 23, 0, tzinfo=UTC)


class Falso:
    """Cliente falso: `respostas` mapeia o caminho (com prefixo) para o JSON, ou para uma exceção."""

    def __init__(self, respostas):
        self.respostas, self.pedidos = respostas, []

    def get(self, caminho, **params):
        self.pedidos.append((caminho, params))
        for prefixo, r in self.respostas.items():
            if caminho.startswith(prefixo):
                if isinstance(r, Exception):
                    raise r
                return r(params) if callable(r) else r
        return None


@pytest.fixture
def con():
    return biblioteca_exemplo(caminhos().banco)


@pytest.fixture
def cfg():
    return carregar_config()


def faixa(con, vid):
    return externos._faixa(con, vid)


def deezer_ok(con, vid, isrc="ISRC1"):
    f = faixa(con, vid)
    return Falso({
        "/search/track": {"data": [
            {"id": 9, "title": f"{f.titulo} (Remix)", "artist": {"name": f.artistas[0]}, "duration": f.duracao_s},
            {"id": 7, "title": f.titulo, "artist": {"name": f.artistas[0]}, "duration": f.duracao_s},
        ]},
        "/track/7": {"id": 7, "title": f.titulo, "isrc": isrc, "bpm": 92.5, "gain": -8.1, "rank": 900000,
                     "release_date": "2002-05-26", "explicit_lyrics": True, "duration": f.duracao_s,
                     "album": {"id": 55}, "contributors": [{"id": 1, "name": f.artistas[0], "role": "Main"}]},
        "/album/55": {"title": "Álbum", "genres": {"data": [{"name": "Rap/Hip Hop"}]}, "label": "X", "record_type": "album"},
    })  # fmt: skip


def mb_ok(con, vid):
    f = faixa(con, vid)
    rec = {"id": "rec-1", "title": f.titulo, "length": (f.duracao_s or 0) * 1000,
           "artist-credit": [{"artist": {"id": "art-1", "name": f.artistas[0]}}]}  # fmt: skip
    return Falso({
        "/isrc/": {"recordings": [rec]},
        "/recording/rec-1": {**rec, "genres": [{"name": "hip hop", "count": 10}, {"name": "pop rap", "count": 4}],
                             "tags": [{"name": "detroit", "count": 3}], "first-release-date": "2002-05-20",
                             "releases": [
                                 {"status": "Official", "date": "2002-05-01",
                                  "release-group": {"id": "rg-s", "primary-type": "Single"}},
                                 {"status": "Official", "date": "2002-05-26",
                                  "release-group": {"id": "rg-a", "primary-type": "Album"}}]},
        "/release-group/rg-a": {"title": "Álbum", "primary-type": "Album", "genres": [{"name": "Hip Hop", "count": 3}]},
        "/artist/art-1": {"name": f.artistas[0], "type": "Person", "country": "US",
                          "genres": [{"name": "hip hop", "count": 39}, {"name": "pop rap", "count": 7}]},
    })  # fmt: skip


def test_deezer_casa_a_faixa_certa_e_guarda_isrc_bpm_e_genero_do_album(con, cfg):
    dz = deezer_ok(con, "a")
    b = externos.deezer(con, dz, faixa(con, "a"), 3, 0.82, 90)
    assert (b.status, b.ref) == ("ok", "7")  # o remix foi recusado pelo casamento
    assert (b.dados["isrc"], b.dados["bpm"], b.dados["rank"], b.dados["album"]["generos"]) == (
        "ISRC1", 92.5, 900000, ["Rap/Hip Hop"],
    )  # fmt: skip
    assert b.generos == [("album", "Rap/Hip Hop", 1.0)]
    externos.deezer(con, dz, faixa(con, "a"), 3, 0.82, 90)
    assert [p for p, _ in dz.pedidos].count("/album/55") == 1  # o álbum vem do cache na 2ª vez


def test_deezer_sem_candidato_bom_e_sem_casamento(con):
    dz = Falso(
        {
            "/search/track": {
                "data": [{"id": 1, "title": "Outra Coisa", "artist": {"name": "Z"}, "duration": 10}]
            }
        }
    )
    assert externos.deezer(con, dz, faixa(con, "a"), 3, 0.82, 90).status == "sem-casamento"


def test_musicbrainz_pelo_isrc_traz_generos_da_faixa_album_e_artista(con):
    mb = mb_ok(con, "a")
    b = externos.musicbrainz(con, mb, faixa(con, "a"), "ISRC1", 0.82, 90)
    assert (b.status, b.ref, b.score) == ("ok", "rec-1", 1.0)
    assert b.dados["release_group"]["id"] == "rg-a"  # o álbum, não o single
    assert b.dados["artista"]["pais"] == "US"
    assert ("artista", "hip hop", 39) in b.generos and ("faixa", "pop rap", 4) in b.generos
    assert mb.pedidos[0][0] == "/isrc/ISRC1"


def test_musicbrainz_sem_isrc_busca_e_confere_o_casamento(con):
    f = faixa(con, "a")
    mb = mb_ok(con, "a")
    mb.respostas["/recording"] = lambda p: (
        {"recordings": [{"id": "rec-1", "title": f.titulo, "length": f.duracao_s * 1000,
                         "artist-credit": [{"artist": {"id": "art-1", "name": f.artistas[0]}}]}]}
        if "query" in p else mb_ok(con, "a").respostas["/recording/rec-1"]
    )  # fmt: skip
    b = externos.musicbrainz(con, mb, f, None, 0.82, 90)
    assert b.status == "ok" and 'recording:"' in mb.pedidos[0][1]["query"]


def test_coletar_grava_cache_e_o_musicbrainz_usa_o_isrc_do_deezer(con, cfg):
    cli = {"deezer": deezer_ok(con, "a"), "musicbrainz": mb_ok(con, "a")}
    c = externos.coletar(con, cfg, cli, ("deezer", "musicbrainz"), ids=["a"], relogio=lambda: T0)
    assert c == {"deezer: ok": 1, "musicbrainz: ok": 1}
    assert cli["musicbrainz"].pedidos[0][0] == "/isrc/ISRC1"
    g = con.execute(
        "SELECT fonte, nivel, rotulo, peso FROM externos_generos WHERE video_id = 'a' ORDER BY fonte, nivel, rotulo"
    ).fetchall()
    assert ("deezer", "album", "rap/hip hop", 1.0) in [tuple(r) for r in g]
    assert ("musicbrainz", "artista", "hip hop", 39.0) in [tuple(r) for r in g]
    # no cache: não volta antes do ttl
    assert "a" not in externos.pendentes(con, "deezer", 90, datetime.now(UTC))
    assert "a" in externos.pendentes(con, "deezer", 90, datetime.now(UTC) + timedelta(days=91))
    assert externos.resumo(con)["deezer"] == {"ok": 1}


def test_erro_de_rede_nao_vira_cache_e_espera_cada_vez_mais(con, cfg):
    cli = {"deezer": Falso({"/search": externos.ErroExterno("HTTP 503")})}
    c = externos.coletar(con, cfg, cli, ("deezer",), ids=["a"], relogio=lambda: T0)
    assert c == {"deezer: erro": 1}
    r = con.execute(
        "SELECT status, tentativas, proxima_em FROM externos_faixas WHERE video_id = 'a'"
    ).fetchone()
    assert (r["status"], r["tentativas"], r["proxima_em"]) == ("erro", 1, "2026-11-06T00:00:00Z")
    assert "a" not in externos.pendentes(con, "deezer", 90, T0)
    assert "a" in externos.pendentes(con, "deezer", 90, T0 + timedelta(hours=1))
    externos.coletar(con, cfg, cli, ("deezer",), ids=["a"], relogio=lambda: T0 + timedelta(hours=1))
    r = con.execute("SELECT tentativas, proxima_em FROM externos_faixas WHERE video_id = 'a'").fetchone()
    assert tuple(r) == (2, "2026-11-06T02:00:00Z")
    # depois um sucesso zera as tentativas
    externos.coletar(
        con,
        cfg,
        {"deezer": deezer_ok(con, "a")},
        ("deezer",),
        ids=["a"],
        relogio=lambda: T0 + timedelta(hours=4),
    )
    assert con.execute("SELECT status, tentativas FROM externos_faixas WHERE video_id = 'a'").fetchone()[
        :
    ] == ("ok", 0)


def test_limite_por_fonte_e_so_deezer_e_musicbrainz(con, cfg):
    # o Last.fm pede chave de API e ficou de fora
    assert set(externos.clientes(cfg)) == {"deezer", "musicbrainz"}
    cli = {"deezer": Falso({"/search": {"data": []}})}
    assert externos.coletar(con, cfg, cli, limite=2, relogio=lambda: T0) == {"deezer: sem-casamento": 2}


class Resposta(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_cliente_http_tenta_de_novo_no_limite_de_taxa_e_404_e_none():
    chamadas, esperas = [], []

    def abrir(req, timeout):
        chamadas.append(req.full_url)
        if len(chamadas) == 1:
            raise urllib.error.HTTPError(req.full_url, 503, "rate", {}, None)
        if "nada" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 404, "x", {}, None)
        return Resposta(json.dumps({"ok": 1}).encode())

    c = externos.ClienteHTTP("https://mb", 0, "ua ( contato )", abrir, esperas.append)
    assert c.get("/x", fmt="json") == {"ok": 1} and len(chamadas) == 2 and esperas == [2]
    assert c.get("/nada") is None
    assert externos.user_agent({"externos": {"contato": "https://exemplo"}}).endswith("( https://exemplo )")


def test_casamento_dos_metadados_aceita_outra_versao_mas_nao_variante():
    from curator.audio.provedores.base import FaixaAlvo

    f = FaixaAlvo("v", "秒針少女 - Chronostasis", ["kessoku band"], 272)
    assert externos.titulos(f.titulo) == ["秒針少女 - Chronostasis", "秒針少女", "Chronostasis"]
    assert externos.casar(f, "秒針少女", "kessoku band", 272) == 1.0
    knife = FaixaAlvo("k", "The Knife In My Back", ["Alec Benjamin"], 188)
    assert 0.82 < externos.casar(knife, "The Knife In My Back", "Alec Benjamin", 169) < 0.9  # outra versão
    assert externos.casar(knife, "The Knife In My Back (Live)", "Alec Benjamin", 188) == 0.0
    assert externos.casar(FaixaAlvo("d", "THE DRAIN", ["Bad Omens"], 226), "THE DRAIN (Original Soundtrack)",
                          "Bad Omens", 225) == 1.0  # fmt: skip


def test_repetir_sem_casamento_antes_do_ttl(con, cfg):
    cli = {"deezer": Falso({"/search": {"data": []}})}
    externos.coletar(con, cfg, cli, ("deezer",), ids=["a"], relogio=lambda: T0)
    assert "a" not in externos.pendentes(con, "deezer", 90, T0)
    assert "a" in externos.pendentes(con, "deezer", 90, T0, repetir_sem_casamento=True)
