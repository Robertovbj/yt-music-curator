"""Receita: a tradução estruturada de um pedido (ADR-05). A IA escreve a receita; o motor escolhe as músicas.

Vocabulário fechado: features, rótulos, artistas, álbuns e playlists precisam existir no banco. Um nome
errado é recusado com sugestão do mais próximo, para a IA corrigir em vez de o motor adivinhar.
"""

import hashlib
import json
import sqlite3
from difflib import get_close_matches
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from curator.motor.espacos import ESCALARES, TIPOS_PROBABILIDADE
from curator.texto import normalizar

Unidade = Annotated[float, Field(ge=0, le=1)]


class Alvo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alvo: float  # 0..1 para as features, BPM para "bpm"
    peso: Annotated[float, Field(gt=0, le=10)] = 1.0


class Filtros(BaseModel):
    model_config = ConfigDict(extra="forbid")
    excluir_artistas: list[str] = []
    exigir_artistas: list[str] = []  # pelo menos um deles
    excluir_albuns_contendo: list[str] = []  # trecho do nome do álbum (sem acento/caixa)
    excluir_playlists: list[str] = []  # id ou título exato
    somente_playlists: list[str] = []
    bpm: tuple[float, float] | None = None
    duracao_s: tuple[int, int] | None = None
    min: dict[str, float] = {}
    max: dict[str, float] = {}
    generos_min: dict[str, Unidade] = {}  # passa se QUALQUER rótulo listado tiver prob ≥ valor
    genero_pai_min: dict[str, Unidade] = {}  # "Rock": maior prob entre os estilos "Rock---*"


class Diversidade(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    lambda_: Annotated[float, Field(alias="lambda", ge=0, le=1)] = 0.75  # 1 = só relevância (MMR desligado)
    max_por_artista: Annotated[int, Field(ge=1)] | None = 3


class Pesos(BaseModel):
    model_config = ConfigDict(extra="forbid")
    perfil: Annotated[float, Field(ge=0)] = 1.0
    sementes: Annotated[float, Field(ge=0)] = 1.0
    generos: Annotated[float, Field(ge=0)] = 1.0


class Receita(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nome: Annotated[str, Field(min_length=1, max_length=120)]
    tamanho: Annotated[int, Field(ge=1, le=1000)] = 50
    universo: Literal["biblioteca", "curtidas", "nao_alocadas"] | str = "biblioteca"  # ou id de playlist
    filtros: Filtros = Filtros()
    perfil_alvo: dict[str, Alvo] = {}
    generos_preferidos: dict[str, Annotated[float, Field(gt=0, le=10)]] = {}
    sementes: list[str] = []  # video_ids
    diversidade: Diversidade = Diversidade()
    pesos: Pesos = Pesos()

    @model_validator(mode="after")
    def _coerente(self):
        for nome, faixa in (("bpm", self.filtros.bpm), ("duracao_s", self.filtros.duracao_s)):
            if faixa and faixa[0] > faixa[1]:
                raise ValueError(f"filtros.{nome}: mínimo maior que o máximo")
        return self

    def canonico(self) -> str:
        return json.dumps(self.model_dump(mode="json", by_alias=True), sort_keys=True, ensure_ascii=False)

    def hash(self) -> str:
        return hashlib.sha256(self.canonico().encode()).hexdigest()[:16]


class ReceitaInvalida(ValueError):
    def __init__(self, erros: list[str]):
        super().__init__("\n".join(erros))
        self.erros = erros


def _sugerir(nome: str, opcoes: list[str]) -> str:
    perto = get_close_matches(nome, opcoes, n=3, cutoff=0.5)
    if not perto:  # tenta sem acento/caixa ("hoyo mix" → "HOYO-MiX")
        norm = {normalizar(o): o for o in opcoes}
        perto = [norm[n] for n in get_close_matches(normalizar(nome), list(norm), n=3, cutoff=0.5)]
    return f" (quis dizer: {', '.join(perto)}?)" if perto else ""


def vocabulario(con: sqlite3.Connection) -> dict[str, list[str]]:
    rotulos = [
        r[0]
        for r in con.execute(
            f"SELECT DISTINCT rotulo FROM rotulos_modelo WHERE tipo IN ({','.join('?' * len(TIPOS_PROBABILIDADE))}) ORDER BY rotulo",
            TIPOS_PROBABILIDADE,
        )
    ]
    return {
        "features": list(ESCALARES),
        "rotulos": rotulos,
        "generos_pai": sorted({r.split("---")[0] for r in rotulos if "---" in r}),
        "artistas": [r[0] for r in con.execute("SELECT DISTINCT nome FROM artistas ORDER BY nome")],
        "playlists": [
            r[0]
            for r in con.execute(
                "SELECT playlist_id FROM playlists WHERE propria = 1 AND ignorada = 0 ORDER BY playlist_id"
            )
        ],
        "titulos_playlists": [
            r[0]
            for r in con.execute(
                "SELECT titulo FROM playlists WHERE propria = 1 AND ignorada = 0 ORDER BY titulo"
            )
        ],
    }


def resolver_playlist(con: sqlite3.Connection, ref: str) -> str | None:
    """Id da playlist própria por id ou título. Ignoradas (#77) não existem para o motor."""
    linha = con.execute(
        "SELECT playlist_id FROM playlists WHERE propria = 1 AND ignorada = 0 AND (playlist_id = ? OR titulo = ?) ORDER BY playlist_id = ? DESC LIMIT 1",
        (ref, ref, ref),
    ).fetchone()
    return linha[0] if linha else None


def validar(con: sqlite3.Connection, r: Receita) -> None:
    """Confere a receita contra o que existe no banco; levanta ReceitaInvalida com todas as falhas."""
    v = vocabulario(con)
    erros = []
    artistas_norm = {normalizar(a) for a in v["artistas"]}
    for campo, nomes in (
        ("excluir_artistas", r.filtros.excluir_artistas),
        ("exigir_artistas", r.filtros.exigir_artistas),
    ):
        for a in nomes:
            if normalizar(a) not in artistas_norm:
                erros.append(
                    f"filtros.{campo}: artista '{a}' não está na biblioteca{_sugerir(a, v['artistas'])}"
                )
    for campo, refs in (
        ("excluir_playlists", r.filtros.excluir_playlists),
        ("somente_playlists", r.filtros.somente_playlists),
    ):
        for p in refs:
            if resolver_playlist(con, p) is None:
                erros.append(
                    f"filtros.{campo}: playlist '{p}' não existe{_sugerir(p, v['titulos_playlists'])}"
                )
    if (
        r.universo not in ("biblioteca", "curtidas", "nao_alocadas")
        and resolver_playlist(con, r.universo) is None
    ):
        erros.append(
            f"universo: playlist '{r.universo}' não existe{_sugerir(r.universo, v['titulos_playlists'])}"
        )
    for campo, chaves in (
        ("perfil_alvo", r.perfil_alvo),
        ("filtros.min", r.filtros.min),
        ("filtros.max", r.filtros.max),
    ):
        for f in chaves:
            if f not in ESCALARES:
                erros.append(f"{campo}: feature '{f}' não existe{_sugerir(f, list(ESCALARES))}")
    for f, alvo in r.perfil_alvo.items():
        if f != "bpm" and f in ESCALARES and not 0 <= alvo.alvo <= 1:
            erros.append(f"perfil_alvo.{f}: alvo {alvo.alvo} fora de 0..1")
    for campo, chaves in (
        ("filtros.generos_min", r.filtros.generos_min),
        ("generos_preferidos", r.generos_preferidos),
    ):
        for rot in chaves:
            if rot not in v["rotulos"]:
                erros.append(f"{campo}: rótulo '{rot}' não existe{_sugerir(rot, v['rotulos'])}")
    for pai in r.filtros.genero_pai_min:
        if pai not in v["generos_pai"]:
            erros.append(
                f"filtros.genero_pai_min: gênero '{pai}' não existe{_sugerir(pai, v['generos_pai'])}"
            )
    if r.sementes:
        existentes = {
            x[0]
            for x in con.execute(
                f"SELECT video_id FROM analises WHERE status = 'ok' AND video_id IN ({','.join('?' * len(r.sementes))})",
                r.sementes,
            )
        }
        erros += [
            f"sementes: faixa '{s}' não existe ou ainda não foi analisada"
            for s in r.sementes
            if s not in existentes
        ]
    if erros:
        raise ReceitaInvalida(erros)


def carregar(dados: dict | str) -> Receita:
    return Receita.model_validate_json(dados) if isinstance(dados, str) else Receita.model_validate(dados)
