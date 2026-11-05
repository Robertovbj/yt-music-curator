"""#84: com `[audio] guardar`, o áudio analisado fica guardado e a reanálise não baixa de novo."""

from pathlib import Path

import pytest

from curator.audio import fila, guardados
from curator.audio.guardados import Guardado
from curator.audio.worker import Worker
from curator.config import caminhos, carregar_config
from tests.falso_audio import AnalisadorFalso, ProvedorFalso
from tests.falso_yt import biblioteca_exemplo
from tests.test_worker import Relogio


@pytest.fixture
def con():
    con = biblioteca_exemplo(caminhos().banco)
    fila.enfileirar_biblioteca(con, "1", "hash-teste")
    return con


@pytest.fixture
def cfg():
    c = carregar_config()
    c["fila"]["cota_diaria"] = 100
    c["audio"]["guardar"] = True
    return c


def rodar(con, cfg, provs, limite=1, hash_modelos="hash-teste", duracao=200.0):
    an = AnalisadorFalso(duracao=duracao)
    an.hash_modelos = hash_modelos
    esperas = []
    w = Worker(con, cfg, an, {p.nome: p for p in provs}, relogio=Relogio(), dormir=esperas.append)
    return w.rodar(limite=limite), esperas


def guardado(con, vid):
    return con.execute("SELECT * FROM audios_guardados WHERE video_id = ?", (vid,)).fetchone()


def reabrir(con):
    """Modelo novo: a fila reabre as faixas já analisadas."""
    return fila.enfileirar_biblioteca(con, "1", "hash-novo")


def test_com_a_flag_o_audio_fica_guardado_com_a_origem(con, cfg):
    yt = ProvedorFalso("youtube_music", ["ok"])
    rodar(con, cfg, [yt])
    r = guardado(con, "a")
    caminho = Path(r["caminho"])
    assert caminho.is_file() and caminho.parent.parent == guardados.pasta(cfg) == caminhos().audios
    assert (r["provedor"], r["ref"], r["exato"], r["parcial"], r["bytes"]) == (
        "youtube_music", "youtube_music:a", 1, 0, 1000,
    )  # fmt: skip
    assert guardados.resumo(con) == (1, 1000)


def test_sem_a_flag_nada_fica(con, cfg):
    cfg["audio"]["guardar"] = False
    yt = ProvedorFalso("youtube_music", ["ok"])
    rodar(con, cfg, [yt])
    assert guardado(con, "a") is None and not yt.arquivos[0].exists()
    assert not caminhos().audios.exists()


def test_reanalise_usa_o_guardado_sem_rede_nem_pausa(con, cfg):
    rodar(con, cfg, [ProvedorFalso("youtube_music", ["ok"])])
    assert reabrir(con) == 0 and fila.resumo(con, 6)["por_status"]["pendente"] >= 1
    yt = ProvedorFalso("youtube_music", [])
    rel, esperas = rodar(con, cfg, [Guardado(con), yt], hash_modelos="hash-novo")
    assert rel.contagem == {"ok": 1}
    assert yt.saidas_usadas == [] and esperas == []  # nem buscou no provedor de rede, nem dormiu
    a = con.execute("SELECT fonte_audio, hash_modelos FROM analises WHERE video_id = 'a'").fetchone()
    assert tuple(a) == ("youtube_music", "hash-novo")  # a fonte é a do download original
    assert con.execute("SELECT provedor FROM fila_download WHERE video_id = 'a'").fetchone()[0] == "guardado"
    assert con.execute("SELECT uso_hoje FROM proxies WHERE nome = 'direta'").fetchone()[0] == 1
    assert Path(guardado(con, "a")["caminho"]).is_file()  # continua guardado para a próxima


def test_arquivo_sumido_cai_para_o_proximo_provedor(con, cfg):
    rodar(con, cfg, [ProvedorFalso("youtube_music", ["ok"])])
    Path(guardado(con, "a")["caminho"]).unlink()
    reabrir(con)
    yt = ProvedorFalso("youtube_music", ["ok"])
    rel, _ = rodar(con, cfg, [Guardado(con), yt], hash_modelos="hash-novo")
    assert rel.contagem == {"ok": 1} and yt.saidas_usadas == ["direta"]
    assert Path(guardado(con, "a")["caminho"]).is_file()  # baixado de novo e guardado outra vez


def test_pasta_local_nao_e_copiada(con, cfg, tmp_path):
    class Local(ProvedorFalso):
        def baixar(self, candidato, destino, saida):
            from curator.audio.provedores.base import Arquivo

            arq = tmp_path / "minha.flac"
            arq.write_bytes(b"\0" * 10)
            return Arquivo(arq, 10, temporario=False)

    rodar(con, cfg, [Local("pasta_local", ["ok"], usa_rede=False)])
    assert guardado(con, "a") is None and (tmp_path / "minha.flac").exists()


def test_candidato_descartado_pela_duracao_nao_e_guardado(con, cfg):
    busca = ProvedorFalso("youtube_busca", ["ok"], score=0.95, exato=False)
    rodar(con, cfg, [busca], duracao=400.0)  # a faixa tem 200 s: outra música
    assert guardado(con, "a") is None and not caminhos().audios.exists()


def test_limpar_apaga_arquivos_e_registros(con, cfg):
    rodar(con, cfg, [ProvedorFalso("youtube_music", ["ok", "ok"])], limite=2)
    caminhos_ = [Path(r[0]) for r in con.execute("SELECT caminho FROM audios_guardados")]
    assert len(caminhos_) == 2 and all(c.exists() for c in caminhos_)
    assert guardados.limpar(con) == 2
    assert guardados.resumo(con) == (0, 0) and not any(c.exists() for c in caminhos_)


def test_cli_status_e_limpar(con, cfg):
    from typer.testing import CliRunner

    from curator.interfaces.cli import app

    rodar(con, cfg, [ProvedorFalso("youtube_music", ["ok"])])
    run = CliRunner().invoke
    r = run(app, ["audios", "status"])
    assert r.exit_code == 0 and "1 áudio(s) guardado(s), 0.0 MB" in r.output and "desligado" in r.output
    assert run(app, ["audios", "limpar"], input="n\n").exit_code == 1 and guardados.resumo(con)[0] == 1
    r = run(app, ["audios", "limpar", "--sim"])
    assert r.exit_code == 0 and "1 áudio(s) apagado(s)" in r.output and guardados.resumo(con) == (0, 0)
