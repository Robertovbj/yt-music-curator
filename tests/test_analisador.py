"""Testes que precisam do ffmpeg e, no caso do analisador completo, do extra `[analise]` e dos modelos."""

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from curator.audio.analisador import AudioInvalido, decodificar

precisa_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg ausente")


def _tom_estereo(destino, segundos=8, taxa=48000, amplitude=0.5):
    """Seno de 440 Hz com a mesma amplitude exata nos dois canais, em opus/webm (o formato do YouTube)."""
    canal = f"{amplitude}*sin(2*PI*440*t)"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", f"aevalsrc={canal}|{canal}:s={taxa}:d={segundos}",
         "-c:a", "libopus", "-b:a", "96k", str(destino)],
        check=True,
    )  # fmt: skip
    return destino


@precisa_ffmpeg
def test_decodificar_media_dos_canais_sem_ganho(tmp_path):
    """O "-ac 1" do ffmpeg somava com ganho √2 (pico 0,71 aqui); a mixagem precisa ser a média (pico 0,5)."""
    audio, taxa = decodificar(_tom_estereo(tmp_path / "t.webm"))
    assert taxa == 48000
    assert len(audio) == pytest.approx(8 * 48000, rel=0.01)
    assert audio.dtype == np.float32
    assert np.abs(audio[4800:-4800]).max() == pytest.approx(0.5, abs=0.03)  # longe das bordas do codec


@precisa_ffmpeg
def test_decodificar_rejeita_arquivo_sem_audio(tmp_path):
    lixo = tmp_path / "x.webm"
    lixo.write_bytes(b"isto nao e audio")
    with pytest.raises(AudioInvalido):
        decodificar(lixo)


@precisa_ffmpeg
def test_analisador_completo_e_deterministico(tmp_path):
    """Roda com os modelos reais do usuário, se instalados (os testes isolam CURATOR_DATA_DIR)."""
    pytest.importorskip("essentia")
    pasta = Path.home() / ".local/share/yt-music-curator/models"
    from curator.audio import modelos

    if modelos.verificar(pasta):
        pytest.skip("modelos não instalados (curator modelos baixar)")
    from curator.audio.analisador import Analisador

    a = Analisador(pasta)
    arq = _tom_estereo(tmp_path / "t.webm", segundos=12)
    r1, r2 = a.analisar(arq), a.analisar(arq)
    assert r1.features == r2.features
    assert {k: v.shape for k, v in r1.vetores.items()} == {
        "effnet_emb": (1280,), "effnet_emb_std": (1280,), "discogs400": (400,), "jamendo_genre": (87,),
        "jamendo_mood": (56,), "jamendo_instr": (40,), "musicnn_emb": (200,),
    }  # fmt: skip
    assert all(np.array_equal(r1.vetores[k], r2.vetores[k]) for k in r1.vetores)
    assert len(r1.rotulos["discogs400"]) == 400
    assert 0 <= r1.features["valence"] <= 1 and 0 <= r1.features["energy"] <= 1
