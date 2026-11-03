"""Worker da fila: escolhe saída → busca no provedor → baixa → analisa → grava → apaga o áudio (ADR-08).

Decisões por tipo de erro:
- ErroBloqueio: a SAÍDA vai para quarentena; a faixa volta a pendente sem gastar tentativa.
- ErroIndisponivel / áudio inválido / nenhum candidato confiável: tenta o próximo provedor.
- Qualquer outro erro: a faixa volta mais tarde (backoff); a saída acumula falha.
Esgotados os provedores, a faixa fica `sem-audio`.

Modo paralelo (`rodar_em_paralelo`): um worker por saída ativa, cada um numa thread com a própria conexão
ao banco e o próprio ritmo de pausas. Os downloads correm em paralelo; a análise do Essentia é serializada
por uma trava (uma cópia só dos modelos na memória, ~2 GB).
"""

import json
import logging
import random
import signal
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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

ESPERA_MAXIMA = timedelta(hours=26)  # no modo aguardar, nunca dorme mais que isso de uma vez


class ErroAnalise(RuntimeError):
    """O download funcionou, mas a análise falhou: a culpa não é da saída de rede."""


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
    saida: str | None = None

    def linha(self) -> str:
        total = self.limite if self.limite is not None else "∞"
        quem = f"{self.titulo} — {', '.join(self.artistas[:2])}" if self.artistas else self.titulo
        por = f" ({self.saida})" if self.saida else ""
        return f"[{self.n}/{total}]{por} {quem} · {self.resultado.replace('_', ' ')} em {self.segundos:.0f} s"


class Compartilhado:
    """O que os workers de um mesmo lote dividem: o limite de faixas, o pedido de parada e a numeração."""

    def __init__(self, limite: int | None = None):
        self.limite = limite
        self.processadas = 0
        self.evento_parar = threading.Event()
        self._trava = threading.Lock()

    def reservar_vaga(self) -> int | None:
        with self._trava:
            if self.limite is not None and self.processadas >= self.limite:
                return None
            self.processadas += 1
            return self.processadas

    def devolver_vaga(self) -> None:
        with self._trava:
            self.processadas -= 1


class AnalisadorTravado:
    """Um analisador só para todas as threads: o Essentia/TensorFlow roda uma faixa por vez."""

    def __init__(self, analisador):
        self._a, self._trava = analisador, threading.Lock()
        self.hash_modelos = analisador.hash_modelos

    def analisar(self, caminho):
        with self._trava:
            return self._a.analisar(caminho)


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
        dormir: Callable[[float], None] | None = None,
        rng: random.Random | None = None,
        saida: str | None = None,
        compartilhado: Compartilhado | None = None,
    ):
        self.con, self.cfg, self.analisador, self.provedores = con, cfg, analisador, provedores
        self.comp = compartilhado or Compartilhado()
        # a pausa padrão acorda na hora se pedirem parada (em vez de terminar um sleep de 30 s)
        self.relogio, self.dormir, self.rng = (
            relogio,
            dormir or self.comp.evento_parar.wait,
            rng or random.Random(),
        )
        self.saida = saida
        self.limiar = cfg["provedores"]["limiar_casamento"]
        self.tolerancia = cfg["provedores"]["tolerancia_duracao_s"]
        self.max_tentativas = cfg["fila"]["max_tentativas"]

    @property
    def parar(self) -> bool:
        return self.comp.evento_parar.is_set()

    @parar.setter
    def parar(self, valor: bool) -> None:
        if valor:
            self.comp.evento_parar.set()

    def instalar_sinais(self) -> None:
        instalar_sinais(self.comp)

    def rodar(
        self,
        limite: int | None = None,
        ate: datetime | None = None,
        ao_processar: Callable[[Progresso], None] | None = None,
        preparar: bool = True,
        aguardar: bool = False,
    ) -> Relatorio:
        """`ao_processar` recebe um `Progresso` depois de cada faixa (a CLI imprime uma linha por faixa).

        `preparar=False` no modo paralelo: o lote já sincronizou as saídas e recuperou a fila uma vez só
        (recuperar em cada thread devolveria à fila o item que outra thread está baixando)."""
        rel = Relatorio()
        if limite is not None:
            self.comp.limite = limite
        if preparar:
            proxies.sincronizar_config(self.con, self.cfg)
            if n := fila.recuperar(self.con):
                log.info("%d item(ns) interrompido(s) voltaram para a fila", n)
        while True:
            if self.parar:
                rel.motivo_parada = "interrompido"
                break
            if ate and self.relogio() >= ate:
                rel.motivo_parada = "horário-limite"
                break
            vaga = self.comp.reservar_vaga()
            if vaga is None:
                rel.motivo_parada = f"limite de {self.comp.limite} faixa(s)"
                break
            item = fila.reservar(self.con, self.relogio(), self.max_tentativas)
            if item is None:
                self.comp.devolver_vaga()
                break
            inicio = time.perf_counter()
            resultado, usou_rede = self._processar(item)
            if resultado in ("sem_saida", "bloqueio"):
                self.comp.devolver_vaga()  # não conta para o limite
            if resultado == "sem_saida" and aguardar and self._aguardar_liberacao(ate):
                continue
            if resultado == "sem_saida":
                rel.motivo_parada = (
                    "todas as saídas em quarentena ou sem cota"
                    if self.saida is None
                    else f"saída {self.saida} em quarentena ou sem cota"
                )
                rel.proxima_liberacao = proxies.proxima_liberacao(self.con, self.relogio())
                break
            rel.contagem[resultado] += 1
            segundos = time.perf_counter() - inicio
            if resultado == "ok":
                rel.segundos_por_faixa.append(segundos)
            if ao_processar:
                f = carregar_faixa(self.con, item["video_id"])
                n = vaga if resultado != "bloqueio" else self.comp.processadas
                ao_processar(
                    Progresso(n, self.comp.limite, f.titulo, f.artistas, resultado, segundos, self.saida)
                )
            restam = self.comp.limite is None or self.comp.processadas < self.comp.limite
            if usou_rede and not self.parar and restam:
                f = self.cfg["fila"]
                self.dormir(self.rng.uniform(f["intervalo_min_s"], f["intervalo_max_s"]))
        return rel

    def _aguardar_liberacao(self, ate: datetime | None) -> bool:
        """Modo serviço: dorme até a saída sair da quarentena ou a cota virar. False = desistir (parada/limite)."""
        agora = self.relogio()
        libera = proxies.proxima_liberacao(self.con, agora, self.saida)
        if libera is None or libera - agora > ESPERA_MAXIMA or (ate and libera >= ate):
            return False
        segundos = max((libera - agora).total_seconds(), 1) + self.rng.uniform(5, 60)
        log.warning("saída %s indisponível; aguardando até %s", self.saida or "(todas)", libera.astimezone())
        self.dormir(segundos)
        return not self.parar

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
                saida = proxies.escolher(self.con, self.relogio(), self.saida)
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
                if saida and isinstance(e, ErroAnalise):
                    proxies.registrar_sucesso(self.con, saida.nome)  # o download deu certo
                elif saida:
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
            try:
                r = self.analisador.analisar(arquivo.caminho)
            except AudioInvalido:
                raise
            except Exception as e:
                raise ErroAnalise(f"análise falhou: {e}") from e
            # o diretório temporário (e o áudio) somem ao sair do `with`, mesmo com exceção
        # um download parcial não tem a duração da faixa; o casamento já conferiu a duração do candidato
        if (
            not melhor.exato
            and not arquivo.parcial
            and faixa.duracao_s
            and abs(r.duracao_s - faixa.duracao_s) > 3 * self.tolerancia
        ):
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


# ------------------------------------------------------------------ vários workers, um por saída


def instalar_sinais(comp: Compartilhado) -> None:
    def pedir_parada(signum, _frame):
        log.warning("sinal %s recebido: termino as faixas em andamento e paro", signum)
        comp.evento_parar.set()

    signal.signal(signal.SIGTERM, pedir_parada)
    signal.signal(signal.SIGINT, pedir_parada)


_PRIORIDADE = ("interrompido", "horário-limite", "limite", "fila vazia")


def _juntar(relatorios: list[Relatorio]) -> Relatorio:
    out = Relatorio()
    for r in relatorios:
        out.contagem.update(r.contagem)
        out.segundos_por_faixa += r.segundos_por_faixa
    motivos = [r.motivo_parada for r in relatorios]
    out.motivo_parada = next(
        (m for p in _PRIORIDADE for m in motivos if m.startswith(p)),
        "todas as saídas em quarentena ou sem cota",
    )
    liberacoes = [r.proxima_liberacao for r in relatorios if r.proxima_liberacao]
    out.proxima_liberacao = min(liberacoes) if liberacoes and out.motivo_parada.startswith("todas") else None
    return out


def rodar_em_paralelo(
    caminho_banco,
    cfg: dict,
    analisador,
    montar_provedores: Callable,
    limite: int | None = None,
    ate: datetime | None = None,
    ao_processar: Callable[[Progresso], None] | None = None,
    compartilhado: Compartilhado | None = None,
    aguardar: bool = False,
    **kw,
) -> Relatorio:
    """Um worker por saída ativa (direta + proxies do config), em threads, dividindo limite e parada."""
    from curator.banco.conexao import abrir

    con = abrir(caminho_banco)
    proxies.sincronizar_config(con, cfg)
    if n := fila.recuperar(con):
        log.info("%d item(ns) interrompido(s) voltaram para a fila", n)
    saidas = [s["nome"] for s in proxies.estado(con, datetime.now(UTC))]
    comp = compartilhado or Compartilhado(limite)
    comp.limite = limite
    an = AnalisadorTravado(analisador)
    relatorios: list[Relatorio] = []
    trava = threading.Lock()

    def tarefa(nome: str) -> None:
        c = abrir(caminho_banco)
        try:
            w = Worker(c, cfg, an, montar_provedores(cfg, c), saida=nome, compartilhado=comp, **kw)
            r = w.rodar(ate=ate, ao_processar=ao_processar, preparar=False, aguardar=aguardar)
        except Exception:
            log.exception("worker da saída %s parou com erro", nome)
            r = Relatorio(motivo_parada=f"erro na saída {nome}")
        with trava:
            relatorios.append(r)

    threads = [threading.Thread(target=tarefa, args=(n,), name=f"saida-{n}") for n in saidas]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return _juntar(relatorios)
