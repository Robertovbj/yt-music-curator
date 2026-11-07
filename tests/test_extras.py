"""#88: análises extras (tags do MSD-MusiCNN, YAMNet, MAEST) com versão própria, fora do hash do núcleo."""

import hashlib
from pathlib import Path

import numpy as np
import pytest

from curator.audio import extras, fila, modelos
from curator.audio.analisador import AudioInvalido
from curator.audio.extras import ResultadoExtra
from curator.audio.persistir import Origem, gravar_analise
from curator.banco import vetores
from curator.config import caminhos, carregar_config
from tests.falso_audio import AnalisadorFalso, ProvedorFalso, resultado_falso
from tests.falso_yt import biblioteca_exemplo
from tests.test_analisador import _tom_estereo, precisa_ffmpeg

HASHES = {"msd_tags": "h-msd", "yamnet": "h-yam", "maest": "h-mae"}


def res(nome, h=None, erro=None):
    tipos = extras.EXTRAS[nome].tipos
    vet = {} if erro else {t: np.full(3, 0.5, np.float32) for t in tipos}
    return ResultadoExtra(h or HASHES[nome], vet, {} if erro else {tipos[0]: ["x", "y", "z"]}, 0.1, erro)


@pytest.fixture
def con(tmp_path):
    con = biblioteca_exemplo(caminhos().banco)
    for vid in "abc":
        gravar_analise(con, vid, resultado_falso(), Origem("youtube_music", f"ref-{vid}"), "1", "h", 1.0)
    for vid in "ab":  # c foi analisada sem guardar o áudio
        arq = tmp_path / f"{vid}.webm"
        arq.write_bytes(b"\0" * 10)
        con.execute(
            "INSERT INTO audios_guardados (video_id, caminho, provedor, ref, score, exato, parcial, bytes, guardado_em) "
            "VALUES (?, ?, 'youtube_music', ?, 1, 1, 0, 10, '2026-11-05')",
            (vid, str(arq), f"ref-{vid}"),
        )
    return con


def test_hash_do_nucleo_ignora_os_extras():
    _, ms = modelos.manifesto()
    assert {m.grupo for m in ms} == {"nucleo", "yamnet", "maest"}
    h = hashlib.sha256()
    for m in sorted((m for m in ms if m.grupo == "nucleo"), key=lambda m: m.nome):
        h.update(f"{m.nome}:{m.pb_sha256}:{m.json_sha256}\n".encode())
    assert modelos.hash_modelos() == h.hexdigest()[:16] == "91c82002b16efb3c"  # o da biblioteca analisada
    assert len({extras.hash_extra(n) for n in extras.EXTRAS}) == 3


def test_hash_do_extra_muda_com_a_versao(monkeypatch):
    antes = extras.hash_extra("yamnet")
    monkeypatch.setitem(extras.EXTRAS, "yamnet", extras.EXTRAS["yamnet"].__class__(
        "yamnet", "2", ("audioset-yamnet-1",), ("yamnet",)))  # fmt: skip
    assert extras.hash_extra("yamnet") != antes


def test_extra_desconhecido_no_config():
    with pytest.raises(ValueError, match="clap"):
        extras.validar(["yamnet", "clap"])


def test_pendentes_so_com_audio_guardado_e_hash_atual(con):
    assert [(v, f) for v, _, f in extras.pendentes(con, HASHES)] == [("a", list(HASHES)), ("b", list(HASHES))]
    extras.gravar(con, "a", {n: res(n) for n in HASHES}, "ref-a")
    extras.gravar(con, "b", {"msd_tags": res("msd_tags"), "maest": res("maest", erro="curta")}, "ref-b")
    # erro com o hash atual não volta sozinho; o que nunca rodou, sim
    assert [(v, f) for v, _, f in extras.pendentes(con, HASHES)] == [("b", ["yamnet"])]
    assert [(v, f) for v, _, f in extras.pendentes(con, {**HASHES, "maest": "h-novo"})] == [
        ("a", ["maest"]), ("b", ["yamnet", "maest"]),
    ]  # fmt: skip
    v = vetores.ler(con, "a", "yamnet_p90")
    assert v is not None and v.shape == (3,)
    assert (
        con.execute("SELECT rotulo FROM rotulos_modelo WHERE tipo = 'yamnet' AND indice = 2").fetchone()[0]
        == "z"
    )
    linha = con.execute(
        "SELECT status, erro FROM analises_extras WHERE video_id = 'b' AND extra = 'maest'"
    ).fetchone()
    assert tuple(linha) == ("erro", "curta")


def test_reanalise_do_nucleo_mantem_os_extras_e_descarta_se_o_audio_mudou(con):
    extras.gravar(con, "a", {"yamnet": res("yamnet")}, "ref-a")
    gravar_analise(con, "a", resultado_falso(), Origem("guardado?", "ref-a"), "2", "h2", 1.0)
    assert vetores.ler(con, "a", "yamnet") is not None and vetores.ler(con, "a", "effnet_emb") is not None
    assert con.execute("SELECT count(*) FROM analises_extras WHERE video_id = 'a'").fetchone()[0] == 1
    gravar_analise(con, "a", resultado_falso(), Origem("youtube_busca", "outra-ref"), "2", "h2", 1.0)
    assert vetores.ler(con, "a", "yamnet") is None
    assert con.execute("SELECT count(*) FROM analises_extras WHERE video_id = 'a'").fetchone()[0] == 0


class ExtrasFalso:
    hashes = HASHES

    def __init__(self, invalido=()):
        self.chamadas, self.invalido = [], set(invalido)

    def analisar(self, caminho, nomes):
        self.chamadas.append((Path(caminho).stem, list(nomes)))
        if Path(caminho).stem in self.invalido:
            raise AudioInvalido("curto")
        return {n: res(n) for n in nomes}


def test_complementar_roda_so_o_que_falta_e_registra_erros(con):
    extras.gravar(con, "a", {"msd_tags": res("msd_tags")}, "ref-a")
    an, linhas = ExtrasFalso(invalido={"b"}), []
    c = extras.complementar(con, an, ao_processar=linhas.append)
    assert an.chamadas == [("a", ["yamnet", "maest"]), ("b", list(HASHES))]
    assert c == {"faixas": 2, "erros": 1, "sem arquivo": 0}
    assert linhas[1].erros == list(HASHES) and "erro em" in linhas[1].linha()
    assert extras.pendentes(con, HASHES) == []
    assert extras.resumo(con)["yamnet"] == {}  # o resumo usa o hash real, não o do teste
    r = con.execute(
        "SELECT fonte_ref, status FROM analises_extras WHERE video_id = 'a' AND extra = 'maest'"
    ).fetchone()
    assert tuple(r) == ("ref-a", "ok")


def test_complementar_respeita_limite_parada_e_arquivo_sumido(con):
    Path(extras.pendentes(con, HASHES)[0][1]).unlink()
    c = extras.complementar(con, ExtrasFalso(), limite=1)
    assert c == {"faixas": 0, "erros": 0, "sem arquivo": 1}
    assert extras.complementar(con, ExtrasFalso(), deve_parar=lambda: True)["faixas"] == 0


def test_worker_grava_os_extras_da_mesma_passada(con):
    from curator.audio.worker import Worker
    from tests.test_worker import Relogio

    class ComExtras(AnalisadorFalso):
        def analisar(self, caminho):
            r = super().analisar(caminho)
            r.extras = {"yamnet": res("yamnet")}
            return r

    fila.enfileirar_biblioteca(con, "1", "outro-hash")
    cfg = carregar_config()
    Worker(con, cfg, ComExtras(), {"youtube_music": ProvedorFalso("youtube_music", ["ok"])},
           relogio=Relogio(), dormir=lambda s: None).rodar(limite=1)  # fmt: skip
    vid = con.execute("SELECT video_id FROM analises_extras WHERE extra = 'yamnet'").fetchone()[0]
    assert vetores.ler(con, vid, "yamnet_emb") is not None


@precisa_ffmpeg
def test_extras_reais_com_os_modelos_instalados(tmp_path):
    pytest.importorskip("essentia")
    pasta = Path.home() / ".local/share/yt-music-curator/models"
    if modelos.verificar(pasta, grupos={"yamnet", "maest"}):
        pytest.skip("modelos dos extras não instalados (curator modelos baixar)")
    an = extras.AnalisadorExtras(pasta, list(extras.EXTRAS))
    r = an.analisar(_tom_estereo(tmp_path / "t.webm", segundos=35))
    assert {n: x.erro for n, x in r.items()} == {"msd_tags": None, "yamnet": None, "maest": None}
    formas = {t: v.shape for x in r.values() for t, v in x.vetores.items()}
    assert formas == {
        "msd_tags": (50,), "yamnet": (521,), "yamnet_p90": (521,), "yamnet_emb": (1024,),
        "maest519": (519,), "maest519_max": (519,),
    }  # fmt: skip
    assert len(r["yamnet"].rotulos["yamnet"]) == 521 and "Rapping" in r["yamnet"].rotulos["yamnet"]
    r2 = an.analisar(_tom_estereo(tmp_path / "t.webm", segundos=35))
    assert all(np.array_equal(r[n].vetores[t], r2[n].vetores[t]) for n in r for t in r[n].vetores)


def test_analisar_completa_os_extras_sem_opcao_separada(con, monkeypatch):
    """#92: o `analisar` normal completa os extras que faltam (ou de versão nova) a partir do áudio guardado."""
    from typer.testing import CliRunner

    import curator.audio.analisador as mod_an
    from curator.audio.worker import Relatorio
    from curator.interfaces import cli
    from curator.interfaces.comandos import analise

    extras.gravar(con, "a", {n: res(n, h="hash-antigo") for n in HASHES}, "ref-a")  # versão antiga: refaz
    an_extras = ExtrasFalso()

    class AnalisadorComExtras:
        def __init__(self, pasta, extras):
            self.extras = an_extras

    monkeypatch.setattr(mod_an, "Analisador", AnalisadorComExtras)
    monkeypatch.setattr(analise, "_extras_com_modelos", lambda nomes: list(HASHES))
    monkeypatch.setattr(analise, "_rodar_worker", lambda *a, **k: Relatorio())
    r = CliRunner().invoke(cli.app, ["analisar"])
    assert r.exit_code == 0, r.output
    assert sorted(v for v, _ in an_extras.chamadas) == ["a", "b"]
    assert extras.pendentes(con, HASHES) == []
    for opcao in ("--so-extras", "--sem-extras"):
        assert CliRunner().invoke(cli.app, ["analisar", opcao]).exit_code != 0
