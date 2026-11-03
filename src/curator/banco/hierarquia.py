"""Árvore mãe/filha das playlists (ADR-03), declarada em `hierarquia.toml` e espelhada em `playlists.mae_id`.

Formato:

    [[playlist]]
    id = "PL..."            # obrigatório
    titulo = "Rock - All"   # informativo; ignorado na leitura
    tipo = "agregadora"     # agregadora (contém todas as filhas) | folha (padrão)

    [[playlist]]
    id = "PL..."
    mae = "PL..."

Playlists próprias que não aparecem no arquivo ficam sem mãe e como folha.
"""

import re
import sqlite3
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from curator.banco.conexao import transacao


class HierarquiaInvalida(ValueError):
    pass


@dataclass(frozen=True)
class No:
    id: str
    mae: str | None
    tipo: str


def ler(arquivo: Path) -> list[No]:
    with arquivo.open("rb") as f:
        dados = tomllib.load(f)
    nos = []
    for i, p in enumerate(dados.get("playlist", []), start=1):
        if "id" not in p:
            raise HierarquiaInvalida(f"entrada {i} sem 'id'")
        tipo = p.get("tipo", "folha")
        if tipo not in ("agregadora", "folha"):
            raise HierarquiaInvalida(f"{p['id']}: tipo '{tipo}' inválido (use agregadora ou folha)")
        nos.append(No(p["id"], p.get("mae"), tipo))
    return nos


def validar(con: sqlite3.Connection, nos: list[No]) -> None:
    erros = []
    ids = [n.id for n in nos]
    repetidos = {i for i in ids if ids.count(i) > 1}
    if repetidos:
        erros.append(f"ids repetidos: {', '.join(sorted(repetidos))}")
    existentes = {r[0] for r in con.execute("SELECT playlist_id FROM playlists WHERE propria = 1")}
    for n in nos:
        if n.id not in existentes:
            erros.append(f"{n.id}: não é uma playlist própria conhecida (rode `curator sincronizar`)")
        if n.mae is not None and n.mae not in existentes:
            erros.append(f"{n.id}: mãe {n.mae} não é uma playlist própria conhecida")
        if n.mae == n.id:
            erros.append(f"{n.id}: é mãe de si mesma")
    mae_de = {n.id: n.mae for n in nos}
    for inicio in mae_de:
        vistos, atual = set(), inicio
        while atual is not None:
            if atual in vistos:
                erros.append(f"ciclo envolvendo {inicio}")
                break
            vistos.add(atual)
            atual = mae_de.get(atual)
    if erros:
        raise HierarquiaInvalida("\n".join(dict.fromkeys(erros)))


def aplicar(con: sqlite3.Connection, nos: list[No]) -> int:
    validar(con, nos)
    with transacao(con):
        con.execute("UPDATE playlists SET mae_id = NULL, tipo = 'folha'")
        for n in nos:  # primeiro os tipos, depois as mães (FK)
            con.execute("UPDATE playlists SET tipo = ? WHERE playlist_id = ?", (n.tipo, n.id))
        for n in nos:
            con.execute("UPDATE playlists SET mae_id = ? WHERE playlist_id = ?", (n.mae, n.id))
    return len(nos)


def arvore(con: sqlite3.Connection) -> str:
    linhas = con.execute(
        """SELECT p.playlist_id, p.titulo, p.mae_id, p.tipo, count(pf.video_id) AS n
           FROM playlists p LEFT JOIN playlist_faixas pf USING (playlist_id)
           WHERE p.propria = 1 GROUP BY p.playlist_id ORDER BY p.titulo"""
    ).fetchall()
    filhas = defaultdict(list)
    for linha in linhas:
        filhas[linha["mae_id"]].append(linha)
    out: list[str] = []

    def desenhar(mae: str | None, prefixo: str) -> None:
        itens = filhas.get(mae, [])
        for i, p in enumerate(itens):
            ultimo = i == len(itens) - 1
            ramo = "" if mae is None else ("└─ " if ultimo else "├─ ")
            marca = " [agregadora]" if p["tipo"] == "agregadora" else ""
            out.append(f"{prefixo}{ramo}{p['titulo']} ({p['n']}){marca}")
            desenhar(p["playlist_id"], prefixo + ("" if mae is None else ("   " if ultimo else "│  ")))

    desenhar(None, "")
    return "\n".join(out)


def faltantes_nas_agregadoras(con: sqlite3.Connection) -> dict[str, list[str]]:
    """{mãe agregadora: [video_ids que estão em alguma descendente e não nela]}."""
    out = {}
    for (mae,) in con.execute("SELECT playlist_id FROM playlists WHERE tipo = 'agregadora'").fetchall():
        faltam = con.execute(
            """WITH RECURSIVE desc(id) AS (
                   SELECT playlist_id FROM playlists WHERE mae_id = :mae
                   UNION SELECT p.playlist_id FROM playlists p JOIN desc ON p.mae_id = desc.id)
               SELECT DISTINCT pf.video_id FROM playlist_faixas pf JOIN desc ON pf.playlist_id = desc.id
               WHERE pf.video_id NOT IN (SELECT video_id FROM playlist_faixas WHERE playlist_id = :mae)
               ORDER BY pf.video_id""",
            {"mae": mae},
        ).fetchall()
        if faltam:
            out[mae] = [f[0] for f in faltam]
    return out


_MAE = re.compile(r"^(all|geral|tudo|todas|todos|mix)$", re.IGNORECASE)


def sugerir(con: sqlite3.Connection) -> str:
    """`hierarquia.toml` inicial a partir de títulos 'Prefixo - Nome' (ex.: 'Rock - All' é mãe de 'Rock - Arena')."""
    grupos = defaultdict(list)
    soltas = []
    for p in con.execute("SELECT playlist_id, titulo FROM playlists WHERE propria = 1 ORDER BY titulo"):
        m = re.match(r"^\s*(.+?)\s+[-–—|:]\s+(.+)$", p["titulo"])
        (grupos[m[1].strip()].append((p, m[2].strip())) if m else soltas.append(p))
    # uma playlist chamada só "Rock" também serve de mãe do grupo "Rock - ..."
    por_titulo = {p["titulo"].strip(): p for p in soltas}

    blocos = ["# Gerado por `curator hierarquia sugerir`. Revise antes de `curator hierarquia aplicar`.\n"]
    for prefixo, membros in sorted(grupos.items()):
        mae = por_titulo.pop(prefixo, None)
        if mae is None:
            mae = next((p for p, nome in membros if _MAE.match(nome)), None)
        filhas = [p for p, _ in membros if mae is not None and p["playlist_id"] != mae["playlist_id"]]
        if not filhas:
            continue
        blocos.append(bloco_toml(mae, tipo="agregadora"))
        blocos.extend(bloco_toml(p, mae=mae["playlist_id"]) for p in filhas)
    return "\n".join(blocos)


def bloco_toml(p, tipo: str | None = None, mae: str | None = None) -> str:
    linhas = ["[[playlist]]", f'id = "{p["playlist_id"]}"', f"titulo = {_toml_str(p['titulo'])}"]
    if tipo:
        linhas.append(f'tipo = "{tipo}"')
    if mae:
        linhas.append(f'mae = "{mae}"')
    return "\n".join(linhas) + "\n"


def _toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
