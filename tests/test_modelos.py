import hashlib
import io

import pytest

from curator.audio import modelos


def test_manifesto_tem_todos_os_modelos_do_classifier():
    _, ms = modelos.manifesto()
    nomes = {m.nome for m in ms}
    assert len(ms) == 22
    assert {
        "discogs-effnet-bs64-1",
        "genre_discogs400-discogs-effnet-1",
        "deam-msd-musicnn-2",
        "deeptemp-k16-3",
    } <= nomes
    assert all(len(m.pb_sha256) == 64 and len(m.json_sha256) == 64 for m in ms)


def test_hash_modelos_estavel():
    assert modelos.hash_modelos() == modelos.hash_modelos()
    assert len(modelos.hash_modelos()) == 16


class Resposta(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _falso_manifesto(monkeypatch, conteudo: bytes):
    sha = hashlib.sha256(conteudo).hexdigest()
    m = modelos.Modelo("m1", "pasta", sha, sha)
    monkeypatch.setattr(modelos, "manifesto", lambda: ("https://x", [m]))
    return m


def test_baixar_verifica_checksum_e_nao_rebaixa(tmp_path, monkeypatch):
    _falso_manifesto(monkeypatch, b"modelo")
    urls = []

    def abrir(url, timeout):
        urls.append(url)
        return Resposta(b"modelo")

    assert modelos.baixar(tmp_path, abrir) == 2
    assert urls == ["https://x/pasta/m1.pb", "https://x/pasta/m1.json"]
    assert modelos.verificar(tmp_path) == []
    assert modelos.baixar(tmp_path, abrir) == 0  # já presentes e íntegros


def test_baixar_rejeita_arquivo_corrompido(tmp_path, monkeypatch):
    _falso_manifesto(monkeypatch, b"modelo")
    with pytest.raises(RuntimeError, match="não confere"):
        modelos.baixar(tmp_path, lambda url, timeout: Resposta(b"adulterado"))
    assert list(tmp_path.iterdir()) == []  # nem o .part fica


def test_verificar_aponta_faltando_e_divergente(tmp_path, monkeypatch):
    _falso_manifesto(monkeypatch, b"modelo")
    (tmp_path / "m1.pb").write_bytes(b"outro")
    assert modelos.verificar(tmp_path) == ["checksum divergente: m1.pb", "faltando: m1.json"]
