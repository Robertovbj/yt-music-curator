import io

import pytest

from curator.audio import provedores
from curator.audio.provedores.bandcamp import Bandcamp, ler_resultados
from curator.audio.provedores.base import ErroBloqueio, ErroIndisponivel, ErroTransitorio, FaixaAlvo
from curator.audio.provedores.pasta_local import PastaLocal, indexar
from curator.audio.provedores.ytdlp import YouTubeMusic, classificar
from curator.banco.conexao import abrir
from curator.config import carregar_config


@pytest.mark.parametrize(
    ("mensagem", "tipo"),
    [
        ("ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies", ErroBloqueio),
        ("ERROR: unable to download video data: HTTP Error 429: Too Many Requests", ErroBloqueio),
        (
            "ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader",
            ErroIndisponivel,
        ),
        (
            "ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate",
            ErroIndisponivel,
        ),
        ("ERROR: [youtube] abc: Private video", ErroIndisponivel),
        (
            "ERROR: [youtube] abc: The uploader has not made this video available in your country",
            ErroIndisponivel,
        ),
        ("ERROR: unable to download video data: HTTP Error 403: Forbidden", ErroTransitorio),
        ("ERROR: [Errno -3] Temporary failure in name resolution", ErroTransitorio),
    ],
)
def test_classificar_erros_do_ytdlp(mensagem, tipo):
    assert isinstance(classificar(Exception(mensagem)), tipo)


def test_youtube_music_e_o_proprio_video_id():
    [c] = YouTubeMusic("bestaudio").buscar(FaixaAlvo("abc123", "Música", ["Banda"], 200), None)
    assert c.ref == "https://music.youtube.com/watch?v=abc123" and c.exato and c.score == 1.0


def test_opcoes_do_ytdlp_sem_cookies_e_com_proxy():
    from curator.audio.provedores.base import Saida

    p = YouTubeMusic("bestaudio[abr<=70]", sleep_requests_s=1.5)
    o = p._opcoes(Saida("vps", "socks5://10.0.0.1:1080"))
    assert o["proxy"] == "socks5://10.0.0.1:1080" and o["sleep_interval_requests"] == 1.5
    assert "cookiefile" not in o and "postprocessors" not in o  # sem conversão para WAV
    assert YouTubeMusic("x")._opcoes(None)["proxy"] == ""  # direta, ignorando proxy do ambiente


PAGINA_BANDCAMP = """
<ul class="result-items">
<li class="searchresult data-search" data-search="{}">
  <div class="result-info">
    <div class="itemtype">TRACK</div>
    <div class="heading"> <a href="https://banda.bandcamp.com/track/cancao?from=search&amp;search_item_id=1">Canção &amp; Eco</a></div>
    <div class="subhead"> from Disco by A Banda </div>
  </div>
</li>
<li class="searchresult data-search">
  <div class="itemtype">ALBUM</div>
  <div class="heading"><a href="https://banda.bandcamp.com/album/disco">Disco</a></div>
</li>
</ul>"""


def test_ler_resultados_do_bandcamp():
    assert ler_resultados(PAGINA_BANDCAMP) == [
        ("https://banda.bandcamp.com/track/cancao", "Canção & Eco", "A Banda")
    ]


def test_busca_bandcamp_pontua_sem_duracao():
    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    b = Bandcamp("bestaudio", abrir_url=lambda req, timeout: R(PAGINA_BANDCAMP.encode()))
    [c] = b.buscar(FaixaAlvo("v", "Canção & Eco", ["A Banda"], 200), None)
    assert c.provedor == "bandcamp" and 0.85 <= c.score <= 0.9  # penalidade de "sem duração"


def test_pasta_local_indexa_e_casa_por_tags(tmp_path):
    musicas = tmp_path / "musicas"
    musicas.mkdir()
    (musicas / "01.flac").write_bytes(b"x")
    (musicas / "capa.jpg").write_bytes(b"x")
    tags = {"01.flac": ("Arctic Monkeys", "Do I Wanna Know?", "AM", 272.4)}
    con = abrir(tmp_path / "c.db")
    ler = lambda arq: tags[arq.name]  # noqa: E731
    assert indexar(con, musicas, ler_tags=ler, fingerprint=lambda a: None) == {
        "novos": 1,
        "inalterados": 0,
        "removidos": 0,
    }
    assert indexar(con, musicas, ler_tags=ler, fingerprint=lambda a: None)["inalterados"] == 1

    p = PastaLocal(con)
    [c] = p.buscar(FaixaAlvo("v", "Do I Wanna Know? (Official Video)", ["Arctic Monkeys"], 272), None)
    assert c.score > 0.95
    arq = p.baixar(c, tmp_path, None)
    assert arq.caminho == (musicas / "01.flac").resolve() and not arq.temporario
    assert p.buscar(FaixaAlvo("v", "Outra Música", ["Arctic Monkeys"], 272), None) == []

    (musicas / "01.flac").unlink()
    assert indexar(con, musicas, ler_tags=ler, fingerprint=lambda a: None)["removidos"] == 1


def test_montar_respeita_ordem_e_pula_pasta_sem_caminho(tmp_path):
    cfg = carregar_config()
    con = abrir(tmp_path / "c.db")
    assert list(provedores.montar(cfg, con)) == ["youtube_music", "youtube_busca", "soundcloud"]
    cfg["pasta_local"]["caminho"] = str(tmp_path)
    cfg["provedores"]["ordem"] = ["pasta_local", "youtube_music"]
    assert list(provedores.montar(cfg, con)) == ["pasta_local", "youtube_music"]
    cfg["provedores"]["ordem"] = ["spotify"]
    with pytest.raises(ValueError, match="desconhecido"):
        provedores.montar(cfg, con)


def test_bandcamp_com_desafio_js_e_indisponivel():
    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    pagina = b"<html><head><title>Client Challenge</title></head><body>JavaScript is disabled</body></html>"
    b = Bandcamp("bestaudio", abrir_url=lambda req, timeout: R(pagina))
    with pytest.raises(ErroIndisponivel, match="desafio JavaScript"):
        b.buscar(FaixaAlvo("v", "x", ["y"], 200), None)
