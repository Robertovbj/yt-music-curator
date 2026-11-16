from typer.testing import CliRunner

from curator.config import PADRAO, caminhos, carregar_config
from curator.interfaces.cli import app


def test_caminhos_respeitam_variaveis(dirs_isolados):
    c = caminhos()
    assert c.config_dir == dirs_isolados / "config"
    assert c.banco == dirs_isolados / "dados" / "curator.db"


def test_config_padrao_sem_arquivo():
    assert carregar_config() == PADRAO


def test_so_o_maest_vem_ligado_e_os_outros_extras_seguem_validos():
    from curator.audio import extras

    assert PADRAO["analise"]["extras"] == ["maest"]
    assert extras.validar(["msd_tags", "yamnet", "maest"]) == ["msd_tags", "yamnet", "maest"]


def test_config_do_usuario_mescla_com_padrao():
    c = caminhos()
    c.garantir()
    c.config_toml.write_text('[fila]\ncota_diaria = 50\n\n[[proxy]]\nnome = "vps"\nurl = "socks5://x:1"\n')
    cfg = carregar_config()
    assert cfg["fila"]["cota_diaria"] == 50
    assert cfg["fila"]["intervalo_min_s"] == PADRAO["fila"]["intervalo_min_s"]
    assert cfg["proxy"] == [{"nome": "vps", "url": "socks5://x:1"}]


def test_cli_versao():
    r = CliRunner().invoke(app, ["--version"])
    assert r.exit_code == 0
    assert "yt-music-curator" in r.output
