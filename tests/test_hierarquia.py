import pytest
from typer.testing import CliRunner

from curator.banco import hierarquia
from curator.config import caminhos
from curator.interfaces.cli import app
from tests.falso_yt import HIERARQUIA_ROCK, biblioteca_exemplo


@pytest.fixture
def con(tmp_path):
    return biblioteca_exemplo(caminhos().banco)


def aplicar_toml(con, tmp_path, texto=HIERARQUIA_ROCK):
    arq = tmp_path / "h.toml"
    arq.write_text(texto)
    return hierarquia.aplicar(con, hierarquia.ler(arq))


def test_arvore_rock_all(con, tmp_path):
    aplicar_toml(con, tmp_path)
    assert hierarquia.arvore(con) == (
        "Dormir (1)\nRock - All (3) [agregadora]\n├─ Rock - Arena (2)\n└─ Rock - Folk (1)"
    )


@pytest.mark.parametrize(
    ("texto", "erro"),
    [
        ('[[playlist]]\nid = "PLnao"\n', "não é uma playlist própria"),
        (
            '[[playlist]]\nid = "PLall"\nmae = "PLarena"\n[[playlist]]\nid = "PLarena"\nmae = "PLall"\n',
            "ciclo",
        ),
        ('[[playlist]]\nid = "PLall"\nmae = "PLall"\n', "si mesma"),
        ('[[playlist]]\nid = "PLall"\ntipo = "pasta"\n', "inválido"),
    ],
)
def test_hierarquia_invalida_e_rejeitada(con, tmp_path, texto, erro):
    with pytest.raises(hierarquia.HierarquiaInvalida, match=erro):
        aplicar_toml(con, tmp_path, texto)


def test_reaplicar_remove_mae_de_quem_saiu_do_arquivo(con, tmp_path):
    aplicar_toml(con, tmp_path)
    aplicar_toml(con, tmp_path, '[[playlist]]\nid = "PLarena"\nmae = "PLall"\n')
    assert con.execute("SELECT mae_id FROM playlists WHERE playlist_id = 'PLfolk'").fetchone()[0] is None


def test_faltantes_na_agregadora(con, tmp_path):
    aplicar_toml(con, tmp_path)
    assert hierarquia.faltantes_nas_agregadoras(con) == {"PLall": ["x"]}


def test_sugerir_agrupa_por_prefixo(con, tmp_path):
    texto = hierarquia.sugerir(con)
    aplicar_toml(con, tmp_path, texto)
    assert "├─ Rock - Arena" in hierarquia.arvore(con)
    assert '"PLall"\ntitulo = "Rock - All"\ntipo = "agregadora"' in texto
    assert "PLsono" not in texto  # "Dormir" não tem prefixo nem filhas


def test_cli_mostrar_e_verificar(con, tmp_path):
    aplicar_toml(con, tmp_path)
    r = CliRunner().invoke(app, ["hierarquia", "mostrar"])
    assert r.exit_code == 0 and "└─ Rock - Folk (1)" in r.output
    r = CliRunner().invoke(app, ["hierarquia", "verificar"])
    assert r.exit_code == 2 and "faltam 1" in r.output
