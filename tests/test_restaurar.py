import json
import sqlite3
import stat
import zipfile

import pytest
from typer.testing import CliRunner

from curator.banco import backup, consultas
from curator.banco.conexao import abrir
from curator.config import Caminhos, caminhos
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


def outro_computador(tmp_path, monkeypatch) -> Caminhos:
    monkeypatch.setenv("CURATOR_CONFIG_DIR", str(tmp_path / "pc2" / "config"))
    monkeypatch.setenv("CURATOR_DATA_DIR", str(tmp_path / "pc2" / "dados"))
    return caminhos()


def test_restaurar_recusa_backup_adulterado(origem, tmp_path):
    c, _ = origem
    backup.criar(tmp_path / "b.zip", c)
    with zipfile.ZipFile(tmp_path / "b.zip") as z:
        itens = {n: z.read(n) for n in z.namelist()}
    itens["config/config.toml"] = b"[fila]\ncota_diaria = 999999\n"
    with zipfile.ZipFile(tmp_path / "ruim.zip", "w") as z:
        for n, d in itens.items():
            z.writestr(n, d)
    with pytest.raises(backup.BackupInvalido, match="nada foi alterado"):
        backup.restaurar(tmp_path / "ruim.zip", caminhos())


def test_restaurar_em_maquina_limpa_da_as_mesmas_estatisticas(origem, tmp_path, monkeypatch):
    c, con = origem
    antes = consultas.estatisticas(con)
    backup.criar(tmp_path / "b.zip", c, com_credenciais=True)
    novo = outro_computador(tmp_path, monkeypatch)
    r = backup.restaurar(tmp_path / "b.zip", novo)
    assert consultas.estatisticas(abrir(novo.banco)) == antes
    assert (novo.config_dir / "hierarquia.toml").read_text() == '[[playlist]]\nid = "PLcalma"\n'
    assert stat.S_IMODE((novo.config_dir / "browser.json").stat().st_mode) == 0o600
    assert any("modelos" in a for a in r.avisos)


def test_restaurar_por_cima_exige_forcar_e_guarda_os_atuais(origem, tmp_path):
    c, con = origem
    backup.criar(tmp_path / "b.zip", c)
    con.execute("DELETE FROM receitas")
    con.close()
    with pytest.raises(backup.BackupInvalido, match="--forcar"):
        backup.restaurar(tmp_path / "b.zip", c)
    r = backup.restaurar(tmp_path / "b.zip", c, forcar=True)
    assert abrir(c.banco).execute("SELECT count(*) FROM receitas").fetchone()[0] == 1
    guardado = next(p for p in r.guardados if ".db.antes-da-restauracao-" in p)
    assert sqlite3.connect(guardado).execute("SELECT count(*) FROM receitas").fetchone()[0] == 0


def test_recusa_schema_mais_novo(origem, tmp_path, monkeypatch):
    c, _ = origem
    backup.criar(tmp_path / "b.zip", c)
    with zipfile.ZipFile(tmp_path / "b.zip") as z:
        itens = {n: z.read(n) for n in z.namelist()}
    m = json.loads(itens["manifesto.json"])
    m["schema"] = 999
    itens["manifesto.json"] = json.dumps(m).encode()
    with zipfile.ZipFile(tmp_path / "novo.zip", "w") as z:
        for n, d in itens.items():
            z.writestr(n, d)
    with pytest.raises(backup.BackupInvalido, match="mais novo"):
        backup.restaurar(tmp_path / "novo.zip", outro_computador(tmp_path, monkeypatch))


def test_restaurar_recupera_itens_que_estavam_baixando(origem, tmp_path, monkeypatch):
    c, con = origem
    con.execute(
        "INSERT INTO fila_download (video_id, status, criada_em, atualizada_em) VALUES ('rap00', 'baixando', 'x', 'x')"
    )
    backup.criar(tmp_path / "b.zip", c)
    novo = outro_computador(tmp_path, monkeypatch)
    r = backup.restaurar(tmp_path / "b.zip", novo)
    assert (
        abrir(novo.banco).execute("SELECT status FROM fila_download WHERE video_id = 'rap00'").fetchone()[0]
        == "pendente"
    )
    assert any("voltaram para a fila" in a for a in r.avisos)


def test_backup_de_schema_antigo_e_migrado_na_restauracao(tmp_path, monkeypatch):
    from curator.banco.conexao import migracoes_do_pacote, versao

    c = caminhos()
    c.garantir()
    abrir(c.banco, migracoes_do_pacote()[:3]).close()  # um curator de antes da migração 004
    backup.criar(tmp_path / "b.zip", c)
    assert backup.verificar(tmp_path / "b.zip")[0]["schema"] == 3
    novo = outro_computador(tmp_path, monkeypatch)
    backup.restaurar(tmp_path / "b.zip", novo)
    con = abrir(novo.banco)
    assert versao(con) == migracoes_do_pacote()[-1][0]
    assert "snapshots" in {r[1] for r in con.execute("PRAGMA table_info(planos)")}


def test_cli_restaurar(origem, tmp_path, monkeypatch):
    backup.criar(tmp_path / "b.zip", origem[0])
    outro_computador(tmp_path, monkeypatch)
    r = CliRunner().invoke(app, ["backup", "restaurar", str(tmp_path / "b.zip")])
    assert r.exit_code == 0 and "restaurado:" in r.output
