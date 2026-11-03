import os

import pytest

from curator.audio.trava import OutroWorkerRodando, trava_worker


def test_segundo_worker_e_recusado_e_trava_solta_ao_sair(tmp_path):
    arq = tmp_path / "worker.lock"
    with trava_worker(arq):
        assert arq.read_text() == str(os.getpid())
        with pytest.raises(OutroWorkerRodando, match=f"pid {os.getpid()}"), trava_worker(arq):
            pass
    with trava_worker(arq):  # liberada depois do primeiro sair
        pass


def test_trava_solta_mesmo_com_excecao(tmp_path):
    arq = tmp_path / "worker.lock"
    with pytest.raises(ValueError), trava_worker(arq):
        raise ValueError
    with trava_worker(arq):
        pass
