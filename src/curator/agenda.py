"""Tarefas diárias no systemd do usuário (#103): opcionais, nada roda sem `curator agenda ligar`.

Cada tarefa vira um par `curator-<nome>.service` + `.timer` em `~/.config/systemd/user/` (sem root). Os arquivos
apontam para o clone e o `uv` de quem ligou, descobertos na hora, e levam `CURATOR_DATA_DIR`/`CURATOR_CONFIG_DIR`
se estiverem definidos. Um arquivo que não foi gerado aqui (ex.: copiado do `contrib/systemd/` à mão) só é
substituído ou apagado com `forcar`.
"""

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

MARCA = "# Gerado por `curator agenda"


@dataclass(frozen=True)
class Tarefa:
    descricao: str
    argumentos: str  # depois de `curator`
    hora: str  # HH:MM padrão
    atraso_min: int  # RandomizedDelaySec
    extra_uv: str = ""
    servico_extra: str = ""


TAREFAS: dict[str, Tarefa] = {
    # antes do lote de análise, para as curtidas novas já entrarem na fila do dia
    "sincronizar": Tarefa(
        "sincronização diária (curtidas, playlists e metadados; as novas vão para a fila)",
        "sincronizar",
        "00:45",
        15,
    ),
    "analisar": Tarefa(
        "lote diário de análise de áudio (até esgotar a cota do dia)",
        "-v analisar --aguardar",
        "01:30",
        30,
        extra_uv="--extra analise",
        # SIGTERM: o worker termina a faixa atual e para; nada fica "baixando" na fila
        servico_extra="KillSignal=SIGTERM\nTimeoutStopSec=180\n",
    ),
}

Rodar = Callable[[list[str]], subprocess.CompletedProcess]


class ErroAgenda(RuntimeError):
    pass


def systemctl(args: list[str]) -> subprocess.CompletedProcess:
    if shutil.which("systemctl") is None:
        raise ErroAgenda("systemctl não encontrado: a agenda usa o systemd do usuário (veja `--mostrar`)")
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, check=False)


def dir_units() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"


def clone() -> Path:
    """A pasta do clone (onde está o `pyproject.toml`) de onde este curator roda."""
    raiz = Path(__file__).resolve().parents[2]
    if not (raiz / "pyproject.toml").is_file():
        raise ErroAgenda(f"não achei o clone do curator (sem pyproject.toml em {raiz})")
    return raiz


def achar_uv() -> Path:
    uv = shutil.which("uv")
    if uv is None:
        raise ErroAgenda("não achei o `uv` no PATH; passe o caminho com `--uv`")
    caminho = Path(uv)
    if "mise" in caminho.parts and "installs" in caminho.parts:
        # instalado pelo mise: o shim segue a versão configurada; o caminho de dentro de installs/ pode sumir
        shim = (
            Path(os.environ.get("MISE_DATA_DIR") or Path.home() / ".local" / "share" / "mise")
            / "shims"
            / "uv"
        )
        if shim.exists():
            return shim
    return caminho


def validar_hora(hora: str) -> str:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", hora)
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise ErroAgenda(f"hora inválida: {hora!r} (use HH:MM)")
    return f"{int(m[1]):02d}:{m[2]}"


def _esc(texto: str) -> str:
    return texto.replace("%", "%%")  # % é especificador nos arquivos do systemd


def _aspas(texto: str) -> str:
    return f'"{_esc(texto)}"' if re.search(r'[\s"\\]', texto) else _esc(texto)


def unidades(nome: str, hora: str, raiz: Path, uv: Path, env: dict[str, str] | None = None) -> dict[str, str]:
    """{arquivo: conteúdo} do service e do timer da tarefa."""
    t = TAREFAS[nome]
    env = os.environ if env is None else env
    cabeca = f"{MARCA} ligar {nome}` (#103). Para tirar: `curator agenda desligar {nome}`.\n"
    ambiente = [f"PATH={uv.parent}:/usr/local/bin:/usr/bin"]  # + ffmpeg, ffprobe e deno do sistema
    ambiente += [f"{k}={env[k]}" for k in ("CURATOR_DATA_DIR", "CURATOR_CONFIG_DIR") if env.get(k)]
    exec_ = " ".join(x for x in (_aspas(str(uv)), "run", t.extra_uv, "curator", t.argumentos) if x)
    servico = (
        f"{cabeca}# Logs: journalctl --user -u curator-{nome}\n"
        f"[Unit]\nDescription=yt-music-curator: {t.descricao}\n"
        "After=network-online.target\nWants=network-online.target\n\n"
        f"[Service]\nType=exec\nWorkingDirectory={_aspas(str(raiz))}\n"
        + "".join(f'Environment="{_esc(a)}"\n' for a in ambiente)
        + f"ExecStart={exec_}\n{t.servico_extra}Nice=10\nIOSchedulingClass=idle\n"
    )
    timer = (
        f"{cabeca}[Unit]\nDescription=yt-music-curator: dispara a {t.descricao.split(' (')[0]}\n\n"
        f"[Timer]\nOnCalendar=*-*-* {hora}\nRandomizedDelaySec={t.atraso_min}min\n"
        "Persistent=true\n\n[Install]\nWantedBy=timers.target\n"
    )
    return {f"curator-{nome}.service": servico, f"curator-{nome}.timer": timer}


def _alheios(nome: str, pasta: Path) -> list[Path]:
    """Arquivos da tarefa que existem mas não foram gerados pela agenda."""
    return [
        p
        for p in (pasta / f"curator-{nome}.service", pasta / f"curator-{nome}.timer")
        if p.exists() and MARCA not in p.read_text(encoding="utf-8", errors="replace")
    ]


def _checar(r: subprocess.CompletedProcess, acao: str) -> None:
    if r.returncode != 0:
        raise ErroAgenda(f"systemctl {acao} falhou: {(r.stderr or r.stdout).strip()}")


def ligar(
    nome: str,
    hora: str | None = None,
    *,
    uv: Path | None = None,
    forcar: bool = False,
    pasta: Path | None = None,
    rodar: Rodar | None = None,
) -> list[Path]:
    """Grava as unidades, recarrega o systemd e ativa o timer. Ligar de novo atualiza a hora e os caminhos."""
    pasta = pasta or dir_units()
    rodar = rodar or systemctl
    if (outros := _alheios(nome, pasta)) and not forcar:
        raise ErroAgenda(
            f"{', '.join(map(str, outros))} já existe e não foi gerado por `curator agenda` "
            "(instalado à mão?); use --forcar para substituir"
        )
    arquivos = unidades(nome, validar_hora(hora or TAREFAS[nome].hora), clone(), uv or achar_uv())
    pasta.mkdir(parents=True, exist_ok=True)
    gravados = []
    for arq, texto in arquivos.items():
        (pasta / arq).write_text(texto, encoding="utf-8")
        gravados.append(pasta / arq)
    _checar(rodar(["daemon-reload"]), "daemon-reload")
    _checar(rodar(["enable", "--now", f"curator-{nome}.timer"]), "enable")
    return gravados


def desligar(
    nome: str, *, forcar: bool = False, pasta: Path | None = None, rodar: Rodar | None = None
) -> list[Path]:
    """Desativa o timer e apaga as unidades. Uma execução em andamento termina normalmente."""
    pasta = pasta or dir_units()
    rodar = rodar or systemctl
    if (outros := _alheios(nome, pasta)) and not forcar:
        raise ErroAgenda(
            f"{', '.join(map(str, outros))} não foi gerado por `curator agenda` (instalado à mão?); "
            "use --forcar para desligar e apagar mesmo assim"
        )
    rodar(["disable", "--now", f"curator-{nome}.timer"])  # se não estava ligado, não há o que desfazer
    apagados = []
    for arq in (f"curator-{nome}.timer", f"curator-{nome}.service"):
        if (p := pasta / arq).exists():
            p.unlink()
            apagados.append(p)
    _checar(rodar(["daemon-reload"]), "daemon-reload")
    return apagados


def _propriedades(rodar: Rodar, unidade: str, props: str) -> dict[str, str]:
    r = rodar(["show", unidade, "-p", props])
    return dict(linha.split("=", 1) for linha in r.stdout.splitlines() if "=" in linha)


def estado(nome: str, *, pasta: Path | None = None, rodar: Rodar | None = None) -> dict:
    """Instalada? Por quem? Timer ativo, próxima e última execução, resultado da última."""
    pasta = pasta or dir_units()
    rodar = rodar or systemctl
    servico = pasta / f"curator-{nome}.service"
    out: dict = {"tarefa": nome, "instalada": servico.exists() and (pasta / f"curator-{nome}.timer").exists()}
    if not out["instalada"]:
        return out
    out["gerada_pela_agenda"] = not _alheios(nome, pasta)
    t = _propriedades(
        rodar, f"curator-{nome}.timer", "ActiveState,UnitFileState,NextElapseUSecRealtime,LastTriggerUSec"
    )
    s = _propriedades(rodar, f"curator-{nome}.service", "ActiveState,Result,ExecMainStatus")
    out.update(
        ativa=t.get("ActiveState") == "active",
        habilitada=t.get("UnitFileState") == "enabled",
        proxima=t.get("NextElapseUSecRealtime") or None,
        ultima=t.get("LastTriggerUSec") or None,
        rodando=s.get("ActiveState") == "active",
        resultado=s.get("Result"),
        codigo=s.get("ExecMainStatus"),
    )
    if hora := re.search(r"^OnCalendar=\S+ (\S+)$", (pasta / f"curator-{nome}.timer").read_text(), re.M):
        out["hora"] = hora[1]
    return out
