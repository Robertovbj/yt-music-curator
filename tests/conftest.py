import pytest


@pytest.fixture(autouse=True)
def dirs_isolados(tmp_path, monkeypatch):
    """Nenhum teste toca em ~/.config ou ~/.local/share do usuário."""
    monkeypatch.setenv("CURATOR_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CURATOR_DATA_DIR", str(tmp_path / "dados"))
    return tmp_path
