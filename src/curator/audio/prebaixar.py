"""Baixar sem analisar (#90, experimental): adiantar o download para uma reanálise com modelos novos (#88).

O worker normal baixa e analisa na mesma passada. Para acrescentar ou trocar modelos, o que demora é o download
(cota, pausas, quarentenas), não a análise. Este modo só baixa e guarda (#84) o áudio das faixas da biblioteca que
ainda não têm arquivo guardado. A análise vem depois, sem rede, pelo provedor `guardado`.

- Faixa já analisada: baixa a mesma fonte da análise (`analises.fonte_audio`/`fonte_ref`), sem nova busca. O
  casamento já foi conferido e a reanálise usa o mesmo áudio que gerou os dados atuais.
- Faixa sem análise: só o próprio video_id no YouTube Music. As buscas em outros provedores dependem da conferência
  de duração que a análise faz, então ficam para o worker normal.
- As regras de rede são as do worker: uma thread por saída, cota diária e quarentena por saída, pausa entre
  downloads. A trava também é a mesma (`worker.lock`), então o lote diário do timer não roda junto.
"""

import logging
import sqlite3
import tempfile
import threading
import time
from collections import Counter, deque
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from curator.audio import guardados, proxies
from curator.audio.provedores.base import Candidato, ErroBloqueio, ErroIndisponivel, FaixaAlvo, ProvedorAudio
from curator.audio.worker import (
    ESPERA_MAXIMA,
    ESPERA_TRAVA_S,
    Compartilhado,
    Progresso,
    _resumir,
    _travado,
    carregar_faixa,
    pausa_da_saida,
)
from curator.banco.consultas import BIBLIOTECA

log = logging.getLogger(__name__)

MAX_TENTATIVAS = 3  # erros transitórios por faixa nesta rodada; a próxima rodada tenta de novo


def pendentes(con: sqlite3.Connection, primeiro: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Faixas da biblioteca sem áudio guardado, na ordem da fila (curtidas mais recentes primeiro).

    `primeiro`: video_ids que passam na frente, na ordem dada (ex.: as faixas de uma avaliação). Ficam de fora as
    que nenhum provedor teve (`sem-audio`) e as analisadas de um arquivo local (já estão no disco)."""
    ids = [
        r[0]
        for r in con.execute(
            f"""SELECT b.video_id FROM ({BIBLIOTECA}) b JOIN faixas f USING (video_id)
                LEFT JOIN analises a USING (video_id)
                WHERE NOT EXISTS (SELECT 1 FROM audios_guardados g WHERE g.video_id = b.video_id)
                  AND coalesce(a.status, '') != 'sem-audio' AND coalesce(a.fonte_audio, '') != 'pasta_local'
                ORDER BY f.posicao_curtida IS NULL, f.posicao_curtida, b.video_id"""
        )
    ]
    na_frente = [v for v in dict.fromkeys(primeiro) if v in set(ids)]
    vistos = set(na_frente)
    return na_frente + [v for v in ids if v not in vistos]


def escolher_fonte(
    con: sqlite3.Connection, faixa: FaixaAlvo, provedores: dict[str, ProvedorAudio]
) -> tuple[str, Candidato] | None:
    """(provedor, candidato): a fonte da análise atual ou, sem análise, o próprio video_id no YouTube Music."""
    a = con.execute(
        "SELECT status, fonte_audio, fonte_ref, score_casamento FROM analises WHERE video_id = ?",
        (faixa.video_id,),
    ).fetchone()
    if a and a["status"] == "ok" and a["fonte_audio"] in provedores and a["fonte_ref"]:
        nome = a["fonte_audio"]
        exato = nome == "youtube_music"
        return nome, Candidato(
            nome, a["fonte_ref"], faixa.titulo, None, faixa.duracao_s, a["score_casamento"] or 1.0, exato
        )
    if "youtube_music" in provedores:
        return "youtube_music", provedores["youtube_music"].buscar(faixa, None)[0]
    return None


class Trabalho:
    """A lista de faixas que as threads dividem. Bloqueio devolve a faixa à frente; erro, para o fim."""

    def __init__(self, ids: list[str]):
        self._fila: deque[str] = deque(ids)
        self._tentativas: Counter = Counter()
        self._trava = threading.Lock()

    def pegar(self) -> str | None:
        with self._trava:
            return self._fila.popleft() if self._fila else None

    def devolver(self, vid: str) -> None:
        with self._trava:
            self._fila.appendleft(vid)

    def adiar(self, vid: str) -> bool:
        """Para o fim da fila; False quando a faixa esgotou as tentativas desta rodada."""
        with self._trava:
            self._tentativas[vid] += 1
            if self._tentativas[vid] >= MAX_TENTATIVAS:
                return False
            self._fila.append(vid)
            return True

    def __len__(self) -> int:
        with self._trava:
            return len(self._fila)


class Baixador:
    """Uma saída de rede: pega a próxima faixa, baixa, guarda e espera a pausa da saída."""

    def __init__(
        self,
        con: sqlite3.Connection,
        cfg: dict,
        provedores: dict[str, ProvedorAudio],
        saida: str,
        trabalho: Trabalho,
        compartilhado: Compartilhado,
        relogio: Callable[[], datetime] = lambda: datetime.now(UTC),
        dormir: Callable[[float], None] | None = None,
        rng=None,
    ):
        import random

        self.con, self.cfg, self.saida, self.trabalho, self.comp = con, cfg, saida, trabalho, compartilhado
        self.provedores = {n: p for n, p in provedores.items() if p.usa_rede}
        self.relogio, self.rng = relogio, rng or random.Random()
        self.dormir = dormir or self.comp.evento_parar.wait
        self.pasta = guardados.pasta(cfg)

    def rodar(self, aguardar: bool = False, ao_baixar: Callable[[Progresso], None] | None = None) -> Counter:
        cont: Counter = Counter()
        while not self.comp.evento_parar.is_set() and len(self.trabalho):
            saida = proxies.escolher(self.con, self.relogio(), self.saida)
            if saida is None:
                if aguardar and self._aguardar_liberacao():
                    continue
                cont["parada: sem cota ou em quarentena"] += 1
                break
            vid = self.trabalho.pegar()
            if vid is None:
                break
            vaga = self.comp.reservar_vaga()
            if vaga is None:
                self.trabalho.devolver(vid)
                break
            inicio = time.perf_counter()
            try:
                resultado = self._uma(vid, saida)
            except sqlite3.OperationalError as e:
                if not _travado(e):
                    raise
                self.trabalho.devolver(vid)
                self.comp.devolver_vaga()
                log.warning("saída %s: banco travado (%s); retomando em %d s", self.saida, e, ESPERA_TRAVA_S)
                self.dormir(ESPERA_TRAVA_S)
                continue
            if resultado in ("bloqueio", "sem fonte"):
                self.comp.devolver_vaga()
            cont[resultado] += 1
            if ao_baixar:
                f = carregar_faixa(self.con, vid)
                n = vaga if resultado not in ("bloqueio", "sem fonte") else self.comp.processadas
                ao_baixar(
                    Progresso(n, self.comp.limite, f.titulo, f.artistas, resultado,
                              time.perf_counter() - inicio, self.saida)
                )  # fmt: skip
            restam = self.comp.limite is None or self.comp.processadas < self.comp.limite
            if (
                resultado != "sem fonte"
                and not self.comp.evento_parar.is_set()
                and restam
                and len(self.trabalho)
            ):
                self.dormir(pausa_da_saida(self.cfg, self.saida, self.rng))
        return cont

    def _uma(self, vid: str, saida) -> str:
        faixa = carregar_faixa(self.con, vid)
        escolha = escolher_fonte(self.con, faixa, self.provedores)
        if escolha is None:
            return "sem fonte"
        nome, c = escolha
        try:
            with tempfile.TemporaryDirectory(prefix="curator-") as tmp:
                arquivo = self.provedores[nome].baixar(c, Path(tmp), saida)
                proxies.registrar_uso(self.con, saida.nome, self.relogio())
                if not arquivo.bytes:
                    raise RuntimeError("arquivo vazio")
                guardados.guardar(self.con, self.pasta, vid, arquivo, c)
        except ErroBloqueio as e:
            fim = proxies.quarentenar(self.con, saida.nome, self.cfg, self.relogio())
            log.warning("saída %s bloqueada (%s); quarentena até %s", saida.nome, _resumir(e), fim)
            self.trabalho.devolver(vid)
            return "bloqueio"
        except ErroIndisponivel as e:
            log.info(
                "%s: %s não tem mais a faixa (%s); fica para o worker normal", faixa.titulo, nome, _resumir(e)
            )
            return "indisponivel"
        except Exception as e:
            if isinstance(e, sqlite3.OperationalError) and _travado(e):
                raise
            log.warning("%s: erro em %s (%s)", faixa.titulo, nome, _resumir(e))
            proxies.registrar_falha(self.con, saida.nome, self.cfg, self.relogio())
            return "erro" if self.trabalho.adiar(vid) else "erro (desistiu nesta rodada)"
        proxies.registrar_sucesso(self.con, saida.nome)
        return "guardado"

    def _aguardar_liberacao(self) -> bool:
        agora = self.relogio()
        libera = proxies.proxima_liberacao(self.con, agora, self.saida)
        if libera is None or libera - agora > ESPERA_MAXIMA or not len(self.trabalho):
            return False
        log.warning("saída %s indisponível; aguardando até %s", self.saida, libera.astimezone())
        self.dormir(max((libera - agora).total_seconds(), 1) + self.rng.uniform(5, 60))
        return not self.comp.evento_parar.is_set()


def baixar_em_paralelo(
    caminho_banco,
    cfg: dict,
    ids: list[str],
    montar_provedores: Callable,
    compartilhado: Compartilhado,
    aguardar: bool = False,
    ao_baixar: Callable[[Progresso], None] | None = None,
) -> Counter:
    """Uma thread por saída ativa (direta + [[proxy]]), dividindo a lista de faixas, o limite e a parada."""
    from curator.banco.conexao import abrir

    con = abrir(caminho_banco)
    proxies.sincronizar_config(con, cfg)
    saidas = [s["nome"] for s in proxies.estado(con, datetime.now(UTC))]
    trabalho = Trabalho(ids)
    total: Counter = Counter()
    trava = threading.Lock()

    def tarefa(nome: str) -> None:
        c = abrir(caminho_banco)
        try:
            r = Baixador(c, cfg, montar_provedores(cfg, c), nome, trabalho, compartilhado).rodar(
                aguardar, ao_baixar
            )
        except Exception:
            log.exception("download da saída %s parou com erro", nome)
            r = Counter({f"erro na saída {nome}": 1})
        with trava:
            total.update(r)

    threads = [threading.Thread(target=tarefa, args=(n,), name=f"baixar-{n}") for n in saidas]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    total["restantes"] = len(trabalho)
    return total
