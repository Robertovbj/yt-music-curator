"""Abertura do SQLite e migrações numeradas (`migracoes/NNN_nome.sql`, versão em `PRAGMA user_version`)."""

import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

log = logging.getLogger(__name__)

Migracao = tuple[int, str, str]  # (número, nome, sql)


def agora() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def migracoes_do_pacote() -> list[Migracao]:
    out = []
    for arq in resources.files("curator.banco.migracoes").iterdir():
        m = re.fullmatch(r"(\d{3})_(.+)\.sql", arq.name)
        if m:
            out.append((int(m[1]), m[2], arq.read_text(encoding="utf-8")))
    return sorted(out)


def versao(con: sqlite3.Connection) -> int:
    return con.execute("PRAGMA user_version").fetchone()[0]


def migrar(con: sqlite3.Connection, migracoes: list[Migracao] | None = None) -> int:
    """Aplica, em ordem e cada uma numa transação, as migrações acima da versão atual."""
    migracoes = migracoes_do_pacote() if migracoes is None else migracoes
    atual = versao(con)
    for numero, nome, sql in migracoes:
        if numero <= atual:
            continue
        log.info("aplicando migração %03d_%s", numero, nome)
        try:
            con.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {numero};\nCOMMIT;")
        except sqlite3.Error:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        atual = numero
    return atual


@contextmanager
def transacao(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN ... COMMIT, com ROLLBACK em caso de erro."""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


def abrir(caminho: Path | str, migracoes: list[Migracao] | None = None) -> sqlite3.Connection:
    """Abre o banco em modo autocommit; escritas em lote usam `transacao()`."""
    if str(caminho) != ":memory:":
        Path(caminho).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(caminho, autocommit=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA busy_timeout = 5000")
    migrar(con, migracoes)
    return con
