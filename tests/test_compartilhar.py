"""#96: exportar as análises para outra pessoa e importar as dela, sem nada pessoal e sem mexer no que é "conhecida"."""

import json
import sqlite3
import zipfile
from pathlib import Path

import numpy as np
import pytest

from curator.audio import extras
from curator.audio.extras import ResultadoExtra
from curator.audio.persistir import Origem, gravar_analise
from curator.banco import compartilhar as cp
from curator.banco import consultas, vetores
from curator.banco.conexao import abrir, agora
from curator.config import caminhos, carregar_config
from curator.motor import espacos, generos
from curator.motor import recomendacoes as rc
from curator.motor import recomendar as rr
from curator.motor.executor import executar
from curator.motor.receita import carregar
from tests.falso_motor import biblioteca_analisada, playlist

V, H = "1", "h"  # a versão do analisador e o hash dos modelos da biblioteca sintética
SEGREDOS = (
    "Playlist Secreta",
    "10.0.0.1",
    "senha-do-proxy",
    "/home/eu/segredo",
    "pedido secreto",
    "Ignorada X",
)


def _extra_ok(nome):
    tipos = extras.EXTRAS[nome].tipos
    return ResultadoExtra(extras.hash_extra(nome), {t: np.full(4, 0.25, np.float32) for t in tipos},
                          {tipos[0]: ["a", "b", "c", "d"]}, 0.1)  # fmt: skip


@pytest.fixture
def meu():
    """Quem exporta: biblioteca de 6 por estilo, com tudo o que é pessoal e não pode sair."""
    con = biblioteca_analisada(caminhos().banco, por_estilo=6)
    playlist(con, "PLsec", "Playlist Secreta", ["rock00", "rap01"])
    playlist(con, "PLign", "Ignorada X", ["ambient05"])
    con.execute("UPDATE playlists SET ignorada = 1 WHERE playlist_id = 'PLign'")
    con.execute("UPDATE faixas SET curtida = 0 WHERE video_id = 'ambient05'")  # só na playlist ignorada
    con.execute("UPDATE faixas SET avaliacao = 'dislike' WHERE video_id = 'rap05'")
    con.execute(
        "INSERT INTO proxies (nome, url, cota_diaria, ativo) VALUES ('vpn', 'socks5://eu:senha-do-proxy@10.0.0.1:1080', 10, 1)"
    )
    con.execute(
        "INSERT INTO audios_guardados (video_id, caminho, provedor, ref, score, exato, parcial, bytes, guardado_em) "
        "VALUES ('rock01', '/home/eu/segredo/rock01.webm', 'youtube_music', 'rock01', 1, 1, 0, 10, '2026-11-05')"
    )
    con.execute(
        "UPDATE analises SET fonte_audio = 'pasta_local', fonte_ref = '/home/eu/segredo/rock02.flac' WHERE video_id = 'rock02'"
    )
    from curator.motor import receitas

    receitas.salvar(con, carregar({"nome": "Minha", "tamanho": 5}), "pedido secreto")
    for vid in ("rock03", "rock04", "rap03"):
        extras.gravar(con, vid, {n: _extra_ok(n) for n in extras.EXTRAS}, vid)
    extras.gravar(
        con, "rock05", {"maest": ResultadoExtra("hash-antigo", {}, {}, 0.1, "erro antigo")}, "rock05"
    )
    for vid in ("rock00", "rock03"):
        con.execute(
            "INSERT INTO externos_faixas (video_id, fonte, ref, score, dados, status, buscado_em) "
            "VALUES (?, 'deezer', '123', 1.0, '{\"isrc\": \"X\"}', 'ok', ?)", (vid, agora()),
        )  # fmt: skip
        con.execute("INSERT INTO externos_generos VALUES (?, 'deezer', 'album', 'rock', 1.0)", (vid,))
    con.execute(
        "INSERT INTO externos_entidades VALUES ('deezer', 'album', '9', 'Álbum', '{}', ?)", (agora(),)
    )
    return con


@pytest.fixture
def amigo(tmp_path):
    """Quem importa: a mesma biblioteca sintética, menor (00 a 02 de cada estilo), noutro banco."""
    return biblioteca_analisada(tmp_path / "amigo.db", por_estilo=3, semente=1)


@pytest.fixture
def pacote(meu, tmp_path):
    return cp.exportar(meu, tmp_path / "pacote.zip", autor="eu", versao_analisador=V, hash_modelos=H)


def importar(con, arquivo, **kw):
    return cp.importar(con, arquivo, versao_analisador=V, hash_modelos=H, **kw)


def _ler_pacote(arquivo: Path, tmp_path: Path) -> tuple[dict, sqlite3.Connection, bytes]:
    with zipfile.ZipFile(arquivo) as z:
        m = json.loads(z.read(cp.MANIFESTO))
        dados = z.read(cp.BANCO)
    (tmp_path / "lido.db").write_bytes(dados)
    return m, sqlite3.connect(tmp_path / "lido.db"), dados


# ------------------------------------------------------------------ exportar


def test_o_pacote_nao_leva_nada_pessoal(pacote, tmp_path):
    m, pc, dados = _ler_pacote(pacote.caminho, tmp_path)
    tabelas = {r[0] for r in pc.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tabelas == set(cp.TABELAS)
    cols = {r[1] for r in pc.execute("PRAGMA table_info(faixas)")}
    assert not cols & {
        "curtida",
        "posicao_curtida",
        "avaliacao",
        "avaliacao_em",
        "criada_em",
        "importacao_id",
    }
    texto = dados + json.dumps(m).encode()
    for s in SEGREDOS:
        assert s.encode() not in texto, s
    ids = {r[0] for r in pc.execute("SELECT video_id FROM analises")}
    assert "ambient05" not in ids  # só numa playlist ignorada (#77): fora de tudo
    assert pc.execute("SELECT fonte_ref FROM analises WHERE video_id = 'rock02'").fetchone()[0] is None
    assert m["autor"] == "eu" and m["contagens"]["analises"] == len(ids)


def test_escopo_so_biblioteca_e_importadas_fora_por_padrao(meu, tmp_path):
    meu.execute(
        "INSERT INTO faixas (video_id, titulo, artistas, criada_em, atualizada_em) VALUES ('cand', 'C', '[]', ?, ?)",
        (agora(), agora()),
    )
    gravar_analise(meu, "cand", _resultado(meu, "rock00"), Origem("youtube_music", "cand"), V, H, 1.0)
    tudo = cp.exportar(meu, tmp_path / "a.zip", versao_analisador=V, hash_modelos=H)
    bib = cp.exportar(meu, tmp_path / "b.zip", so_biblioteca=True, versao_analisador=V, hash_modelos=H)
    assert tudo.contagens["analises"] == bib.contagens["analises"] + 1


def _resultado(con, vid):
    from curator.audio.analisador import Resultado

    feats = dict(con.execute("SELECT energy, bpm FROM features WHERE video_id = ?", (vid,)).fetchone())
    emb = vetores.ler(con, vid, "effnet_emb")
    probs = vetores.ler(con, vid, "discogs400")
    rot = [
        r[0]
        for r in con.execute("SELECT rotulo FROM rotulos_modelo WHERE tipo = 'discogs400' ORDER BY indice")
    ]
    return Resultado(feats, {"effnet_emb": emb, "discogs400": probs}, {"discogs400": rot}, 200.0, {})


# ------------------------------------------------------------------ importar


def test_importa_as_que_faltam_e_a_local_vence(amigo, pacote, meu):
    antes = {v: vetores.ler(amigo, v, "effnet_emb").tobytes() for v in ("rock00", "rock01")}
    res = importar(amigo, pacote.caminho)
    # 18 menos ambient05 (só numa playlist ignorada) = 17 no pacote; 00 a 02 já são do amigo. rap05 (dislike
    # meu) entra: o dislike não vai no pacote
    assert res.entrou["análises"] == 8
    assert res.fora["já analisada aqui (a sua vale)"] == 9
    for v, b in antes.items():
        assert vetores.ler(amigo, v, "effnet_emb").tobytes() == b
        assert (
            amigo.execute("SELECT importacao_id FROM analises WHERE video_id = ?", (v,)).fetchone()[0] is None
        )
    assert np.array_equal(
        vetores.ler(amigo, "rock04", "effnet_emb"), vetores.ler(meu, "rock04", "effnet_emb")
    )


def test_importada_continua_nova_e_nada_do_motor_muda(amigo, pacote):
    cfg = carregar_config()
    cid = rc.salvar_coleta(amigo, "yt_radio", "rock00", ["rock04", "rock01"])
    p = rr.Pedido(modo="rapido", escopo="novas", faixas=["rock00"], fontes=["yt_radio"]).com_padroes(cfg)
    receita = carregar({"nome": "x", "tamanho": 20, "perfil_alvo": {"energy": {"alvo": 0.8}}})

    def foto():
        esc = espacos.escalares(amigo).subconjunto(espacos.da_biblioteca(amigo)).X
        afin = generos.Afinidades(amigo)
        return {
            "biblioteca": consultas.biblioteca(amigo),
            "da_biblioteca": espacos.da_biblioteca(amigo),
            "zscore": [np.round(x, 9).tolist() for x in espacos.zscore(esc)[1:]],
            "reguas": {k: (r.referencia, r.nucleo) for k, r in afin.reguas.items()},
            "receita": [(i.video_id, i.score) for i in executar(amigo, receita).itens],
            "conhecidas": [
                i.video_id for i in rr.candidatas(amigo, p, cfg, ["rock00"], [cid], conhecidas=True).itens
            ],
        }

    antes = foto()
    importar(amigo, pacote.caminho)
    assert foto() == antes
    novas = [i.video_id for i in rr.candidatas(amigo, p, cfg, ["rock00"], [cid]).itens]
    assert novas == ["rock04"]  # a importada aparece como NOVA
    assert tuple(
        amigo.execute("SELECT curtida, avaliacao FROM faixas WHERE video_id = 'rock04'").fetchone()
    ) == (0, "neutra")
    assert not amigo.execute("SELECT 1 FROM playlist_faixas WHERE video_id = 'rock04'").fetchone()


def test_versao_diferente_e_descartada_e_o_pacote_pode_voltar_depois(amigo, pacote):
    res = cp.importar(amigo, pacote.caminho, versao_analisador="2", hash_modelos=H)
    assert res.entrou["análises"] == 0
    assert res.fora["análise de outra versão do analisador ou dos modelos"] == 17
    assert res.entrou["metadados externos"] == 1  # não depende do analisador: o da rock00, que o amigo tem
    de_novo = importar(amigo, pacote.caminho)  # depois, com a versão certa, entram as análises
    assert de_novo.de_novo and de_novo.importacao_id == res.importacao_id
    assert de_novo.entrou["análises"] == 8 and de_novo.entrou["metadados externos"] == 1


def test_analise_local_desatualizada_e_trocada_e_a_fila_fecha(amigo, pacote):
    amigo.execute("UPDATE analises SET hash_modelos = 'velho' WHERE video_id = 'rock01'")
    amigo.execute(
        "INSERT INTO fila_download (video_id, status, criada_em, atualizada_em) VALUES ('rock01', 'pendente', ?, ?)",
        (agora(), agora()),
    )
    importar(amigo, pacote.caminho)
    a = amigo.execute("SELECT hash_modelos, importacao_id FROM analises WHERE video_id = 'rock01'").fetchone()
    assert a[0] == H and a[1] is not None
    assert amigo.execute("SELECT status FROM fila_download WHERE video_id = 'rock01'").fetchone()[0] == "ok"


def test_extras_e_externos_entram_so_com_a_versao_atual(amigo, pacote):
    res = importar(amigo, pacote.caminho)
    assert res.entrou["extras maest"] == 3 and res.fora["maest: outra versão do extra"] == 1
    assert vetores.ler(amigo, "rock03", "maest519") is not None
    assert (
        amigo.execute("SELECT count(*) FROM externos_faixas WHERE importacao_id IS NOT NULL").fetchone()[0]
        == 2
    )
    assert amigo.execute("SELECT count(*) FROM externos_generos").fetchone()[0] == 2


def test_o_mesmo_pacote_de_novo_nao_muda_nada(amigo, pacote):
    importar(amigo, pacote.caminho)
    antes = amigo.execute("SELECT count(*) FROM analises").fetchone()[0]
    res = importar(amigo, pacote.caminho)
    assert res.de_novo and not any(res.entrou.values()) and res.importacao_id is None
    assert amigo.execute("SELECT count(*) FROM analises").fetchone()[0] == antes
    assert len(cp.listar(amigo)) == 1


def test_seco_conta_e_nao_grava(amigo, pacote):
    res = importar(amigo, pacote.caminho, seco=True)
    assert res.entrou["análises"] == 8 and res.importacao_id is None
    assert amigo.execute("SELECT count(*) FROM analises").fetchone()[0] == 9
    assert amigo.execute("SELECT count(*) FROM importacoes").fetchone()[0] == 0


def test_remover_desfaz_so_o_que_veio_da_importacao(amigo, pacote):
    tabelas = (
        "faixas",
        "analises",
        "features",
        "vetores",
        "analises_extras",
        "externos_faixas",
        "externos_generos",
    )
    antes = {t: amigo.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tabelas}
    vetor_local = vetores.ler(amigo, "rock00", "effnet_emb").tobytes()
    res = importar(amigo, pacote.caminho)
    amigo.execute("UPDATE faixas SET curtida = 1 WHERE video_id = 'rock05'")  # curtida depois: a faixa fica
    c = cp.remover(amigo, res.importacao_id)
    assert c["análises"] == 8
    depois = {t: amigo.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tabelas}
    assert depois == {**antes, "faixas": antes["faixas"] + 1}
    assert vetores.ler(amigo, "rock00", "effnet_emb").tobytes() == vetor_local
    assert amigo.execute("SELECT importacao_id FROM faixas WHERE video_id = 'rock05'").fetchone()[0] is None
    assert cp.listar(amigo) == []
    with pytest.raises(KeyError):
        cp.remover(amigo, res.importacao_id)


def test_reanalise_local_tira_a_marca_da_importacao(amigo, pacote):
    importar(amigo, pacote.caminho)
    gravar_analise(amigo, "rock04", _resultado(amigo, "rock04"), Origem("youtube_music", "rock04"), V, H, 1.0)
    assert amigo.execute("SELECT importacao_id FROM analises WHERE video_id = 'rock04'").fetchone()[0] is None
    assert "rock04" not in cp.importadas(amigo)


# ------------------------------------------------------------------ pacote adulterado


def _refazer(arquivo: Path, tmp_path: Path, mexer, conservar_sha=False) -> Path:
    with zipfile.ZipFile(arquivo) as z:
        m = json.loads(z.read(cp.MANIFESTO))
        (tmp_path / "x.db").write_bytes(z.read(cp.BANCO))
    c = sqlite3.connect(tmp_path / "x.db")
    mexer(c)
    c.commit()
    m["contagens"] = {
        t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in cp.TABELAS if _existe(c, t)
    }
    c.close()
    if not conservar_sha:
        m["sha256"][cp.BANCO] = cp._sha256(tmp_path / "x.db")
    novo = tmp_path / "adulterado.zip"
    with zipfile.ZipFile(novo, "w") as z:
        z.writestr(cp.MANIFESTO, json.dumps(m))
        z.write(tmp_path / "x.db", cp.BANCO)
    return novo


def _existe(c, t):
    return c.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (t,)).fetchone() is not None


@pytest.mark.parametrize(
    ("mexer", "msg"),
    [
        (lambda c: c.execute("UPDATE features SET energy = 0.5"), "SHA-256"),
        (lambda c: c.execute("CREATE TABLE playlists (x)"), "esquema inesperado"),
        (lambda c: c.execute("CREATE VIEW v AS SELECT 1"), "esquema inesperado"),
        (
            lambda c: c.execute("CREATE TRIGGER t AFTER INSERT ON faixas BEGIN SELECT 1; END"),
            "esquema inesperado",
        ),
        (
            lambda c: c.execute("UPDATE vetores SET dim = dim + 1 WHERE tipo = 'effnet_emb'"),
            "dimensão errada",
        ),
        (
            lambda c: c.execute(
                "UPDATE vetores SET dados = ? WHERE tipo = 'effnet_emb'",
                (np.full(32, np.nan, np.float32).tobytes(),),
            ),
            "NaN",
        ),  # fmt: skip
        (
            lambda c: c.execute("UPDATE faixas SET video_id = '../../x' WHERE video_id = 'rock03'"),
            "video_id inválido",
        ),
        (lambda c: c.execute("UPDATE features SET energy = 'muito'"), "não numérico"),
    ],
)
def test_pacote_adulterado_e_recusado_sem_gravar_nada(amigo, pacote, tmp_path, mexer, msg):
    ruim = _refazer(pacote.caminho, tmp_path, mexer, conservar_sha=(msg == "SHA-256"))
    antes = amigo.execute("SELECT count(*) FROM analises").fetchone()[0]
    with pytest.raises(cp.PacoteInvalido, match=msg):
        importar(amigo, ruim)
    assert amigo.execute("SELECT count(*) FROM analises").fetchone()[0] == antes
    assert amigo.execute("SELECT count(*) FROM importacoes").fetchone()[0] == 0


def test_zip_com_arquivo_a_mais_ou_que_nao_e_zip(amigo, pacote, tmp_path):
    outro = tmp_path / "outro.zip"
    with zipfile.ZipFile(pacote.caminho) as z, zipfile.ZipFile(outro, "w") as o:
        for n in z.namelist():
            o.writestr(n, z.read(n))
        o.writestr("../fora.sh", "rm -rf /")
    with pytest.raises(cp.PacoteInvalido, match="só"):
        importar(amigo, outro)
    (tmp_path / "texto.zip").write_text("oi")
    with pytest.raises(cp.PacoteInvalido, match="não é um pacote"):
        importar(amigo, tmp_path / "texto.zip")


# ------------------------------------------------------------------ CLI


def test_cli_exporta_confere_e_lista(meu, tmp_path):
    from typer.testing import CliRunner

    from curator.interfaces.cli import app

    run = CliRunner()
    r = run.invoke(app, ["analises", "exportar", "--saida", str(tmp_path / "p.zip")])
    assert r.exit_code == 0, r.output
    assert "Não vai: curtidas" in r.output
    amigo_db = tmp_path / "amigo-cli"
    amigo_db.mkdir()
    abrir(amigo_db / "curator.db").close()
    r = run.invoke(app, ["analises", "importar", str(tmp_path / "p.zip"), "--seco"])
    assert r.exit_code == 0, r.output  # no próprio banco: tudo já analisado aqui ou de outra versão
    assert "nada a importar" in r.output and "outra versão" in r.output
    assert run.invoke(app, ["analises", "importadas"]).output.strip() == "nenhuma importação"
    assert run.invoke(app, ["analises", "remover", "7"]).exit_code != 0
