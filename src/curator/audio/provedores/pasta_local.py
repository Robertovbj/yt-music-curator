"""Arquivos que você já tem: indexados por tags (mutagen) e, se o `fpcalc` existir, por fingerprint Chromaprint.

O casamento usa tags e duração. O fingerprint fica guardado para quando houver uma consulta ao AcoustID
(precisa de chave de API) ou para comparar com o áudio de outra fonte; sem `fpcalc` o índice funciona só com tags.
"""

import json
import logging
import shutil
import sqlite3
import subprocess
from pathlib import Path

from curator.audio import casamento
from curator.audio.provedores.base import Arquivo, Candidato, FaixaAlvo, Saida
from curator.banco.conexao import agora, transacao
from curator.texto import normalizar, normalizar_titulo

log = logging.getLogger(__name__)

EXTENSOES = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav", ".wma", ".aiff", ".aif"}


def _tags(arquivo: Path) -> tuple[str | None, str | None, str | None, float | None]:
    import mutagen

    try:
        m = mutagen.File(arquivo, easy=True)
    except Exception as e:
        log.info("sem tags legíveis em %s: %s", arquivo, e)
        return None, None, None, None
    if m is None:
        return None, None, None, None
    primeiro = lambda chave: (m.get(chave) or [None])[0]  # noqa: E731
    return primeiro("artist"), primeiro("title"), primeiro("album"), getattr(m.info, "length", None)


def _fingerprint(arquivo: Path) -> str | None:
    if not shutil.which("fpcalc"):
        return None
    try:
        r = subprocess.run(
            ["fpcalc", "-json", str(arquivo)], capture_output=True, text=True, timeout=120, check=True
        )
        return json.loads(r.stdout).get("fingerprint")
    except Exception as e:
        log.info("fpcalc falhou em %s: %s", arquivo, e)
        return None


def indexar(con: sqlite3.Connection, pasta: Path, ler_tags=_tags, fingerprint=_fingerprint) -> dict[str, int]:
    """Indexa arquivos novos ou alterados (mtime/tamanho) e remove do índice os que sumiram."""
    contagem = {"novos": 0, "inalterados": 0, "removidos": 0}
    conhecidos = {
        r["caminho"]: (r["mtime"], r["tamanho"])
        for r in con.execute("SELECT caminho, mtime, tamanho FROM arquivos_locais")
    }
    vistos = set()
    for arq in sorted(p for p in pasta.rglob("*") if p.suffix.lower() in EXTENSOES and p.is_file()):
        st = arq.stat()
        chave = str(arq.resolve())
        vistos.add(chave)
        if conhecidos.get(chave) == (st.st_mtime, st.st_size):
            contagem["inalterados"] += 1
            continue
        artista, titulo, album, dur = ler_tags(arq)
        con.execute(
            """INSERT OR REPLACE INTO arquivos_locais (caminho, mtime, tamanho, duracao_s, artista, titulo, album,
                 artista_norm, titulo_norm, fingerprint, indexado_em) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (chave, st.st_mtime, st.st_size, dur, artista, titulo or arq.stem, album,
             normalizar(artista), normalizar_titulo(titulo or arq.stem), fingerprint(arq), agora()),
        )  # fmt: skip
        contagem["novos"] += 1
    with transacao(con):
        for caminho in set(conhecidos) - vistos:
            con.execute("DELETE FROM arquivos_locais WHERE caminho = ?", (caminho,))
            contagem["removidos"] += 1
    return contagem


class PastaLocal:
    nome = "pasta_local"
    usa_rede = False

    def __init__(self, con: sqlite3.Connection, tolerancia_s: float = 3):
        self.con = con
        self.tolerancia_s = tolerancia_s

    def buscar(self, faixa: FaixaAlvo, saida: Saida | None) -> list[Candidato]:
        titulo = normalizar_titulo(faixa.titulo)
        if not titulo:
            return []
        # Pré-filtro barato no SQL (mesmo título, ou título contendo/contido); o score decide.
        linhas = self.con.execute(
            "SELECT caminho, artista, titulo, duracao_s FROM arquivos_locais "
            "WHERE titulo_norm = :t OR titulo_norm LIKE '%' || :t || '%' OR :t LIKE '%' || titulo_norm || '%'",
            {"t": titulo},
        ).fetchall()
        candidatos = [
            Candidato(self.nome, r["caminho"], r["titulo"], r["artista"], r["duracao_s"],
                      casamento.score(faixa, r["titulo"], r["artista"], r["duracao_s"], self.tolerancia_s))
            for r in linhas
            if Path(r["caminho"]).exists()
        ]  # fmt: skip
        return sorted(candidatos, key=lambda c: (-c.score, c.ref))

    def baixar(self, candidato: Candidato, destino: Path, saida: Saida | None) -> Arquivo:
        caminho = Path(candidato.ref)
        return Arquivo(caminho, 0, temporario=False)  # lido direto, sem cópia nem rede
