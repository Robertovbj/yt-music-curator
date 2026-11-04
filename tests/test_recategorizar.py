"""#74: refazer as filhas de UMA mãe pelo som; as filhas antigas não servem de referência."""

import json

import pytest

pytest.importorskip("umap")

from typer.testing import CliRunner  # noqa: E402

from curator.coleta import aplicar as ap  # noqa: E402
from curator.coleta.cliente import ClienteYT  # noqa: E402
from curator.config import caminhos  # noqa: E402
from curator.interfaces.cli import app  # noqa: E402
from curator.interfaces.comandos import auth  # noqa: E402
from curator.motor import clusters, planos  # noqa: E402
from curator.motor import reorganizar as reorg  # noqa: E402
from tests.falso_motor import biblioteca_analisada, playlist  # noqa: E402
from tests.falso_yt import FalsoYT, faixa  # noqa: E402

ROCK = [f"rock{i:02d}" for i in range(14)]
RAP = [f"rap{i:02d}" for i in range(14)]
# a divisão antiga, feita "por outra lógica": mistura os dois estilos
ANTIGAS = {"PLa": ("Antiga A", ROCK[:7] + RAP[:7]), "PLb": ("Antiga B", ROCK[7:] + RAP[7:])}


@pytest.fixture
def con():
    c = biblioteca_analisada(caminhos().banco, por_estilo=14)
    playlist(c, "PLmae", "Pesado", ROCK + RAP, tipo="agregadora")
    for pid, (titulo, ids) in ANTIGAS.items():
        playlist(c, pid, titulo, ids, mae="PLmae")
    playlist(c, "PLcalma", "Calma", [f"ambient{i:02d}" for i in range(14)])
    return c


@pytest.fixture
def yt():
    y = FalsoYT()
    for pid, (titulo, ids) in {**ANTIGAS, "PLmae": ("Pesado", ROCK + RAP)}.items():
        y.playlists[pid] = {"title": titulo, "tracks": [faixa(v) for v in ids]}
    return y


P = clusters.Parametros(min_cluster_size=6, selecao="leaf")


def test_cria_filhas_pelo_som_e_esvazia_as_antigas(con):
    _, cs, _ = clusters.obter(con, "PLmae", P)
    ops = reorg.recategorizar(con, "PLmae", cs)
    novas = [o for o in ops if o.tipo == "adicionar"]
    assert novas and all(o.playlist.startswith("nova:") for o in novas)
    assert all(set(o.video_ids) <= set(ROCK) or set(o.video_ids) <= set(RAP) for o in novas)  # o som separa
    removidas = {o.playlist: set(o.video_ids) for o in ops if o.tipo == "remover"}
    assert removidas == {pid: set(ids) for pid, (_, ids) in ANTIGAS.items()}
    assert not {o.playlist for o in ops} & {"PLmae", "PLcalma"}  # a mãe e o resto ficam intactos


def test_aplicado_as_novas_viram_filhas_da_mae(con, yt, monkeypatch):
    _, cs, _ = clusters.obter(con, "PLmae", P)
    pid = planos.de_recategorizacao(con, "PLmae", reorg.recategorizar(con, "PLmae", cs))
    res = ap.aplicar(con, ClienteYT(yt, intervalo_s=0, dormir=lambda s: None), pid)
    assert res.status == "aplicado", res.log
    maes = {r[0] for r in con.execute(
        f"SELECT mae_id FROM playlists WHERE playlist_id IN ({','.join('?' * len(res.criadas))})", list(res.criadas.values())
    )}  # fmt: skip
    assert maes == {"PLmae"}
    assert all(not yt.playlists[p]["tracks"] for p in ANTIGAS)
    assert len(yt.playlists["PLmae"]["tracks"]) == 28


def test_cli_recategorizar_e_linhas_do_hierarquia(con, yt, monkeypatch):
    run = CliRunner().invoke
    r = run(app, ["reorganizar", "--mae", "Pesado", "--recategorizar", "--min-cluster", "6", "--plano"])
    assert r.exit_code == 0, r.output
    assert "filha(s) nova(s)" in r.output and "2 filha(s) atual(is) seriam esvaziadas" in r.output
    salvos = json.loads(
        con.execute("SELECT parametros FROM agrupamentos ORDER BY agrupamento_id DESC").fetchone()[0]
    )
    assert salvos["selecao"] == "leaf"
    monkeypatch.setattr(auth, "cliente", lambda: ClienteYT(yt, intervalo_s=0, dormir=lambda s: None))
    r = run(app, ["plano", "aplicar", "--sim", "--grande"])
    assert r.exit_code == 0, r.output
    assert "acrescente ao hierarquia.toml" in r.output and 'mae = "PLmae"' in r.output
