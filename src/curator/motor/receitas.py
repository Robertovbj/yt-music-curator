"""Receitas salvas ("playlists inteligentes") e o histórico de execuções (ADR-05, #26)."""

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from curator.banco.conexao import agora
from curator.motor.executor import Execucao, executar
from curator.motor.receita import Receita, carregar, validar
from curator.texto import normalizar


def hash_pedido(pedido: str) -> str:
    """Pedido normalizado (caixa, acentos, pontuação, espaços): a mesma frase cai no mesmo cache."""
    return hashlib.sha256(normalizar(pedido).encode()).hexdigest()[:16]


def salvar(con: sqlite3.Connection, r: Receita, pedido: str | None = None) -> int:
    validar(con, r)
    momento = agora()
    con.execute(
        """INSERT INTO receitas (nome, pedido, spec, hash_spec, hash_pedido, criada_em, atualizada_em)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (nome) DO UPDATE SET pedido = coalesce(excluded.pedido, receitas.pedido), spec = excluded.spec,
             hash_spec = excluded.hash_spec, hash_pedido = coalesce(excluded.hash_pedido, receitas.hash_pedido),
             atualizada_em = excluded.atualizada_em""",
        (r.nome, pedido, r.canonico(), r.hash(), hash_pedido(pedido) if pedido else None, momento, momento),
    )
    return con.execute("SELECT receita_id FROM receitas WHERE nome = ?", (r.nome,)).fetchone()[0]


def obter(con: sqlite3.Connection, nome: str) -> Receita | None:
    linha = con.execute("SELECT spec FROM receitas WHERE nome = ?", (nome,)).fetchone()
    return carregar(linha[0]) if linha else None


def por_pedido(con: sqlite3.Connection, pedido: str) -> Receita | None:
    """Receita já salva para a mesma frase (normalizada), se houver."""
    linha = con.execute(
        "SELECT spec FROM receitas WHERE hash_pedido = ? ORDER BY atualizada_em DESC LIMIT 1",
        (hash_pedido(pedido),),
    ).fetchone()
    return carregar(linha[0]) if linha else None


def listar(con: sqlite3.Connection) -> list[dict]:
    return [
        dict(r)
        for r in con.execute(
            """SELECT r.nome, r.pedido, r.hash_spec, r.atualizada_em, count(e.execucao_id) AS execucoes
               FROM receitas r LEFT JOIN execucoes e USING (receita_id) GROUP BY r.receita_id ORDER BY r.nome"""
        )
    ]


def executar_e_registrar(con: sqlite3.Connection, nome: str) -> tuple[Execucao, int]:
    r = obter(con, nome)
    if r is None:
        raise KeyError(f"receita '{nome}' não existe")
    ex = executar(con, r)
    rid = con.execute("SELECT receita_id FROM receitas WHERE nome = ?", (nome,)).fetchone()[0]
    cur = con.execute(
        "INSERT INTO execucoes (receita_id, versao_banco, resultado, executada_em) VALUES (?, ?, ?, ?)",
        (rid, ex.versao_banco, json.dumps(ex.ids), agora()),
    )
    return ex, cur.lastrowid


@dataclass
class Comparacao:
    jaccard: float
    entraram: list[str]
    sairam: list[str]
    mesma_ordem: bool


def jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb) if sa | sb else 1.0


def comparar(a: list[str], b: list[str]) -> Comparacao:
    return Comparacao(
        round(jaccard(a, b), 4), [v for v in b if v not in set(a)], [v for v in a if v not in set(b)], a == b
    )


def execucoes(con: sqlite3.Connection, nome: str) -> list[dict]:
    return [
        {**dict(r), "resultado": json.loads(r["resultado"])}
        for r in con.execute(
            """SELECT e.execucao_id, e.versao_banco, e.resultado, e.executada_em FROM execucoes e
               JOIN receitas r USING (receita_id) WHERE r.nome = ? ORDER BY e.execucao_id""",
            (nome,),
        )
    ]
