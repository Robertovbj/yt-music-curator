"""Backup e restauração (M8, #50 e #51): tudo o que é caro de refazer num .zip, e o caminho de volta.

Entra no zip:
- o banco (análises, vetores, receitas, planos, clusters), copiado pela API de backup online do SQLite,
  que dá uma cópia consistente mesmo com o worker gravando;
- `config.toml` e `hierarquia.toml`;
- com `--com-credenciais`: `browser.json` (cookies da conta) e `saidas.env` (chave do Tailscale, VPN).

Fica de fora o que se baixa ou gera de novo: modelos Essentia, `mapa.html`, a trava do worker.
O `manifesto.json` traz versões, contagens e o SHA-256 de cada arquivo; a restauração confere tudo antes
de tocar em qualquer coisa e nunca apaga dados existentes (com `--forcar`, eles são renomeados).
"""

import hashlib
import json
import os
import socket
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from curator import __version__
from curator.config import Caminhos

FORMATO = 1
CONFIGS = ("config.toml", "hierarquia.toml")
CREDENCIAIS = ("browser.json", "saidas.env")
CONTAGENS = {
    "faixas": "SELECT count(*) FROM faixas",
    "curtidas": "SELECT count(*) FROM faixas WHERE curtida = 1",
    "playlists": "SELECT count(*) FROM playlists WHERE propria = 1",
    "analises_ok": "SELECT count(*) FROM analises WHERE status = 'ok'",
    "vetores": "SELECT count(*) FROM vetores",
    "fila_pendente": "SELECT count(*) FROM fila_download WHERE status IN ('pendente', 'erro')",
    "receitas": "SELECT count(*) FROM receitas",
    "planos": "SELECT count(*) FROM planos",
}


class BackupInvalido(ValueError):
    pass


def _sha256(caminho: Path) -> str:
    h = hashlib.sha256()
    with caminho.open("rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


def contagens(con: sqlite3.Connection) -> dict[str, int]:
    out = {}
    for nome, sql in CONTAGENS.items():
        try:
            out[nome] = con.execute(sql).fetchone()[0]
        except sqlite3.OperationalError:  # schema antigo sem a tabela
            out[nome] = 0
    return out


def criar(destino: Path, c: Caminhos, com_credenciais: bool = False) -> dict:
    """Gera o zip e devolve o manifesto."""
    if not c.banco.exists():
        raise BackupInvalido(f"não há banco em {c.banco}")
    from curator.audio.analisador import VERSAO_ANALISADOR
    from curator.audio.modelos import hash_modelos

    with tempfile.TemporaryDirectory(prefix="curator-backup-") as tmp:
        copia = Path(tmp) / "curator.db"
        origem = sqlite3.connect(f"file:{c.banco}?mode=ro", uri=True)
        alvo = sqlite3.connect(copia)
        origem.backup(alvo)  # cópia consistente, mesmo com outra conexão gravando
        origem.close()
        schema = alvo.execute("PRAGMA user_version").fetchone()[0]
        alvo.row_factory = sqlite3.Row
        cont = contagens(alvo)
        alvo.close()

        arquivos = {"dados/curator.db": copia}
        for nome in CONFIGS + (CREDENCIAIS if com_credenciais else ()):
            if (c.config_dir / nome).exists():
                arquivos[f"config/{nome}"] = c.config_dir / nome
        manifesto = {
            "formato": FORMATO,
            "versao_curator": __version__,
            "schema": schema,
            "versao_analisador": VERSAO_ANALISADOR,
            "hash_modelos": hash_modelos(),
            "criado_em": datetime.now().astimezone().isoformat(timespec="seconds"),
            "origem": socket.gethostname(),
            "com_credenciais": com_credenciais,
            "contagens": cont,
            "arquivos": {
                nome: {"sha256": _sha256(p), "bytes": p.stat().st_size} for nome, p in arquivos.items()
            },
        }
        destino.parent.mkdir(parents=True, exist_ok=True)
        parcial = destino.with_name(destino.name + ".part")
        with zipfile.ZipFile(parcial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.writestr("manifesto.json", json.dumps(manifesto, ensure_ascii=False, indent=2))
            for nome, p in arquivos.items():
                z.write(p, nome)
        if com_credenciais:
            os.chmod(parcial, 0o600)
        parcial.replace(destino)
    return manifesto


def verificar(arquivo: Path) -> tuple[dict, list[str]]:
    """(manifesto, problemas). Problemas vazios = zip íntegro."""
    try:
        z = zipfile.ZipFile(arquivo)
    except (zipfile.BadZipFile, FileNotFoundError) as e:
        raise BackupInvalido(f"não é um backup legível: {e}") from e
    with z:
        try:
            manifesto = json.loads(z.read("manifesto.json"))
        except KeyError as e:
            raise BackupInvalido("zip sem manifesto.json: não foi gerado pelo `curator backup criar`") from e
        problemas = []
        if manifesto.get("formato") != FORMATO:
            problemas.append(
                f"formato {manifesto.get('formato')} desconhecido (este curator lê o formato {FORMATO})"
            )
        nomes = set(z.namelist()) - {"manifesto.json"}
        for nome, info in manifesto.get("arquivos", {}).items():
            if nome not in nomes:
                problemas.append(f"falta no zip: {nome}")
                continue
            if hashlib.sha256(z.read(nome)).hexdigest() != info["sha256"]:
                problemas.append(f"checksum divergente: {nome}")
        for extra in sorted(nomes - set(manifesto.get("arquivos", {}))):
            problemas.append(f"arquivo fora do manifesto: {extra}")
        if "dados/curator.db" not in manifesto.get("arquivos", {}):
            problemas.append("o backup não contém o banco")
    return manifesto, problemas
