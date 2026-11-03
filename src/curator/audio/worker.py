"""Worker da fila: escolhe saída → busca no provedor → baixa → analisa → grava → apaga o áudio (ADR-08).

Decisões por tipo de erro:
- ErroBloqueio: a SAÍDA vai para quarentena; a faixa volta a pendente sem gastar tentativa.
- ErroIndisponivel / áudio inválido / nenhum candidato confiável: tenta o próximo provedor.
- Qualquer outro erro: a faixa volta mais tarde (backoff); a saída acumula falha.
Esgotados os provedores, a faixa fica `sem-audio`.
"""

import json
import logging
import random
import signal
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from curator.audio import fila, proxies
from curator.audio.analisador import VERSAO_ANALISADOR, AudioInvalido
from curator.audio.persistir import Origem, gravar_analise, gravar_falha
from curator.audio.provedores.base import (
    Candidato,
    ErroBloqueio,
    ErroIndisponivel,
    FaixaAlvo,
    ProvedorAudio,
    Saida,
)

log = logging.getLogger(__name__)


@dataclass
class Relatorio:
    contagem: Counter = field(default_factory=Counter)
    motivo_parada: str = "fila vazia"
    proxima_liberacao: datetime | None = None
    segundos_por_faixa: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class Progresso:
    """Uma faixa processada. `n` conta as faixas que valem para o limite (bloqueios não contam)."""

    n: int
    limite: int | None
    titulo: str
    artistas: list[str]
    resultado: str  # ok | sem_audio | erro | bloqueio
    segundos: float

    def linha(self) -> str:
        total = self.limite if self.limite is not None else "∞"
        quem = f"{self.titulo} — {', '.join(self.artistas[:2])}" if self.artistas else self.titulo
        return f"[{self.n}/{total}] {quem} · {self.resultado.replace('_', ' ')} em {self.segundos:.0f} s"


def carregar_faixa(con, video_id: str) -> FaixaAlvo:
    r = con.execute(
        "SELECT video_id, titulo, artistas, duracao_s, album_nome FROM faixas WHERE video_id = ?", (video_id,)
    ).fetchone()
    return FaixaAlvo(
        r["video_id"],
        r["titulo"],
        [a["nome"] for a in json.loads(r["artistas"])],
        r["duracao_s"],
        r["album_nome"],
    )


class Worker:
    def __init__(
        self,
        con,
        cfg: dict,
        analisador,
        provedores: dict[str, ProvedorAudio],
        relogio: Callable[[], datetime] = lambda: datetime.now(UTC),
        dormir: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ):
        self.con, self.cfg, self.analisador, self.provedores = con, cfg, analisador, provedores
        self.relogio, self.dormir, self.rng = relogio, dormir, rng or random.Random()
        self.parar = False
        self.limiar = cfg["provedores"]["limiar_casamento"]
        self.tolerancia = cfg["provedores"]["tolerancia_duracao_s"]
        self.max_tentativas = cfg["fila"]["max_tentativas"]

    def instalar_sinais(self) -> None:
        def pedir_parada(signum, _frame):
            log.warning("sinal %s recebido: termino a faixa atual e paro", signum)
            self.parar = True

        signal.signal(signal.SIGTERM, pedir_parada)
        signal.signal(signal.SIGINT, pedir_parada)

    def rodar(
        self,
        limite: int | None = None,
        ate: datetime | None = None,
        ao_processar: Callable[[Progresso], None] | None = None,
    ) -> Relatorio:
        """`ao_processar` recebe um `Progresso` depois de cada faixa (a CLI imprime uma linha por faixa)."""
        rel = Relatorio()
        proxies.sincronizar_config(self.con, self.cfg)
        if n := fila.recuperar(self.con):
            log.info("%d item(ns) interrompido(s) voltaram para a fila", n)
        processadas = 0
        while True:
            if self.parar:
                rel.motivo_parada = "interrompido"
                break
            if limite is not None and processadas >= limite:
                rel.motivo_parada = f"limite de {limite} faixa(s)"
                break
            if ate and self.relogio() >= ate:
                rel.motivo_parada = "horário-limite"
                break
            item = fila.proximo(self.con, self.relogio(), self.max_tentativas)
            if item is None:
                break
            inicio = time.perf_counter()
            resultado, usou_rede = self._processar(item)
            if resultado == "sem_saida":
                rel.motivo_parada = "todas as saídas em quarentena ou sem cota"
                rel.proxima_liberacao = proxies.proxima_liberacao(self.con, self.relogio())
                break
            rel.contagem[resultado] += 1
            if resultado != "bloqueio":
                processadas += 1
            segundos = time.perf_counter() - inicio
            if resultado == "ok":
                rel.segundos_por_faixa.append(segundos)
            if ao_processar:
                f = carregar_faixa(self.con, item["video_id"])
                ao_processar(Progresso(processadas, limite, f.titulo, f.artistas, resultado, segundos))
            if usou_rede and not self.parar and (limite is None or processadas < limite):
                f = self.cfg["fila"]
                self.dormir(self.rng.uniform(f["intervalo_min_s"], f["intervalo_max_s"]))
        return rel

    # ------------------------------------------------------------------ uma faixa

    def _processar(self, item) -> tuple[str, bool]:
        vid = item["video_id"]
        faixa = carregar_faixa(self.con, vid)
        tentados: list[str] = json.loads(item["provedores_tentados"])
        usou_rede = False
        for nome, prov in self.provedores.items():
            if nome in tentados:
                continue
            saida: Saida | None = None
            if prov.usa_rede:
                saida = proxies.escolher(self.con, self.relogio())
                if saida is None:
                    fila.marcar(self.con, vid, "pendente", provedores_tentados=tentados)
                    return "sem_saida", usou_rede
                usou_rede = True
            try:
                status = self._tentar(faixa, nome, prov, saida)
            except ErroBloqueio as e:
                fim = proxies.quarentenar(self.con, saida.nome, self.cfg, self.relogio())
                log.warning("saída %s bloqueada (%s); quarentena até %s", saida.nome, _resumir(e), fim)
                fila.marcar(self.con, vid, "pendente", provedores_tentados=tentados)
                return "bloqueio", usou_rede
            except (ErroIndisponivel, AudioInvalido) as e:
                log.info("%s: %s não tem a faixa (%s)", faixa.titulo, nome, _resumir(e))
                status = None
            except Exception as e:
                log.warning("%s: erro em %s (%s); nova tentativa mais tarde", faixa.titulo, nome, _resumir(e))
                if saida:
                    proxies.registrar_falha(self.con, saida.nome, self.cfg, self.relogio())
                fila.falhar(self.con, vid, type(e).__name__, str(e), self.cfg, self.relogio(), self.rng)
                fila.marcar(self.con, vid, "erro", provedores_tentados=tentados)
                return "erro", usou_rede
            if status == "ok":
                if saida:
                    proxies.registrar_sucesso(self.con, saida.nome)
                fila.marcar(
                    self.con,
                    vid,
                    "ok",
                    provedor=nome,
                    saida=saida.nome if saida else None,
                    provedores_tentados=tentados,
                )
                return "ok", usou_rede
            tentados.append(nome)
            fila.marcar(self.con, vid, "pendente", provedores_tentados=tentados)

        gravar_falha(self.con, vid, "sem-audio", f"nenhum provedor teve a faixa ({', '.join(tentados)})")
        fila.marcar(self.con, vid, "sem-audio", provedores_tentados=tentados)
        log.info("%s: sem áudio em nenhum provedor", faixa.titulo)
        return "sem_audio", usou_rede

    def _tentar(self, faixa: FaixaAlvo, nome: str, prov: ProvedorAudio, saida: Saida | None) -> str | None:
        candidatos = [c for c in prov.buscar(faixa, saida) if c.score >= self.limiar]
        if not candidatos:
            return None
        melhor: Candidato = candidatos[0]
        fila.marcar(self.con, faixa.video_id, "baixando", provedor=nome, saida=saida.nome if saida else None)
        inicio = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="curator-") as tmp:
            arquivo = prov.baixar(melhor, Path(tmp), saida)
            if saida:
                proxies.registrar_uso(self.con, saida.nome, self.relogio())
            fila.marcar(self.con, faixa.video_id, "analisando")
            r = self.analisador.analisar(arquivo.caminho)
            # o diretório temporário (e o áudio) somem ao sair do `with`, mesmo com exceção
        if not melhor.exato and faixa.duracao_s and abs(r.duracao_s - faixa.duracao_s) > 3 * self.tolerancia:
            raise ErroIndisponivel(
                f"duração baixada {r.duracao_s:.0f}s ≠ esperada {faixa.duracao_s}s; candidato descartado"
            )
        gravar_analise(
            self.con, faixa.video_id, r,
            Origem(nome, melhor.ref, melhor.score, arquivo.bytes or None),
            VERSAO_ANALISADOR, self.analisador.hash_modelos, round(time.perf_counter() - inicio, 2),
        )  # fmt: skip
        return "ok"


def _resumir(e: Exception) -> str:
    return str(e).splitlines()[0][:160] if str(e) else type(e).__name__
