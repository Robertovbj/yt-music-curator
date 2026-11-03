"""Analisador e provedores falsos para testar a fila e o worker sem rede nem Essentia."""

import numpy as np

from curator.audio.analisador import AudioInvalido, Resultado
from curator.audio.provedores.base import Arquivo, Candidato, ErroBloqueio, ErroIndisponivel, ErroTransitorio


def resultado_falso(duracao=200.0) -> Resultado:
    return Resultado(
        {"bpm": 120.0, "energy": 0.5}, {"effnet_emb": np.ones(1280, np.float32)}, {}, duracao, {"x": 0.1}
    )


class AnalisadorFalso:
    hash_modelos = "hash-teste"

    def __init__(self, duracao=200.0, invalido=False):
        self.duracao, self.invalido, self.analisados = duracao, invalido, []

    def analisar(self, caminho):
        assert caminho.exists(), "o áudio precisa existir durante a análise"
        self.analisados.append(caminho)
        if self.invalido:
            raise AudioInvalido("curto")
        return resultado_falso(self.duracao)


class ProvedorFalso:
    """`comportamento`: lista consumida a cada busca — 'ok', 'nada', 'bloqueio', 'indisponivel', 'erro'."""

    def __init__(self, nome, comportamento, usa_rede=True, score=1.0, exato=True, duracao=200):
        self.nome, self.usa_rede = nome, usa_rede
        self.comportamento = list(comportamento)
        self.score, self.exato, self.duracao = score, exato, duracao
        self.saidas_usadas, self.arquivos = [], []

    def buscar(self, faixa, saida):
        self.saidas_usadas.append(saida.nome if saida else None)
        acao = self.comportamento.pop(0) if self.comportamento else "ok"
        if acao == "bloqueio":
            raise ErroBloqueio("Sign in to confirm you're not a bot")
        if acao == "indisponivel":
            raise ErroIndisponivel("Video unavailable")
        if acao == "erro":
            raise ErroTransitorio("timed out")
        if acao == "nada":
            return []
        return [
            Candidato(
                self.nome,
                f"{self.nome}:{faixa.video_id}",
                faixa.titulo,
                None,
                self.duracao,
                self.score,
                self.exato,
            )
        ]

    def baixar(self, candidato, destino, saida):
        arq = destino / "audio.opus"
        arq.write_bytes(b"\0" * 1000)
        self.arquivos.append(arq)
        return Arquivo(arq, 1000)
