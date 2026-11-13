"""Análises extras (#88): modelos que rodam à parte do núcleo do analisador, cada grupo com a própria versão.

O núcleo (EffNet e cabeças, MusiCNN, tempo e tom) já foi calculado para a biblioteca inteira, e o `hash_modelos`
dele reabre a fila toda quando muda. Um extra novo só precisa rodar o próprio modelo. Com os áudios guardados
(#84/#90), o `curator analisar` faz isso sozinho depois da fila, sem rede (#92). Faixas novas recebem os extras na
mesma passada do núcleo, sem decodificar o áudio de novo.

Por que estes três (pesquisa da #83, ADR-14): a fusão Discogs + Jamendo ganhou pouco porque as duas cabeças usam o
mesmo embedding, e ensembles ganham com diversidade (Kuncheva & Whitaker, 2003).

| extra    | modelo e treino                                     | tipos em `vetores`                             | custo (*) |
|----------|-----------------------------------------------------|------------------------------------------------|-----------|
| msd_tags | MSD-MusiCNN (o do núcleo), saída sigmoide; 50 tags  | `msd_tags` (média dos trechos)                 | ~3 s   |
|          | do Last.fm no Million Song Dataset                  |                                                |        |
| yamnet   | YAMNet, 521 classes do AudioSet (vídeos do YouTube, | `yamnet` (média), `yamnet_p90` (percentil 90   | ~1 s   |
|          | Gemmeke et al., 2017): "Rapping", "Singing" etc.    | dos quadros de ~1 s), `yamnet_emb` (1024-d)    |        |
| maest    | MAEST 30 s (Alonso-Jiménez et al., ISMIR 2023),     | `maest519` (média dos trechos de 30 s) e       | ~14 s  |
|          | transformer com 519 estilos do Discogs              | `maest519_max` (maior trecho)                  |        |

(*) Faixa de ~4 min numa CPU de 8 núcleos. O MAEST fica só com as previsões: o embedding dele custaria outra
passada inteira (~9 s); com o áudio guardado, dá para calcular depois.
"""

import hashlib
import json
import logging
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from curator.audio import modelos
from curator.banco import vetores
from curator.banco.conexao import agora, transacao

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Extra:
    nome: str
    versao: str  # mudar quando o CÁLCULO mudar; trocar o arquivo do modelo já muda o hash pelo SHA-256
    modelos: tuple[str, ...]  # nomes no manifesto (modelos.toml)
    tipos: tuple[str, ...]  # tipos gravados em `vetores`


EXTRAS: dict[str, Extra] = {
    "msd_tags": Extra("msd_tags", "1", ("msd-musicnn-1",), ("msd_tags",)),
    "yamnet": Extra("yamnet", "1", ("audioset-yamnet-1",), ("yamnet", "yamnet_p90", "yamnet_emb")),
    "maest": Extra("maest", "1", ("discogs-maest-30s-pw-519l-2",), ("maest519", "maest519_max")),
}
TIPOS_EXTRAS = tuple(t for e in EXTRAS.values() for t in e.tipos)


def hash_extra(nome: str) -> str:
    """Versão do cálculo + SHA-256 dos arquivos dos modelos do extra."""
    e = EXTRAS[nome]
    por_nome = {m.nome: m for m in modelos.manifesto()[1]}
    h = hashlib.sha256(f"{e.nome}:{e.versao}\n".encode())
    for n in e.modelos:
        m = por_nome[n]
        h.update(f"{m.nome}:{m.pb_sha256}:{m.json_sha256}\n".encode())
    return h.hexdigest()[:16]


def validar(nomes) -> list[str]:
    nomes = list(dict.fromkeys(nomes))
    if desconhecidos := [n for n in nomes if n not in EXTRAS]:
        raise ValueError(
            f"extra desconhecido em [analise] extras: {', '.join(desconhecidos)} (há {', '.join(EXTRAS)})"
        )
    return nomes


@dataclass
class ResultadoExtra:
    hash: str
    vetores: dict[str, np.ndarray]
    rotulos: dict[str, list[str]]
    tempo_s: float
    erro: str | None = None


class AnalisadorExtras:
    """Carrega os modelos dos extras pedidos uma vez. `analisar_audio` recebe o áudio mono a 16 kHz."""

    def __init__(self, pasta_modelos: Path, nomes, verificar_checksums: bool = True):
        self.nomes = validar(nomes)
        arquivos = {m for n in self.nomes for m in EXTRAS[n].modelos}
        if verificar_checksums and (problemas := modelos.verificar(pasta_modelos, nomes=arquivos)):
            raise RuntimeError(
                "modelos dos extras ausentes ou corrompidos: " + "; ".join(problemas[:5])
                + " — rode `curator modelos baixar`"
            )  # fmt: skip
        os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
        import essentia
        import essentia.standard as es

        essentia.log.infoActive = False
        essentia.log.warningActive = False
        self.es = es
        self.hashes = {n: hash_extra(n) for n in self.nomes}
        mp = lambda nome: str(pasta_modelos / f"{nome}.pb")  # noqa: E731
        self.rotulos = {
            m: json.loads((pasta_modelos / f"{m}.json").read_text(encoding="utf-8"))["classes"]
            for m in arquivos
        }
        if "msd_tags" in self.nomes:
            self.msd = es.TensorflowPredictMusiCNN(graphFilename=mp("msd-musicnn-1"), output="model/Sigmoid")
        if "yamnet" in self.nomes:
            yam = mp("audioset-yamnet-1")
            self.yam_act = es.TensorflowPredictVGGish(
                graphFilename=yam, input="melspectrogram", output="activations"
            )
            self.yam_emb = es.TensorflowPredictVGGish(
                graphFilename=yam, input="melspectrogram", output="embeddings"
            )
        if "maest" in self.nomes:
            # saída sigmoide (as previsões); "PartitionedCall/Identity" seriam os logits
            self.maest = es.TensorflowPredictMAEST(
                graphFilename=mp("discogs-maest-30s-pw-519l-2"), output="PartitionedCall/Identity_13"
            )

    def analisar_audio(self, audio16: np.ndarray, nomes=None) -> dict[str, ResultadoExtra]:
        """Um resultado por extra; a falha de um (ex.: faixa curta demais para o MAEST) não derruba os outros."""
        out = {}
        for nome in nomes or self.nomes:
            inicio = time.perf_counter()
            try:
                vet, rot = getattr(self, f"_{nome}")(audio16)
                vet = {k: np.asarray(v, dtype=np.float32) for k, v in vet.items()}
                out[nome] = ResultadoExtra(
                    self.hashes[nome], vet, rot, round(time.perf_counter() - inicio, 3)
                )
            except Exception as e:
                log.warning("extra %s falhou: %s", nome, e)
                out[nome] = ResultadoExtra(
                    self.hashes[nome],
                    {},
                    {},
                    round(time.perf_counter() - inicio, 3),
                    str(e)[:500] or type(e).__name__,
                )
        return out

    def analisar(self, caminho: Path, nomes=None) -> dict[str, ResultadoExtra]:
        """Decodifica o arquivo (como o núcleo: até `DURACAO_MAXIMA_S`) e roda os extras."""
        from curator.audio.analisador import DURACAO_MAXIMA_S, DURACAO_MINIMA_S, AudioInvalido, decodificar

        nativo, taxa, duracao = decodificar(caminho, DURACAO_MAXIMA_S)
        if duracao < DURACAO_MINIMA_S:
            raise AudioInvalido(f"áudio com {duracao:.1f} s (mínimo {DURACAO_MINIMA_S} s)")
        audio16 = (
            nativo
            if taxa == 16000
            else self.es.Resample(inputSampleRate=taxa, outputSampleRate=16000, quality=4)(nativo)
        )
        return self.analisar_audio(audio16, nomes)

    # ------------------------------------------------------------------ um extra

    def _msd_tags(self, a16):
        p = self.msd(a16)
        return {"msd_tags": p.mean(axis=0)}, {"msd_tags": self.rotulos["msd-musicnn-1"]}

    def _yamnet(self, a16):
        act, emb = self.yam_act(a16), self.yam_emb(a16)  # (quadros de ~0,96 s, 521) e (quadros, 1024)
        rot = self.rotulos["audioset-yamnet-1"]
        return (
            {
                "yamnet": act.mean(axis=0),
                "yamnet_p90": np.quantile(act, 0.9, axis=0),
                "yamnet_emb": emb.mean(axis=0),
            },
            {"yamnet": rot, "yamnet_p90": rot},
        )

    def _maest(self, a16):
        p = self.maest(a16)
        p = p.reshape(-1, p.shape[-1])  # (trechos de 30 s, 519)
        rot = self.rotulos["discogs-maest-30s-pw-519l-2"]
        return {"maest519": p.mean(axis=0), "maest519_max": p.max(axis=0)}, {
            "maest519": rot,
            "maest519_max": rot,
        }


# ------------------------------------------------------------------ banco


def gravar(
    con: sqlite3.Connection, video_id: str, resultados: dict[str, ResultadoExtra], fonte_ref: str | None
):
    """Vetores, rótulos e o status de cada extra. Chame dentro de uma transação quando gravar junto do núcleo."""
    for nome, r in resultados.items():
        for tipo, rot in r.rotulos.items():
            con.executemany(
                "INSERT INTO rotulos_modelo (tipo, indice, rotulo) VALUES (?, ?, ?) "
                "ON CONFLICT (tipo, indice) DO UPDATE SET rotulo = excluded.rotulo",
                [(tipo, i, x) for i, x in enumerate(rot)],
            )
        con.execute(
            f"DELETE FROM vetores WHERE video_id = ? AND tipo IN ({','.join('?' * len(EXTRAS[nome].tipos))})",
            (video_id, *EXTRAS[nome].tipos),
        )
        for tipo, v in r.vetores.items():
            vetores.gravar(con, video_id, tipo, v)
        con.execute(
            """INSERT OR REPLACE INTO analises_extras
                 (video_id, extra, hash, status, fonte_ref, tempo_s, erro, analisada_em)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (video_id, nome, r.hash, "erro" if r.erro else "ok", fonte_ref, r.tempo_s, r.erro, agora()),
        )


def descartar(con: sqlite3.Connection, video_id: str) -> None:
    """O áudio da faixa mudou (outra fonte): os extras calculados no áudio antigo deixam de valer."""
    con.execute("DELETE FROM analises_extras WHERE video_id = ?", (video_id,))
    con.execute(
        f"DELETE FROM vetores WHERE video_id = ? AND tipo IN ({','.join('?' * len(TIPOS_EXTRAS))})",
        (video_id, *TIPOS_EXTRAS),
    )


def pendentes(con: sqlite3.Connection, hashes: dict[str, str]) -> list[tuple[str, str, list[str]]]:
    """(video_id, arquivo guardado, extras que faltam) das faixas analisadas com áudio guardado.

    Falta = sem registro ou com hash diferente do atual. Um erro com o hash atual não volta sozinho (a mesma
    faixa falharia de novo); a biblioteca vem antes, na ordem da fila."""
    from curator.banco.consultas import BIBLIOTECA

    feitos: dict[str, dict[str, str]] = {}
    for r in con.execute("SELECT video_id, extra, hash FROM analises_extras"):
        feitos.setdefault(r[0], {})[r[1]] = r[2]
    out = []
    for r in con.execute(
        f"""SELECT a.video_id, g.caminho FROM analises a JOIN audios_guardados g USING (video_id)
              JOIN faixas f USING (video_id)
            WHERE a.status = 'ok'
            ORDER BY a.video_id NOT IN ({BIBLIOTECA}), f.posicao_curtida IS NULL, f.posicao_curtida, a.video_id"""
    ):
        faltam = [n for n, h in hashes.items() if feitos.get(r[0], {}).get(n) != h]
        if faltam:
            out.append((r[0], r[1], faltam))
    return out


def resumo(con: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """{extra: {status: n}} só com o hash atual de cada extra."""
    out: dict[str, dict[str, int]] = {n: {} for n in EXTRAS}
    for nome in EXTRAS:
        for status, n in con.execute(
            "SELECT status, count(*) FROM analises_extras WHERE extra = ? AND hash = ? GROUP BY status",
            (nome, hash_extra(nome)),
        ):
            out[nome][status] = n
    return out


@dataclass(frozen=True)
class ProgressoExtra:
    n: int
    total: int
    titulo: str
    feitos: list[str]
    erros: list[str]
    segundos: float

    def linha(self) -> str:
        e = f" · erro em {', '.join(self.erros)}" if self.erros else ""
        return f"[extras {self.n}/{self.total}] {self.titulo} · {', '.join(self.feitos) or '-'} em {self.segundos:.0f} s{e}"


def complementar(
    con: sqlite3.Connection,
    analisador,
    limite: int | None = None,
    deve_parar: Callable[[], bool] = lambda: False,
    ao_processar: Callable[[ProgressoExtra], None] | None = None,
) -> dict[str, int]:
    """Roda os extras que faltam nas faixas com áudio guardado, sem rede. `analisador`: um `AnalisadorExtras`."""
    from curator.audio.analisador import AudioInvalido

    lista = pendentes(con, analisador.hashes)
    if limite is not None:
        lista = lista[:limite]
    cont = {"faixas": 0, "erros": 0, "sem arquivo": 0}
    for i, (vid, caminho, faltam) in enumerate(lista, 1):
        if deve_parar():
            break
        inicio = time.perf_counter()
        ref = con.execute("SELECT fonte_ref FROM analises WHERE video_id = ?", (vid,)).fetchone()[0]
        if not Path(caminho).is_file():
            cont["sem arquivo"] += 1
            log.info("%s: áudio guardado sumiu (%s)", vid, caminho)
            continue
        try:
            res = analisador.analisar(Path(caminho), faltam)
        except AudioInvalido as e:
            res = {
                n: ResultadoExtra(analisador.hashes[n], {}, {}, 0.0, f"áudio inválido: {e}"[:500])
                for n in faltam
            }
        with transacao(con):
            gravar(con, vid, res, ref)
        cont["faixas"] += 1
        erros = [n for n, r in res.items() if r.erro]
        cont["erros"] += bool(erros)
        if ao_processar:
            t = con.execute("SELECT titulo FROM faixas WHERE video_id = ?", (vid,)).fetchone()[0]
            feitos = [n for n in res if n not in erros]
            ao_processar(ProgressoExtra(i, len(lista), t, feitos, erros, time.perf_counter() - inicio))
    return cont
