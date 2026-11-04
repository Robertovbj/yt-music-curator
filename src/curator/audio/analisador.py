"""Analisador Essentia (porte de yt-classifier/teste.py, ADR-09).

Mudanças em relação ao protótipo: decodifica uma vez e reamostra (antes eram três leituras do arquivo),
devolve os embeddings e os vetores completos de probabilidade (antes só top-k), escolhe a classe positiva
das cabeças binárias pelo nome do rótulo e mede o tempo de cada etapa. Requer o extra `[analise]`.
"""

import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from curator.audio import modelos

log = logging.getLogger(__name__)

# Incrementar quando o pipeline mudar: as análises com versão diferente entram de novo na fila.
VERSAO_ANALISADOR = "1"

# Cabeças binárias sobre o EffNet: modelo -> (coluna em `features`, rótulo da classe positiva).
CABECAS_BINARIAS = {
    "danceability-discogs-effnet-1": ("danceability", "danceable"),
    "mood_acoustic-discogs-effnet-1": ("acousticness", "acoustic"),
    "mood_aggressive-discogs-effnet-1": ("aggressive", "aggressive"),
    "mood_electronic-discogs-effnet-1": ("electronic", "electronic"),
    "mood_happy-discogs-effnet-1": ("happy", "happy"),
    "mood_sad-discogs-effnet-1": ("sad", "sad"),
    "mood_relaxed-discogs-effnet-1": ("relaxed", "relaxed"),
    "mood_party-discogs-effnet-1": ("party", "party"),
    "voice_instrumental-discogs-effnet-1": ("instrumental", "instrumental"),
    "gender-discogs-effnet-1": ("voz_feminina", "female"),
    "engagement_2c-discogs-effnet-1": ("engagement", "engaging"),
    "approachability_2c-discogs-effnet-1": ("approachability", "approachable"),
}

# Cabeças multirrótulo: modelo -> (tipo em `vetores`, nó de entrada, nó de saída).
CABECAS_MULTI = {
    "genre_discogs400-discogs-effnet-1": (
        "discogs400",
        "serving_default_model_Placeholder",
        "PartitionedCall:0",
    ),
    "mtg_jamendo_genre-discogs-effnet-1": ("jamendo_genre", None, "model/Sigmoid"),
    "mtg_jamendo_moodtheme-discogs-effnet-1": ("jamendo_mood", None, "model/Sigmoid"),
    "mtg_jamendo_instrument-discogs-effnet-1": ("jamendo_instr", None, "model/Sigmoid"),
}

# Valência/excitação sobre o MusiCNN, escala 1-9, saída [valence, arousal] (confirmado nos .json).
MODELOS_AV = ["emomusic-msd-musicnn-2", "deam-msd-musicnn-2", "muse-msd-musicnn-2"]

# HEURÍSTICA do protótipo: "Modern Rock" não existe em nenhum conjunto de rótulos do Essentia.
# Cada tag recebe o maior score entre os estilos listados. Derivável do vetor discogs400 salvo.
GENEROS_PERSONALIZADOS = {
    "Alternative Rock": ["Rock---Alternative Rock"],
    "Modern Rock": ["Rock---Alternative Rock", "Rock---Pop Rock", "Rock---Hard Rock", "Rock---Post-Hardcore"],
}

DURACAO_MINIMA_S = 5.0
# Medleys de 1 h quebram o RhythmExtractor ("push item into source OnsetDetectionGlobal") e pesam no EffNet.
# Acima disso, só os primeiros minutos entram na análise; a duração total continua registrada.
DURACAO_MAXIMA_S = 15 * 60


class AudioInvalido(ValueError):
    """Arquivo curto demais ou sem áudio decodificável."""


@dataclass
class Resultado:
    features: dict[str, float | str | None]
    vetores: dict[str, np.ndarray]
    rotulos: dict[str, list[str]]
    duracao_s: float  # duração total do áudio (o worker compara com a da faixa)
    tempos: dict[str, float] = field(default_factory=dict)
    duracao_analisada_s: float | None = None  # < duracao_s quando a faixa passou de DURACAO_MAXIMA_S


def normalizar_1a9(x: float) -> float:
    return float(min(max((x - 1.0) / 8.0, 0.0), 1.0))


def energia(
    arousal: float | None, aggressive: float | None, relaxed: float | None, party: float | None
) -> float | None:
    """HEURÍSTICA (ADR-09): média de sinais que concordam entre si sobre "energia"."""
    partes = [arousal, aggressive, None if relaxed is None else 1.0 - relaxed, party]
    partes = [p for p in partes if p is not None]
    return float(np.mean(partes)) if partes else None


def generos_personalizados(discogs400: np.ndarray, rotulos: list[str]) -> dict[str, float]:
    score = dict(zip(rotulos, discogs400.tolist(), strict=True))
    return {tag: max(score.get(m, 0.0) for m in membros) for tag, membros in GENEROS_PERSONALIZADOS.items()}


def concordancia_tempo(bpm_rhythm: float, bpm_tempocnn: float) -> str:
    r = bpm_rhythm / bpm_tempocnn if bpm_tempocnn else 0.0
    if abs(r - 1) < 0.05:
        return "consistente"
    if abs(r - 2) < 0.1 or abs(r - 0.5) < 0.05:
        return "oitava"
    return "divergem"


class Analisador:
    """Carrega os modelos uma única vez; reutilize para a fila inteira."""

    def __init__(self, pasta_modelos: Path, verificar_checksums: bool = True):
        if verificar_checksums and (problemas := modelos.verificar(pasta_modelos)):
            raise RuntimeError(
                "modelos ausentes ou corrompidos: "
                + "; ".join(problemas[:5])
                + " — rode `curator modelos baixar`"
            )
        os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")  # CPU; o TensorFlow do essentia não usa GPU aqui
        import essentia
        import essentia.standard as es

        essentia.log.infoActive = False
        essentia.log.warningActive = False
        self.es = es
        self.hash_modelos = modelos.hash_modelos()
        mp = lambda nome, ext="pb": str(pasta_modelos / f"{nome}.{ext}")  # noqa: E731
        rotulos = lambda nome: _ler_classes(pasta_modelos / f"{nome}.json")  # noqa: E731

        self.effnet = es.TensorflowPredictEffnetDiscogs(
            graphFilename=mp("discogs-effnet-bs64-1"), output="PartitionedCall:1"
        )
        self.binarias = {
            nome: (es.TensorflowPredict2D(graphFilename=mp(nome), output="model/Softmax"), rotulos(nome))
            for nome in CABECAS_BINARIAS
        }
        self.multi = {}
        for nome, (tipo, entrada, saida) in CABECAS_MULTI.items():
            kw = {"input": entrada} if entrada else {}
            self.multi[nome] = (
                es.TensorflowPredict2D(graphFilename=mp(nome), output=saida, **kw),
                tipo,
                rotulos(nome),
            )
        self.musicnn = es.TensorflowPredictMusiCNN(
            graphFilename=mp("msd-musicnn-1"), output="model/dense/BiasAdd"
        )
        self.av = {
            nome: es.TensorflowPredict2D(graphFilename=mp(nome), output="model/Identity")
            for nome in MODELOS_AV
        }
        self.tempocnn = es.TempoCNN(graphFilename=mp("deeptemp-k16-3"))

    def reamostrar(self, audio: np.ndarray, de: int, para: int, qualidade: int) -> np.ndarray:
        """Reamostrador do próprio Essentia (o mesmo do MonoLoader, usado nos exemplos oficiais dos modelos).

        Os modelos são sensíveis ao reamostrador: com o do ffmpeg, `happy` chegou a variar 0,14 na mesma faixa.
        Qualidade 4 (linear) para 16/11 kHz e 1 para 44,1 kHz, como no MonoLoader do protótipo.
        """
        if de == para:
            return audio
        return self.es.Resample(inputSampleRate=de, outputSampleRate=para, quality=qualidade)(audio)

    def analisar(self, caminho: Path) -> Resultado:
        tempos: dict[str, float] = {}
        marco = time.perf_counter()

        def etapa(nome: str) -> None:
            nonlocal marco
            agora = time.perf_counter()
            tempos[nome] = round(agora - marco, 3)
            marco = agora

        nativo, taxa = decodificar(caminho)
        duracao = len(nativo) / taxa
        if duracao < DURACAO_MINIMA_S:
            raise AudioInvalido(f"áudio com {duracao:.1f} s (mínimo {DURACAO_MINIMA_S} s)")
        if duracao > DURACAO_MAXIMA_S:
            nativo = nativo[: int(DURACAO_MAXIMA_S * taxa)]
        audio44 = self.reamostrar(nativo, taxa, 44100, 1)
        audio16 = self.reamostrar(nativo, taxa, 16000, 4)
        audio11 = self.reamostrar(nativo, taxa, 11025, 4)
        del nativo
        etapa("decodificar")

        emb = self.effnet(audio16)  # (patches, 1280)
        etapa("effnet")
        features: dict[str, float | str | None] = {}
        for nome, (modelo, rot) in self.binarias.items():
            coluna, positivo = CABECAS_BINARIAS[nome]
            features[coluna] = float(modelo(emb).mean(axis=0)[rot.index(positivo)])
        vetores = {"effnet_emb": emb.mean(axis=0), "effnet_emb_std": emb.std(axis=0)}
        rotulos_saida: dict[str, list[str]] = {}
        for _nome, (modelo, tipo, rot) in self.multi.items():
            vetores[tipo] = modelo(emb).mean(axis=0)
            rotulos_saida[tipo] = rot
        etapa("cabecas")

        mus = self.musicnn(audio16)  # (patches, 200)
        vetores["musicnn_emb"] = mus.mean(axis=0)
        val, aro = [], []
        for modelo in self.av.values():
            pred = modelo(mus)  # (n, 2): [valence, arousal], escala 1-9
            val.append(float(pred[:, 0].mean()))
            aro.append(float(pred[:, 1].mean()))
        features["valence"] = normalizar_1a9(float(np.mean(val)))
        features["arousal"] = normalizar_1a9(float(np.mean(aro)))
        features["valence_spread"] = (max(val) - min(val)) / 8.0  # modelos discordando = menos confiança
        features["arousal_spread"] = (max(aro) - min(aro)) / 8.0
        etapa("musicnn_av")

        # Instâncias novas a cada faixa: depois de um erro interno, o RhythmExtractor reaproveitado passava a
        # falhar em todas as faixas seguintes ("Descriptor name 'internal.bpm' of type Real not found").
        bpm_r, _, conf, _, _ = self.es.RhythmExtractor2013(method="multifeature")(audio44)
        tom, escala, forca = self.es.KeyExtractor()(audio44)
        bpm_t, _, _ = self.tempocnn(audio11)
        features.update(
            bpm=float(bpm_t),  # TempoCNN erra menos oitava que o RhythmExtractor
            bpm_rhythm=float(bpm_r),
            bpm_tempocnn=float(bpm_t),
            beat_confidence=float(conf),
            tom=tom,
            escala=escala_pt(escala),
            key_strength=float(forca),
        )
        features["energy"] = energia(
            features["arousal"], features["aggressive"], features["relaxed"], features["party"]
        )
        etapa("tempo_tom")

        vetores = {k: np.asarray(v, dtype=np.float32) for k, v in vetores.items()}
        analisada = round(min(duracao, DURACAO_MAXIMA_S), 2)
        return Resultado(features, vetores, rotulos_saida, round(duracao, 2), tempos, analisada)


def decodificar(caminho: Path) -> tuple[np.ndarray, int]:
    """(mono float32, taxa nativa), decodificado pelo ffmpeg do sistema direto para a memória.

    O MonoLoader do essentia-tensorflow 2.1b6 não abre opus/webm ("Unsupported codec"), e converter
    para WAV gastaria ~30 MB de disco por faixa. A reamostragem fica com o Essentia (ver `reamostrar`).
    """
    for prog in ("ffmpeg", "ffprobe"):
        if not shutil.which(prog):
            raise RuntimeError(f"{prog} não encontrado no PATH (necessário para decodificar o áudio)")
    sonda = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=sample_rate,channels",
         "-of", "csv=p=0", str(caminho)],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    try:
        taxa, canais = (int(x) for x in sonda.stdout.strip().split(",")[:2])
    except ValueError:
        raise AudioInvalido(f"sem trilha de áudio legível: {sonda.stderr.strip()[:300]}") from None
    # Canais nativos + média no numpy: o "-ac 1" do ffmpeg soma com ganho √2 (+3 dB, picos acima de 1,0),
    # enquanto o MonoLoader do Essentia (e os modelos) usam a média simples dos canais.
    r = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(caminho), "-map", "0:a:0", "-f", "f32le", "-"],
        capture_output=True, check=False,
    )  # fmt: skip
    if r.returncode != 0:
        raise AudioInvalido(
            f"ffmpeg não decodificou o arquivo: {r.stderr.decode(errors='replace').strip()[:300]}"
        )
    amostras = np.frombuffer(r.stdout, dtype="<f4")
    amostras = amostras[: len(amostras) - len(amostras) % canais].reshape(-1, canais)
    return amostras.mean(axis=1, dtype=np.float32), taxa


def escala_pt(escala: str) -> str:
    return {"major": "maior", "minor": "menor"}.get(escala, escala)


def _ler_classes(arquivo: Path) -> list[str]:
    return json.loads(arquivo.read_text(encoding="utf-8"))["classes"]
