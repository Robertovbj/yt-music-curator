import pytest

from curator.audio.casamento import score
from curator.audio.provedores.base import FaixaAlvo

FAIXA = FaixaAlvo("vid", "Do I Wanna Know?", ["Arctic Monkeys"], 272)


@pytest.mark.parametrize(
    ("titulo", "artista", "dur", "minimo", "maximo"),
    [
        ("Do I Wanna Know?", "Arctic Monkeys - Topic", 272, 0.99, 1.0),  # canal Topic
        ("Arctic Monkeys - Do I Wanna Know? (Official Video)", "ArcticMonkeysVEVO", 273, 0.95, 1.0),
        (
            "Arctic Monkeys - Do I Wanna Know? [Lyrics]",
            "Canal de letras",
            272,
            0.95,
            1.0,
        ),  # artista no título
        ("Do I Wanna Know? [Lyrics]", "Canal de letras", 272, 0.0, 0.81),  # sem o artista: abaixo do limiar
        ("Do I Wanna Know? (cover)", "Fulano Covers", 290, 0.0, 0.0),  # duração fora de 3× a tolerância
        ("R U Mine?", "Arctic Monkeys", 201, 0.0, 0.0),
        ("Do I Wanna Know?", "Arctic Monkeys", None, 0.85, 0.9),  # sem duração: penalizado
    ],
)
def test_score(titulo, artista, dur, minimo, maximo):
    assert minimo <= score(FAIXA, titulo, artista, dur, 3) <= maximo


@pytest.mark.parametrize(
    "candidato",
    ["Yuzu - Reason MIDI", "Reason (Piano Cover)", "Reason - slowed + reverb", "Reason [Nightcore]",
     "Reason (Karaoke Version)", "【歌ってみた】Reason", "Reason (Live at Budokan)"],
)  # fmt: skip
def test_versao_alternativa_e_recusada(candidato):
    """Falso positivo real da validação da #19: "Yuzu - Reason MIDI" passava com 0,84."""
    assert score(FaixaAlvo("v", "Reason", ["YUZU"], 305), candidato, "YUZU", 305, 3) == 0.0


def test_variante_presente_na_original_nao_e_penalizada():
    original = FaixaAlvo(
        "v", "Cogwork Dancers (Hollow Knight: Silksong) - Violin Cover", ["IAmARainbowSheep"], 113
    )
    assert score(original, "Cogwork Dancers - Violin Cover", "IAmARainbowSheep", 113, 3) > 0.82


def test_musica_diferente_do_mesmo_artista_fica_abaixo_do_limiar():
    assert score(FAIXA, "Why'd You Only Call Me When You're High?", "Arctic Monkeys", 272, 3) < 0.82
