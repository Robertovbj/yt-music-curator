"""Escrita de cada faixa (#111): japonesa, coreana, chinesa, cirílica ou latina, pelos metadados.

O áudio não diz o idioma nem o mercado: o cluster de "J-pop" das masters juntava pop ocidental, eletrônica de osu e
nightcore porque soam parecidos. A escrita do título, do álbum e dos artistas separa o que o embedding não separa, e
sai dos metadados já sincronizados (sem rede, sem reanalisar áudio).

Regras, em ordem (determinísticas; mudar alguma pede um `VERSAO` novo):

1. **Escrita própria** da faixa, no título, no álbum e nos nomes dos artistas:
   - kana (hiragana/katakana) → japonesa. "・" e "ー" sozinhos não contam;
   - hangul → coreana; cirílico → cirílica;
   - ideogramas (han) SEM kana ficam em aberto: o japonês também escreve só com kanji ("怪物", "群青").
2. **Escrita do artista**: a que aparece por conta própria em pelo menos `LIMIAR_ARTISTA` das faixas dele que o
   banco conhece (curtidas, playlists e candidatas de recomendação), com no mínimo uma faixa. Ideogramas sem kana
   contam como chinesa para o artista que não é japonês.
3. **Faixa**: as escritas próprias; ideogramas sem kana viram japonesa se algum artista é japonês, senão chinesa.
   Sem nenhuma escrita não latina, a faixa herda a dos artistas (o "MONSTER" da Reol é japonês). Sem nada disso e com
   letras latinas, é latina. Uma faixa pode ter mais de uma (parceria entre japonês e coreano).

É um sinal, não um rótulo: um artista japonês que só publica títulos em inglês fica latino. Para o que a escrita
não resolve, as marcações do usuário (#107) valem mais.
"""

import hashlib
import json
import sqlite3
from collections import defaultdict
from typing import Literal

VERSAO = 1
ESCRITAS: tuple[str, ...] = ("japonesa", "coreana", "chinesa", "cirilica", "latina")
Escrita = Literal["japonesa", "coreana", "chinesa", "cirilica", "latina"]
LIMIAR_ARTISTA = 0.3

# estilos do Discogs400 que dizem mercado ou idioma, não som: o nome automático de um cluster só os cita quando a
# escrita das faixas sustenta (ver clusters.nome_automatico)
MERCADOS = {"Pop---J-pop": "japonesa", "Pop---Kayōkyoku": "japonesa", "Pop---K-pop": "coreana"}
SUSTENTA_MERCADO = 0.5


def _kana(c: int) -> bool:
    return (
        0x3041 <= c <= 0x309F
        or (0x30A0 <= c <= 0x30FF and c not in (0x30FB, 0x30FC))
        or 0x31F0 <= c <= 0x31FF
    ) or 0xFF66 <= c <= 0xFF9D


def _han(c: int) -> bool:
    return 0x4E00 <= c <= 0x9FFF or 0x3400 <= c <= 0x4DBF or 0xF900 <= c <= 0xFAFF or 0x20000 <= c <= 0x2FA1F


def _hangul(c: int) -> bool:
    return 0xAC00 <= c <= 0xD7AF or 0x1100 <= c <= 0x11FF or 0x3130 <= c <= 0x318F


def _latina(c: int) -> bool:
    return 0x41 <= c <= 0x5A or 0x61 <= c <= 0x7A or (0xC0 <= c <= 0x24F and c not in (0xD7, 0xF7))


def do_texto(texto: str) -> set[str]:
    """Escritas que aparecem no texto: as de `ESCRITAS`, com "han" para ideogramas sem kana (ainda em aberto)."""
    achou: set[str] = set()
    for ch in texto:
        c = ord(ch)
        if c < 0x80:
            if _latina(c):
                achou.add("latina")
        elif _kana(c):
            achou.add("japonesa")
        elif _han(c):
            achou.add("han")
        elif _hangul(c):
            achou.add("coreana")
        elif 0x400 <= c <= 0x4FF:
            achou.add("cirilica")
        elif _latina(c):
            achou.add("latina")
    if "japonesa" in achou:
        achou.discard("han")
    return achou


def _propria(titulo: str, album: str | None, artistas: list[dict]) -> set[str]:
    return do_texto(" ".join([titulo or "", album or "", *(a.get("nome") or "" for a in artistas)]))


def calcular(con: sqlite3.Connection, ids: list[str] | None = None) -> dict[str, frozenset[str]]:
    """video_id → escritas, para `ids` (padrão: todas as faixas do banco). A escrita dos artistas sai de todas."""
    proprias: dict[str, set[str]] = {}
    artistas_de: dict[str, list[str]] = {}
    for vid, titulo, album, lista in con.execute("SELECT video_id, titulo, album_nome, artistas FROM faixas"):
        arts = json.loads(lista or "[]")
        proprias[vid] = _propria(titulo, album, arts)
        artistas_de[vid] = [a["id"] for a in arts if a.get("id")]
    total: dict[str, int] = defaultdict(int)
    com: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for vid, esc in proprias.items():
        for a in set(artistas_de[vid]):
            total[a] += 1
            for e in esc - {"latina"}:
                com[a][e] += 1
    do_artista: dict[str, set[str]] = {}
    for a, n in total.items():
        es = {e for e, k in com[a].items() if k / n >= LIMIAR_ARTISTA}
        if "han" in es:
            es.discard("han")
            if "japonesa" not in es:
                es.add("chinesa")
        do_artista[a] = es
    alvo = proprias if ids is None else {v: proprias[v] for v in ids if v in proprias}
    out: dict[str, frozenset[str]] = {}
    for vid, propria in alvo.items():
        dos_artistas = set().union(*(do_artista.get(a, set()) for a in artistas_de[vid]))
        esc = propria - {"latina", "han"}
        if "han" in propria:
            esc.add("japonesa" if "japonesa" in dos_artistas else "chinesa")
        if not esc:
            esc = set(dos_artistas)
        if not esc and "latina" in propria:
            esc = {"latina"}
        out[vid] = frozenset(esc)
    return out


def proporcoes(esc: dict[str, frozenset[str]], ids: list[str]) -> dict[str, float]:
    """Fração das faixas com cada escrita (uma faixa pode contar em mais de uma), só as que aparecem."""
    if not ids:
        return {}
    conta: dict[str, int] = defaultdict(int)
    for v in ids:
        for e in esc.get(v, ()):
            conta[e] += 1
    return {e: round(conta[e] / len(ids), 3) for e in ESCRITAS if conta.get(e)}


def versao(esc: dict[str, frozenset[str]]) -> str:
    """Hash das escritas usadas numa execução (entra na versão do banco, como o das marcações)."""
    dump = "\n".join(f"{v}:{','.join(sorted(e))}" for v, e in sorted(esc.items()))
    return f"v{VERSAO}:{hashlib.sha256(dump.encode()).hexdigest()[:12]}"
