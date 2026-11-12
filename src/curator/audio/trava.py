"""Um worker por vez: `flock` exclusivo num arquivo da pasta de dados.

A fila não tem trava por item entre processos; dois `curator analisar` (ex.: o timer e um lote manual)
pegariam a mesma faixa e dobrariam o tráfego. O sistema libera o `flock` sozinho se o processo morrer.
O `curator sincronizar` usa a mesma trava num arquivo próprio (`sincronizar.lock`, #103): o timer e uma
sincronização manual não rodam juntos, mas a sincronização roda junto com o worker.
"""

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path


class OutroWorkerRodando(RuntimeError):
    pass


@contextmanager
def trava_worker(arquivo: Path, quem: str = "outro worker (`curator analisar` ou `audios baixar`)"):
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(arquivo, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            dono = os.pread(fd, 32, 0).decode(errors="replace").strip() or "?"
            raise OutroWorkerRodando(f"{quem} já está rodando (pid {dono})") from None
        os.ftruncate(fd, 0)
        os.pwrite(fd, str(os.getpid()).encode(), 0)
        yield
    finally:
        os.close(fd)  # fechar o descritor solta o flock
