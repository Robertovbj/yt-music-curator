"""Credenciais do YouTube Music (headers do navegador) e cliente ytmusicapi com ritmo e novas tentativas."""

import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from ytmusicapi import YTMusic, setup

log = logging.getLogger(__name__)


class CredencialAusente(RuntimeError):
    pass


# Headers de transporte que o navegador manda e quebram a requisição do ytmusicapi
# (com "content-encoding: gzip" o servidor tenta descomprimir um corpo que vai em texto puro).
TRANSPORTE = {
    "content-encoding",
    "content-length",
    "te",
    "priority",
    "alt-used",
    "connection",
    "pragma",
    "cache-control",
}


def _limpar(headers: dict) -> dict:
    """Tira headers de transporte e a linha de requisição ("POST /... HTTP/2") colada junto."""
    return {
        k: v
        for k, v in headers.items()
        if k.lower() not in TRANSPORTE and not re.match(r"^(GET|POST|PUT|HEAD|OPTIONS) /", k, re.IGNORECASE)
    }


def configurar(headers_raw: str, destino: Path) -> None:
    """Valida os headers e grava o `browser.json` com permissão 600."""
    destino.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = destino.with_suffix(".tmp")
    setup(filepath=str(tmp), headers_raw=headers_raw)
    os.chmod(tmp, 0o600)
    tmp.write_text(json.dumps(_limpar(json.loads(tmp.read_text())), ensure_ascii=True, indent=2))
    tmp.replace(destino)


def ler_area_de_transferencia() -> str:
    for cmd in (["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"]):
        if shutil.which(cmd[0]):
            return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    raise RuntimeError("nem wl-paste nem xclip encontrados; use --headers ARQUIVO")


class ClienteYT:
    """Embrulha o YTMusic: pausa entre chamadas e repete falhas transitórias com backoff."""

    def __init__(self, yt: YTMusic, intervalo_s: float = 0.7, tentativas: int = 3, dormir=time.sleep):
        self.yt = yt
        self.intervalo_s = intervalo_s
        self.tentativas = tentativas
        self._dormir = dormir
        self._ultima = 0.0

    def chamar(self, metodo: str, *args, **kwargs):
        for tentativa in range(1, self.tentativas + 1):
            espera = self.intervalo_s - (time.monotonic() - self._ultima)
            if espera > 0:
                self._dormir(espera)
            try:
                return getattr(self.yt, metodo)(*args, **kwargs)
            except Exception as e:
                if tentativa == self.tentativas or not _transitorio(e):
                    raise
                atraso = 2**tentativa
                log.warning("%s falhou (%s); nova tentativa em %ss", metodo, e, atraso)
                self._dormir(atraso)
            finally:
                self._ultima = time.monotonic()
        raise AssertionError("inalcançável")


def _transitorio(e: Exception) -> bool:
    texto = str(e).lower()
    return any(s in texto for s in ("timed out", "timeout", "connection", "429", "500", "502", "503", "504"))


def abrir_cliente(credenciais: Path, intervalo_s: float = 0.7) -> ClienteYT:
    if not credenciais.exists():
        raise CredencialAusente(f"credenciais não encontradas em {credenciais}; rode `curator auth`")
    return ClienteYT(YTMusic(str(credenciais)), intervalo_s=intervalo_s)


def testar(cliente: ClienteYT) -> dict:
    """Nome da conta e número de curtidas; falha se os headers expiraram."""
    conta = cliente.chamar("get_account_info")
    curtidas = cliente.chamar("get_liked_songs", limit=1)
    return {"conta": conta.get("accountName"), "curtidas": curtidas.get("trackCount")}
