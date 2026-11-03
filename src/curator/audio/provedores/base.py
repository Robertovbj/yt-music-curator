"""Contrato dos provedores de áudio e a classificação de erros que a fila usa para decidir o que fazer."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class FaixaAlvo:
    """A faixa da biblioteca que queremos analisar."""

    video_id: str
    titulo: str
    artistas: list[str] = field(default_factory=list)
    duracao_s: int | None = None
    album: str | None = None


@dataclass(frozen=True)
class Candidato:
    provedor: str
    ref: str  # URL ou caminho
    titulo: str
    artista: str | None = None
    duracao_s: float | None = None
    score: float = 0.0
    exato: bool = False  # o próprio video_id da curtida: não precisa conferir duração


@dataclass(frozen=True)
class Saida:
    """Saída de rede para um download (ADR-08.3). `url=None` = conexão direta."""

    nome: str
    url: str | None = None


@dataclass(frozen=True)
class Arquivo:
    caminho: Path
    bytes: int
    temporario: bool = True  # False para a pasta local: o worker nunca apaga
    parcial: bool = False  # só o começo da faixa (faixas longas): a duração do arquivo não é a da faixa


class ErroBloqueio(RuntimeError):
    """O serviço recusou por volume/robô: a SAÍDA vai para quarentena; a faixa não tem culpa."""


class ErroIndisponivel(RuntimeError):
    """Este provedor não tem a faixa (removida, privada, região, idade): tentar o próximo provedor."""


class ErroTransitorio(RuntimeError):
    """Rede, timeout, 403 avulso: tentar a mesma faixa mais tarde, com backoff."""


class ProvedorAudio(Protocol):
    nome: str
    usa_rede: bool

    def buscar(self, faixa: FaixaAlvo, saida: Saida | None) -> list[Candidato]: ...

    def baixar(self, candidato: Candidato, destino: Path, saida: Saida | None) -> Arquivo: ...
