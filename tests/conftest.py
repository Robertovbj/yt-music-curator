import pytest


@pytest.fixture(autouse=True)
def dirs_isolados(tmp_path, monkeypatch):
    """Nenhum teste toca em ~/.config ou ~/.local/share do usuário."""
    monkeypatch.setenv("CURATOR_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CURATOR_DATA_DIR", str(tmp_path / "dados"))
    return tmp_path


@pytest.fixture(autouse=True)
def sem_rede(monkeypatch):
    """Nenhum teste chama a API do Deezer de verdade (#65)."""

    def recusar(*a, **k):
        raise AssertionError("teste tentou acessar a rede (Deezer)")

    monkeypatch.setattr("curator.coleta.deezer.urllib.request.urlopen", recusar)
