"""Caminhos (XDG) e configuração do usuário (`config.toml`).

Nada pessoal fica no repositório: credenciais e configuração em ~/.config/yt-music-curator,
banco e modelos em ~/.local/share/yt-music-curator. As variáveis CURATOR_CONFIG_DIR e
CURATOR_DATA_DIR sobrescrevem os dois diretórios (usado nos testes).
"""

import copy
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

APP = "yt-music-curator"

PADRAO: dict = {
    "metadados": {
        "ttl_dias": 30,
        "intervalo_s": 0.7,  # pausa entre chamadas ao ytmusicapi
    },
    "planos": {
        "limite_faixas": 200,  # acima disso, `plano aplicar` exige --grande além da confirmação
    },
    "playlists": {
        "ttl_h": 24,  # playlist com a mesma contagem e sincronizada há menos que isso não é rebaixada
    },
    "fila": {
        "cota_diaria": 200,  # downloads por saída (direta ou proxy) por dia
        "intervalo_min_s": 20,
        "intervalo_max_s": 60,
        "sleep_requests_s": 1.0,
        "max_tentativas": 6,
        "backoff_base_s": 300,
    },
    "disjuntor": {
        "quarentena_inicial_h": 3,
        "quarentena_max_h": 48,
    },
    "audio": {
        # menor bitrate aceitável: os modelos reamostram para 16/11/44,1 kHz de qualquer forma
        "formato": "bestaudio[abr<=70]/worstaudio/bestaudio",
        "cookies": "",  # arquivo de cookies de uma conta SECUNDÁRIA; vazio = sem cookies
    },
    "provedores": {
        # "bandcamp" fica fora: a busca exige desafio JS (ver provedores/bandcamp.py); dá para pôr de volta no config
        "ordem": ["youtube_music", "youtube_busca", "soundcloud", "pasta_local"],
        "limiar_casamento": 0.82,
        "tolerancia_duracao_s": 3,
    },
    "pasta_local": {
        "caminho": "",
    },
    "proxy": [],  # [[proxy]] nome = "...", url = "socks5://..."; opcionais: cota_diaria, intervalo_min_s/max_s
}


@dataclass(frozen=True)
class Caminhos:
    config_dir: Path
    dados_dir: Path

    @property
    def config_toml(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def credenciais(self) -> Path:
        return self.config_dir / "browser.json"

    @property
    def hierarquia(self) -> Path:
        return self.config_dir / "hierarquia.toml"

    @property
    def banco(self) -> Path:
        return self.dados_dir / "curator.db"

    @property
    def modelos_dir(self) -> Path:
        return self.dados_dir / "models"

    def garantir(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.dados_dir.mkdir(parents=True, exist_ok=True)


def caminhos() -> Caminhos:
    home = Path.home()
    config = (
        os.environ.get("CURATOR_CONFIG_DIR")
        or Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / APP
    )
    dados = (
        os.environ.get("CURATOR_DATA_DIR")
        or Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / APP
    )
    return Caminhos(Path(config), Path(dados))


def _mesclar(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _mesclar(out[k], v)
        else:
            out[k] = v
    return out


def carregar_config(arquivo: Path | None = None) -> dict:
    """Configuração padrão mesclada com o `config.toml` do usuário, se existir."""
    arquivo = arquivo or caminhos().config_toml
    if not arquivo.exists():
        return copy.deepcopy(PADRAO)
    with arquivo.open("rb") as f:
        return _mesclar(PADRAO, tomllib.load(f))
