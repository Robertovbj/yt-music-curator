import json
import stat

import pytest

from curator.coleta.cliente import ClienteYT, configurar

HEADERS = """accept: */*
content-type: application/json
cookie: SAPISID=abc/def; __Secure-3PAPISID=abc/def; outro=1
x-goog-authuser: 0
user-agent: Mozilla/5.0
"""


def test_configurar_grava_json_privado(tmp_path):
    destino = tmp_path / "cfg" / "browser.json"
    configurar(HEADERS, destino)
    assert stat.S_IMODE(destino.stat().st_mode) == 0o600
    dados = json.loads(destino.read_text())
    assert dados["x-goog-authuser"] == "0"
    assert "SAPISID" in dados["cookie"]


def test_configurar_descarta_linha_de_requisicao_e_transporte(tmp_path):
    """Cópia "bruta" do Firefox: vem com a linha POST e com Content-Encoding: gzip."""
    bruto = "POST /youtubei/v1/browse?prettyPrint=false HTTP/2\nHost: music.youtube.com\n"
    bruto += "Content-Encoding: gzip\nContent-Length: 1774\nTE: trailers\nPriority: u=4\n" + HEADERS
    destino = tmp_path / "browser.json"
    configurar(bruto, destino)
    chaves = set(json.loads(destino.read_text()))
    assert not chaves & {"content-encoding", "content-length", "te", "priority", "host"}
    assert not any(k.startswith("POST") for k in chaves)
    assert {"cookie", "x-goog-authuser"} <= chaves


def test_configurar_rejeita_headers_sem_cookie(tmp_path):
    with pytest.raises(Exception, match="cookie"):
        configurar("accept: */*\n", tmp_path / "browser.json")
    assert not (tmp_path / "browser.json").exists()


class Falso:
    def __init__(self, falhas):
        self.falhas = list(falhas)
        self.chamadas = 0

    def get_account_info(self):
        self.chamadas += 1
        if self.falhas:
            raise self.falhas.pop(0)
        return {"accountName": "Fulano"}


def test_cliente_repete_falha_transitoria():
    yt = Falso([Exception("HTTP 503"), Exception("Connection reset")])
    esperas = []
    c = ClienteYT(yt, intervalo_s=0, tentativas=3, dormir=esperas.append)
    assert c.chamar("get_account_info") == {"accountName": "Fulano"}
    assert yt.chamadas == 3
    assert esperas == [2, 4]


def test_cliente_nao_repete_erro_definitivo():
    yt = Falso([Exception("playlist não existe")])
    c = ClienteYT(yt, intervalo_s=0, dormir=lambda s: None)
    with pytest.raises(Exception, match="não existe"):
        c.chamar("get_account_info")
    assert yt.chamadas == 1
