"""#103: tarefas diárias opcionais no systemd do usuário. Nenhum teste toca no systemd de verdade."""

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from curator import agenda
from curator.audio.trava import trava_worker
from curator.config import caminhos
from curator.interfaces.cli import app
from curator.interfaces.comandos import coleta

UV = Path("/home/x/.local/share/mise/shims/uv")


class FalsoSystemctl:
    def __init__(self):
        self.chamadas: list[list[str]] = []
        self.habilitado: set[str] = set()

    def __call__(self, args):
        self.chamadas.append(args)
        if args[:2] == ["enable", "--now"]:
            self.habilitado.add(args[2])
        if args[:2] == ["disable", "--now"]:
            self.habilitado.discard(args[2])
        saida = ""
        if args[0] == "show" and args[1].endswith(".timer"):
            ligado = args[1] in self.habilitado
            saida = (
                f"ActiveState={'active' if ligado else 'inactive'}\nUnitFileState={'enabled' if ligado else 'disabled'}\n"
                "NextElapseUSecRealtime=Fri 2026-11-13 00:52:11 -03\nLastTriggerUSec=n/a\n"
            )
        elif args[0] == "show":
            saida = "ActiveState=inactive\nResult=success\nExecMainStatus=0\n"
        return subprocess.CompletedProcess(args, 0, saida, "")


@pytest.fixture
def pasta(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg" / "systemd" / "user"


@pytest.fixture
def sysctl(monkeypatch):
    f = FalsoSystemctl()
    monkeypatch.setattr(agenda, "systemctl", f)
    monkeypatch.setattr(agenda, "achar_uv", lambda: UV)
    return f


def test_nada_instalado_sem_ligar(pasta, sysctl):
    r = CliRunner().invoke(app, ["agenda", "status"])
    assert r.exit_code == 0 and "sincronizar  desligada" in r.output and "analisar     desligada" in r.output
    assert not pasta.exists() and not [c for c in sysctl.chamadas if c[0] != "show"]


def test_unidades_apontam_para_o_clone_e_o_uv():
    u = agenda.unidades(
        "sincronizar", "00:45", Path("/home/x/meu clone"), UV, env={"CURATOR_DATA_DIR": "/d/50%"}
    )
    servico, timer = u["curator-sincronizar.service"], u["curator-sincronizar.timer"]
    assert agenda.MARCA in servico and agenda.MARCA in timer
    assert 'WorkingDirectory="/home/x/meu clone"' in servico
    assert 'Environment="PATH=/home/x/.local/share/mise/shims:/usr/local/bin:/usr/bin"' in servico
    assert 'Environment="CURATOR_DATA_DIR=/d/50%%"' in servico and "CURATOR_CONFIG_DIR" not in servico
    assert f"ExecStart={UV} run curator sincronizar\n" in servico
    assert "OnCalendar=*-*-* 00:45\n" in timer and "Persistent=true" in timer
    a = agenda.unidades("analisar", "01:30", Path("/c"), UV, env={})["curator-analisar.service"]
    assert (
        f"ExecStart={UV} run --extra analise curator -v analisar --aguardar\n" in a
        and "KillSignal=SIGTERM" in a
    )


def test_ligar_status_e_desligar(pasta, sysctl):
    run = CliRunner().invoke
    r = run(app, ["agenda", "ligar", "sincronizar", "--hora", "0:30"])
    assert r.exit_code == 0, r.output
    assert "todo dia às 00:30" in r.output and "próxima: Fri 2026-11-13" in r.output
    assert {p.name for p in pasta.iterdir()} == {"curator-sincronizar.service", "curator-sincronizar.timer"}
    assert f"WorkingDirectory={agenda.clone()}" in (pasta / "curator-sincronizar.service").read_text()
    assert ["daemon-reload"] in sysctl.chamadas and [
        "enable",
        "--now",
        "curator-sincronizar.timer",
    ] in sysctl.chamadas
    r = run(app, ["agenda", "status"])
    assert "sincronizar  ligada, às 00:30" in r.output and "última: nunca" in r.output
    assert "analisar     desligada" in r.output
    r = run(app, ["agenda", "ligar", "sincronizar"])  # de novo: atualiza a hora
    assert r.exit_code == 0 and "OnCalendar=*-*-* 00:45" in (pasta / "curator-sincronizar.timer").read_text()
    r = run(app, ["agenda", "desligar", "sincronizar"])
    assert r.exit_code == 0 and "desligada" in r.output and not list(pasta.iterdir())
    assert "curator-sincronizar.timer" not in sysctl.habilitado
    r = run(app, ["agenda", "desligar", "sincronizar"])
    assert r.exit_code == 0 and "não estava instalada" in r.output


def test_mostrar_nao_instala(pasta, sysctl):
    r = CliRunner().invoke(app, ["agenda", "ligar", "analisar", "--mostrar"])
    assert r.exit_code == 0 and "[Timer]" in r.output and "OnCalendar=*-*-* 01:30" in r.output
    assert not pasta.exists() and not sysctl.chamadas


def test_unidade_instalada_a_mao_so_com_forcar(pasta, sysctl):
    pasta.mkdir(parents=True)
    (pasta / "curator-analisar.service").write_text("[Service]\nExecStart=uv run curator analisar\n")
    (pasta / "curator-analisar.timer").write_text("[Timer]\nOnCalendar=*-*-* 01:30\n")
    run = CliRunner().invoke
    for args in (["ligar", "analisar"], ["desligar", "analisar"]):
        r = run(app, ["agenda", *args])
        assert r.exit_code != 0 and "instalado à mão" in r.output
    assert "uv run curator analisar" in (pasta / "curator-analisar.service").read_text()
    assert "instalada à mão" in run(app, ["agenda", "status"]).output
    r = run(app, ["agenda", "ligar", "analisar", "--forcar"])
    assert r.exit_code == 0 and agenda.MARCA in (pasta / "curator-analisar.service").read_text()


def test_entradas_invalidas(pasta, sysctl):
    run = CliRunner().invoke
    assert "não existe" in run(app, ["agenda", "ligar", "baixar"]).output
    r = run(app, ["agenda", "ligar", "sincronizar", "--hora", "25:00"])
    assert r.exit_code != 0 and "hora inválida" in r.output and not pasta.exists()


def test_falha_do_systemctl_vira_mensagem(pasta, monkeypatch):
    def falha(args):
        return subprocess.CompletedProcess(args, 1, "", "Failed to connect to bus")

    monkeypatch.setattr(agenda, "achar_uv", lambda: UV)
    with pytest.raises(agenda.ErroAgenda, match="Failed to connect to bus"):
        agenda.ligar("sincronizar", rodar=falha)


def test_duas_sincronizacoes_nao_rodam_juntas(monkeypatch):
    chamou = []
    monkeypatch.setattr(coleta, "_sincronizar", lambda *a: chamou.append(a))
    with trava_worker(caminhos().dados_dir / coleta.TRAVA):
        r = CliRunner().invoke(app, ["sincronizar"])
    assert r.exit_code == 0 and "outro `curator sincronizar` já está rodando" in r.output and not chamou
    assert CliRunner().invoke(app, ["sincronizar"]).exit_code == 0 and len(chamou) == 1


def test_uv_do_mise_vira_o_shim(tmp_path, monkeypatch):
    shim = tmp_path / "mise" / "shims" / "uv"
    shim.parent.mkdir(parents=True)
    shim.touch()
    monkeypatch.setenv("MISE_DATA_DIR", str(tmp_path / "mise"))
    monkeypatch.setattr(
        agenda.shutil, "which", lambda n: "/h/.local/share/mise/installs/uv/latest/.mise-bins/uv"
    )
    assert agenda.achar_uv() == shim
    monkeypatch.setattr(agenda.shutil, "which", lambda n: "/usr/bin/uv")
    assert agenda.achar_uv() == Path("/usr/bin/uv")
