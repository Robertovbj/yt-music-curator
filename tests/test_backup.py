import sqlite3
import stat
import zipfile

import pytest
from typer.testing import CliRunner

from curator.banco import backup
from curator.config import caminhos
from curator.interfaces.cli import app
from tests.falso_motor import biblioteca_analisada, playlist


@pytest.fixture
def origem():
    c = caminhos()
    c.garantir()
    con = biblioteca_analisada(c.banco)
    playlist(con, "PLcalma", "Calma", [f"ambient{i:02d}" for i in range(6)])
    con.execute(
        "INSERT INTO receitas (nome, spec, hash_spec, criada_em, atualizada_em) VALUES ('Dormir', '{}', 'h', 'x', 'x')"
    )
    (c.config_dir / "config.toml").write_text("[fila]\ncota_diaria = 1200\n")
    (c.config_dir / "hierarquia.toml").write_text('[[playlist]]\nid = "PLcalma"\n')
    (c.config_dir / "browser.json").write_text('{"cookie": "SEGREDO"}')
    (c.config_dir / "saidas.env").write_text("TS_AUTHKEY=tskey-auth-segredo\n")
    return c, con


def test_backup_sem_credenciais_por_padrao(origem, tmp_path):
    c, con = origem
    m = backup.criar(tmp_path / "b.zip", c)
    nomes = set(zipfile.ZipFile(tmp_path / "b.zip").namelist())
    assert nomes == {"manifesto.json", "dados/curator.db", "config/config.toml", "config/hierarquia.toml"}
    assert m["contagens"]["analises_ok"] == 36 and m["contagens"]["receitas"] == 1 and m["schema"] >= 4
    assert backup.verificar(tmp_path / "b.zip")[1] == []


def test_com_credenciais_inclui_e_protege(origem, tmp_path):
    c, _ = origem
    backup.criar(tmp_path / "b.zip", c, com_credenciais=True)
    assert {"config/browser.json", "config/saidas.env"} <= set(zipfile.ZipFile(tmp_path / "b.zip").namelist())
    assert stat.S_IMODE((tmp_path / "b.zip").stat().st_mode) == 0o600


def test_backup_com_outra_conexao_gravando_e_consistente(origem, tmp_path):
    c, con = origem
    con.execute(
        "BEGIN"
    )  # transação aberta e não confirmada noutra conexão (como o worker no meio de uma faixa)
    con.execute("DELETE FROM analises WHERE video_id LIKE 'rock%'")
    m = backup.criar(tmp_path / "b.zip", c)
    con.execute("ROLLBACK")
    assert m["contagens"]["analises_ok"] == 36  # a cópia vê o estado confirmado, não o meio da transação
    with zipfile.ZipFile(tmp_path / "b.zip") as z:
        (tmp_path / "x.db").write_bytes(z.read("dados/curator.db"))
    assert sqlite3.connect(tmp_path / "x.db").execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_verificar_aponta_adulteracao(origem, tmp_path):
    c, _ = origem
    backup.criar(tmp_path / "b.zip", c)
    with zipfile.ZipFile(tmp_path / "b.zip") as z:
        itens = {n: z.read(n) for n in z.namelist()}
    itens["config/config.toml"] = b"[fila]\ncota_diaria = 999999\n"
    with zipfile.ZipFile(tmp_path / "ruim.zip", "w") as z:
        for n, d in itens.items():
            z.writestr(n, d)
    assert backup.verificar(tmp_path / "ruim.zip")[1] == ["checksum divergente: config/config.toml"]


def test_cli_criar_e_verificar(origem, tmp_path):
    run = CliRunner().invoke
    r = run(app, ["backup", "criar", "--saida", str(tmp_path / "b.zip")])
    assert r.exit_code == 0 and "36 análises" in r.output
    assert "ok: todos os checksums" in run(app, ["backup", "verificar", str(tmp_path / "b.zip")]).output
