"""Marcações por artista (#107): etiquetas como "k-pop", "vtuber" ou "pop-ocidental", dadas pelo usuário.

O áudio sozinho não separa pop ocidental de J-pop, nem K-pop de pop: na revisão da J-pop & VTubers (13/11), 59 faixas
de pop ocidental e 17 de K-pop estavam lá. O artista separa, na maioria dos casos.

- **Dados, não regra.** A etiqueta de uma faixa vem dos artistas dela (todos, não só o principal: uma parceria de
  artistas com etiquetas diferentes fica com as duas). Uma exceção da faixa vence o artista: `sim = 1` acrescenta a
  etiqueta, `sim = 0` tira. Artista misto (HoYo-MiX faz trilha e também K-pop) fica sem etiqueta; só a faixa recebe.
- **Uso pela IA.** As receitas filtram (`exigir_marcacoes`, `excluir_marcacoes`) e pontuam (`marcacoes_preferidas`)
  por etiqueta, conforme o pedido.
- **Regras de destino, opcionais e de cada pessoa** (`[[marcacoes.regras]]` no config.toml): a etiqueta põe a faixa
  em masters (`entrar_em`) e tira de outras (`sair_de`). Entrar vence sair: uma faixa de VTuber que também é K-pop
  fica nas duas. Sem regra, as marcações só informam.
- **Fora do classificador de áudio:** não viram feature, não entram nos limiares nem na calibração. As regras valem
  depois do `classificar --nas-gerais` e no plano que corrige as masters (`plano`).
"""

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import get_close_matches

from curator.banco import consultas
from curator.banco.conexao import agora
from curator.texto import normalizar

ORIGENS = ("cli", "mcp")
_ETIQUETA = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class MarcacaoInvalida(ValueError):
    pass


def etiqueta(texto: str) -> str:
    """Forma canônica: minúsculas, sem acento, palavras unidas por hífen ("Pop ocidental" → "pop-ocidental")."""
    e = normalizar(texto).replace(" ", "-").replace("_", "-")
    if not _ETIQUETA.match(e):
        raise MarcacaoInvalida(f"etiqueta inválida: '{texto}' (use letras, números e hífens, como 'k-pop')")
    return e


# ------------------------------------------------------------------ gravar


def artistas(con: sqlite3.Connection, ref: str) -> list[tuple[str, str]]:
    """[(artista_id, nome)] pelo id exato ou pelo nome (sem acento/caixa). O mesmo nome em canais diferentes vem
    todo: é o mesmo artista para o usuário. Levanta MarcacaoInvalida com sugestões se não achar."""
    linhas = con.execute("SELECT artista_id, nome FROM artistas WHERE artista_id = ?", (ref,)).fetchall()
    if linhas:
        return [tuple(r) for r in linhas]
    alvo = normalizar(ref)
    todos = con.execute("SELECT artista_id, nome FROM artistas ORDER BY nome, artista_id").fetchall()
    achados = [tuple(r) for r in todos if normalizar(r["nome"]) == alvo]
    if achados:
        return achados
    nomes = {normalizar(r["nome"]): r["nome"] for r in todos}
    perto = [nomes[n] for n in get_close_matches(alvo, list(nomes), n=3, cutoff=0.6)]
    raise MarcacaoInvalida(
        f"artista '{ref}' não está no banco" + (f" (quis dizer: {', '.join(perto)}?)" if perto else "")
    )


def marcar_artista(
    con: sqlite3.Connection, ref: str, etq: str, origem: str = "cli", desfazer: bool = False
) -> list[tuple[str, str]]:
    """Marca (ou desmarca) todos os canais do artista. Devolve os [(artista_id, nome)] afetados."""
    assert origem in ORIGENS
    e, achados = etiqueta(etq), artistas(con, ref)
    for aid, _ in achados:
        if desfazer:
            con.execute("DELETE FROM marcacoes_artista WHERE artista_id = ? AND etiqueta = ?", (aid, e))
        else:
            con.execute(
                "INSERT INTO marcacoes_artista (artista_id, etiqueta, origem, criado_em) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (artista_id, etiqueta) DO NOTHING",
                (aid, e, origem, agora()),
            )
    return achados


def marcar_faixa(
    con: sqlite3.Connection, video_id: str, etq: str, sim: bool | None, origem: str = "cli"
) -> None:
    """Exceção da faixa: sim=True acrescenta a etiqueta, False tira (vence o artista), None apaga a exceção."""
    assert origem in ORIGENS
    e = etiqueta(etq)
    if not con.execute("SELECT 1 FROM faixas WHERE video_id = ?", (video_id,)).fetchone():
        raise MarcacaoInvalida(f"faixa {video_id} não está no banco")
    if sim is None:
        con.execute("DELETE FROM marcacoes_faixa WHERE video_id = ? AND etiqueta = ?", (video_id, e))
    else:
        con.execute(
            "INSERT INTO marcacoes_faixa (video_id, etiqueta, sim, origem, criado_em) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (video_id, etiqueta) DO UPDATE SET sim = excluded.sim, origem = excluded.origem, "
            "criado_em = excluded.criado_em",
            (video_id, e, int(sim), origem, agora()),
        )


# ------------------------------------------------------------------ ler


def da_faixa(con: sqlite3.Connection, ids: list[str] | None = None) -> dict[str, set[str]]:
    """video_id → etiquetas efetivas (artistas + exceções da faixa). Só as faixas com alguma etiqueta."""
    out: dict[str, set[str]] = defaultdict(set)
    for vid, e in con.execute(
        "SELECT fa.video_id, m.etiqueta FROM faixa_artistas fa JOIN marcacoes_artista m USING (artista_id)"
    ):
        out[vid].add(e)
    for vid, e, sim in con.execute("SELECT video_id, etiqueta, sim FROM marcacoes_faixa"):
        (out[vid].add if sim else out[vid].discard)(e)
    if ids is not None:
        quer = set(ids)
        return {v: s for v, s in out.items() if v in quer and s}
    return {v: s for v, s in out.items() if s}


def existentes(con: sqlite3.Connection) -> list[str]:
    """As etiquetas em uso (de artista ou de faixa), em ordem."""
    return [
        r[0]
        for r in con.execute(
            "SELECT etiqueta FROM marcacoes_artista UNION SELECT etiqueta FROM marcacoes_faixa WHERE sim = 1 "
            "ORDER BY 1"
        )
    ]


def resumo(con: sqlite3.Connection, etq: str | None = None) -> list[dict]:
    """Por etiqueta: artistas marcados, exceções de faixa e quantas faixas da biblioteca (sem a lista negra) ficam
    com ela."""
    alvo = etiqueta(etq) if etq else None
    bloqueadas = consultas.bloqueadas(con)
    por_faixa = da_faixa(con)
    contagem: dict[str, int] = defaultdict(int)
    for v, es in por_faixa.items():
        if v not in bloqueadas:
            for e in es:
                contagem[e] += 1
    arts: dict[str, list[str]] = defaultdict(list)
    for e, nome in con.execute(
        "SELECT m.etiqueta, a.nome FROM marcacoes_artista m JOIN artistas a USING (artista_id) "
        "ORDER BY m.etiqueta, a.nome"
    ):
        if nome not in arts[e]:
            arts[e].append(nome)
    exc: dict[str, dict[str, list[str]]] = defaultdict(lambda: {"sim": [], "nao": []})
    for e, vid, sim in con.execute(
        "SELECT etiqueta, video_id, sim FROM marcacoes_faixa ORDER BY etiqueta, video_id"
    ):
        exc[e]["sim" if sim else "nao"].append(vid)
    nomes = sorted(set(arts) | set(exc) | set(contagem))
    return [
        {"etiqueta": e, "faixas": contagem.get(e, 0), "artistas": arts.get(e, []),
         "faixas_com": exc[e]["sim"] if e in exc else [], "faixas_sem": exc[e]["nao"] if e in exc else []}
        for e in nomes
        if alvo is None or e == alvo
    ]  # fmt: skip


# ------------------------------------------------------------------ regras de destino (config de cada pessoa)


@dataclass
class Regra:
    etiqueta: str
    entrar_em: list[str] = field(default_factory=list)  # playlist_ids (masters)
    sair_de: list[str] = field(default_factory=list)


def regras(con: sqlite3.Connection, cfg: dict) -> list[Regra]:
    """As `[[marcacoes.regras]]` do config, com as masters resolvidas (id ou título). Só masters: as outras
    playlists seguem a lógica do usuário e não são mexidas por regra. Levanta MarcacaoInvalida."""
    from curator.motor import gerais

    masters = set(gerais.playlists(con))
    titulos = dict(con.execute("SELECT playlist_id, titulo FROM playlists"))
    out, erros = [], []
    for i, r in enumerate(cfg.get("marcacoes", {}).get("regras", []) or [], start=1):
        try:
            e = etiqueta(str(r.get("etiqueta", "")))
        except MarcacaoInvalida as x:
            erros.append(f"regra {i}: {x}")
            continue
        extra = set(r) - {"etiqueta", "entrar_em", "sair_de"}
        if extra:
            erros.append(f"regra {i} ({e}): campo(s) desconhecido(s): {', '.join(sorted(extra))}")
        regra = Regra(e)
        for campo in ("entrar_em", "sair_de"):
            for ref in r.get(campo, []) or []:
                pid = next((p for p in masters if p == ref or titulos.get(p) == ref), None)
                if pid is None:
                    erros.append(f"regra {i} ({e}): {campo} '{ref}' não é uma playlist master")
                else:
                    getattr(regra, campo).append(pid)
        if not regra.entrar_em and not regra.sair_de:
            erros.append(f"regra {i} ({e}): sem entrar_em nem sair_de")
        out.append(regra)
    if erros:
        raise MarcacaoInvalida("config.toml, [[marcacoes.regras]]:\n" + "\n".join(erros))
    return out


def destinos_por_regra(etiquetas_faixa: set[str], rs: list[Regra]) -> tuple[dict[str, str], dict[str, str]]:
    """({playlist: etiqueta que manda entrar}, {playlist: etiqueta que manda sair}). Entrar vence sair."""
    entrar: dict[str, str] = {}
    sair: dict[str, str] = {}
    for r in rs:
        if r.etiqueta in etiquetas_faixa:
            for p in r.entrar_em:
                entrar.setdefault(p, r.etiqueta)
            for p in r.sair_de:
                sair.setdefault(p, r.etiqueta)
    return entrar, {p: e for p, e in sair.items() if p not in entrar}


def plano(con: sqlite3.Connection, rs: list[Regra]) -> int:
    """Plano que corrige as masters atuais, só com o que contraria as regras: as curtidas com a etiqueta entram nas
    masters de `entrar_em`, e as faixas com ela saem das de `sair_de`. Dislikes e lista negra não entram. Levanta
    ValueError se não há nada a corrigir."""
    from curator.motor.planos import Operacao, criar

    if not rs:
        raise ValueError("nenhuma regra em [[marcacoes.regras]] no config.toml")
    bloqueadas = consultas.bloqueadas(con)
    curtidas = {r[0] for r in con.execute("SELECT video_id FROM faixas WHERE curtida = 1")}
    alvos = {p for r in rs for p in (*r.entrar_em, *r.sair_de)}
    membros: dict[str, set[str]] = defaultdict(set)
    for pid, vid in con.execute(
        f"SELECT playlist_id, video_id FROM playlist_faixas WHERE playlist_id IN ({','.join('?' * len(alvos))})",
        sorted(alvos),
    ):
        membros[pid].add(vid)
    add: dict[str, dict[str, str]] = defaultdict(dict)
    rem: dict[str, dict[str, str]] = defaultdict(dict)
    for vid, es in sorted(da_faixa(con).items()):
        entrar, sair = destinos_por_regra(es, rs)
        for pid, e in entrar.items():
            if vid in curtidas and vid not in bloqueadas and vid not in membros[pid]:
                add[pid][vid] = f"marcação {e}"
        for pid, e in sair.items():
            if vid in membros[pid]:
                rem[pid][vid] = f"marcação {e}: sai desta master"
    ops = []
    for pid in sorted(alvos):
        if rem[pid]:
            ops.append(Operacao("remover", pid, list(rem[pid]), motivo=rem[pid]))
        if add[pid]:
            ops.append(Operacao("adicionar", pid, list(add[pid]), motivo=add[pid]))
    if not ops:
        raise ValueError("as masters já seguem as regras das marcações: nada a corrigir")
    return criar(con, "marcacoes", ops, propagar_agregadoras=False)
