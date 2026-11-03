"""Modelos Essentia: manifesto com SHA-256 (`modelos.toml`), download verificado e `hash_modelos`."""

import hashlib
import logging
import tomllib
import urllib.request
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Modelo:
    nome: str
    pasta: str
    pb_sha256: str
    json_sha256: str

    def arquivos(self) -> list[tuple[str, str]]:
        return [(f"{self.nome}.pb", self.pb_sha256), (f"{self.nome}.json", self.json_sha256)]


def manifesto() -> tuple[str, list[Modelo]]:
    dados = tomllib.loads(
        resources.files("curator.audio").joinpath("modelos.toml").read_text(encoding="utf-8")
    )
    return dados["base_url"], [Modelo(**m) for m in dados["modelo"]]


def hash_modelos() -> str:
    """Identifica o conjunto de modelos; muda quando qualquer arquivo do manifesto muda."""
    _, modelos = manifesto()
    h = hashlib.sha256()
    for m in sorted(modelos, key=lambda m: m.nome):
        h.update(f"{m.nome}:{m.pb_sha256}:{m.json_sha256}\n".encode())
    return h.hexdigest()[:16]


def _sha256(arquivo: Path) -> str:
    h = hashlib.sha256()
    with arquivo.open("rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


def verificar(pasta: Path) -> list[str]:
    """Problemas encontrados (vazio = tudo certo)."""
    _, modelos = manifesto()
    problemas = []
    for m in modelos:
        for nome, esperado in m.arquivos():
            arq = pasta / nome
            if not arq.exists():
                problemas.append(f"faltando: {nome}")
            elif _sha256(arq) != esperado:
                problemas.append(f"checksum divergente: {nome}")
    return problemas


def baixar(pasta: Path, abrir_url=urllib.request.urlopen) -> int:
    """Baixa o que falta ou está corrompido (`.part` → renomeia só se o SHA-256 bater). Devolve quantos baixou."""
    base, modelos = manifesto()
    pasta.mkdir(parents=True, exist_ok=True)
    baixados = 0
    for m in modelos:
        for nome, esperado in m.arquivos():
            destino = pasta / nome
            if destino.exists() and _sha256(destino) == esperado:
                continue
            url = f"{base}/{m.pasta}/{nome}"
            log.info("baixando %s", url)
            parcial = destino.with_name(destino.name + ".part")
            with abrir_url(url, timeout=120) as r, parcial.open("wb") as f:
                for bloco in iter(lambda: r.read(1 << 20), b""):
                    f.write(bloco)
            obtido = _sha256(parcial)
            if obtido != esperado:
                parcial.unlink()
                raise RuntimeError(
                    f"{nome}: SHA-256 {obtido[:12]}… não confere com o manifesto ({esperado[:12]}…)"
                )
            parcial.replace(destino)
            baixados += 1
    return baixados
