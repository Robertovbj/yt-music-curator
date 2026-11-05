"""Áudios guardados (#84, experimental): reanalisar sem baixar de novo.

Enquanto o projeto está em desenvolvimento, um modelo novo ou uma mudança no analisador reabre a fila inteira, e
o download é a parte demorada (cota, pausas, quarentenas). Com `[audio] guardar = true`, o worker move o áudio
analisado para `guardar_dir` em vez de apagá-lo. O provedor `guardado` vem sempre antes dos outros: se a faixa
tem arquivo, ele é usado sem rede, sem cota e sem pausa, e a análise registra a origem do download original.

Desligado por padrão: o ADR-08 apaga o áudio depois da análise. Na biblioteca real (05/11) a média foi de 1,6 MB
por faixa, ~5,5 GB para 3.408 faixas (`curator audios status` mostra o espaço; `curator audios limpar` apaga tudo).
"""

import logging
import shutil
import sqlite3
from pathlib import Path

from curator.audio.provedores.base import Arquivo, Candidato, FaixaAlvo, Saida
from curator.banco.conexao import agora

log = logging.getLogger(__name__)

NOME = "guardado"


def pasta(cfg: dict) -> Path:
    from curator.config import caminhos

    return (
        Path(cfg["audio"]["guardar_dir"]).expanduser() if cfg["audio"]["guardar_dir"] else caminhos().audios
    )


def guardar(con: sqlite3.Connection, destino: Path, video_id: str, arquivo: Arquivo, c: Candidato) -> Path:
    """Move o áudio já analisado para `destino/<2 primeiros caracteres>/<video_id>.<ext>` e registra a origem."""
    alvo = destino / video_id[:2] / f"{video_id}{arquivo.caminho.suffix}"
    alvo.parent.mkdir(parents=True, exist_ok=True)
    for antigo in alvo.parent.glob(f"{video_id}.*"):  # outra extensão de um download anterior
        antigo.unlink()
    shutil.move(arquivo.caminho, alvo)
    con.execute(
        """INSERT INTO audios_guardados (video_id, caminho, provedor, ref, score, exato, parcial, bytes, guardado_em)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (video_id) DO UPDATE SET caminho = excluded.caminho, provedor = excluded.provedor,
             ref = excluded.ref, score = excluded.score, exato = excluded.exato, parcial = excluded.parcial,
             bytes = excluded.bytes, guardado_em = excluded.guardado_em""",
        (video_id, str(alvo), c.provedor, c.ref, c.score, int(c.exato), int(arquivo.parcial),
         alvo.stat().st_size, agora()),
    )  # fmt: skip
    return alvo


def resumo(con: sqlite3.Connection) -> tuple[int, int]:
    """(arquivos registrados, bytes)."""
    n, b = con.execute("SELECT count(*), coalesce(sum(bytes), 0) FROM audios_guardados").fetchone()
    return n, b


def limpar(con: sqlite3.Connection) -> int:
    """Apaga todos os arquivos guardados e os registros. Devolve quantos eram."""
    linhas = con.execute("SELECT video_id, caminho FROM audios_guardados").fetchall()
    for r in linhas:
        Path(r["caminho"]).unlink(missing_ok=True)
    con.execute("DELETE FROM audios_guardados")
    return len(linhas)


class Guardado:
    """Provedor do áudio já baixado antes. Arquivo sumido (apagado à mão): o registro sai e a fila segue."""

    nome = NOME
    usa_rede = False

    def __init__(self, con: sqlite3.Connection):
        self.con = con

    def buscar(self, faixa: FaixaAlvo, saida: Saida | None) -> list[Candidato]:
        r = self.con.execute(
            "SELECT * FROM audios_guardados WHERE video_id = ?", (faixa.video_id,)
        ).fetchone()
        if r is None:
            return []
        if not Path(r["caminho"]).is_file():
            log.info("%s: áudio guardado sumiu (%s); baixando de novo", faixa.titulo, r["caminho"])
            self.con.execute("DELETE FROM audios_guardados WHERE video_id = ?", (faixa.video_id,))
            return []
        return [
            Candidato(r["provedor"], r["ref"], faixa.titulo, None, faixa.duracao_s, r["score"], bool(r["exato"]),
                      local=r["caminho"])
        ]  # fmt: skip

    def baixar(self, candidato: Candidato, destino: Path, saida: Saida | None) -> Arquivo:
        r = self.con.execute(
            "SELECT parcial FROM audios_guardados WHERE caminho = ?", (candidato.local,)
        ).fetchone()
        caminho = Path(candidato.local)
        return Arquivo(caminho, caminho.stat().st_size, temporario=False, parcial=bool(r and r["parcial"]))
