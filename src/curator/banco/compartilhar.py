"""Compartilhar análises entre pessoas (#96): exportar as suas, importar as de outra pessoa, desfazer.

Baixar e analisar uma biblioteca leva dias. O pacote leva só o que é igual para todo mundo: os metadados públicos da
faixa, a análise (features, vetores e rótulos), os extras da #88 e os metadados externos (Deezer, MusicBrainz).

**Nunca vai:** curtidas e posição, avaliações (like/dislike), playlists, receitas, planos, recomendações e coletas
(as sementes revelam a biblioteca), clusters e projeções, fila, proxies e saídas, config e credenciais, o áudio, caminhos
locais (o `fonte_ref` da pasta local sai vazio) e as datas que mostram quando a faixa entrou na biblioteca. As faixas de
playlists ignoradas (#77) também ficam fora. A lista de faixas do pacote, sozinha, já mostra o que a pessoa escuta:
isso faz parte da ideia, e `so_biblioteca` restringe o escopo.

**Formato:** um zip com `manifesto.json` e `analises.db` (SQLite só com as tabelas de `TABELAS`). O manifesto traz as
versões, as contagens e o SHA-256 do banco.

**Importar** (o pacote é dado não confiável):
- Antes de gravar, confere o zip (só os dois arquivos, tamanho máximo), o SHA-256, o `integrity_check`, o esquema (só
  as tabelas e colunas esperadas, sem view nem trigger), os ids, a dimensão de cada vetor e os valores finitos.
  Qualquer falha recusa tudo.
- Só entra análise com a versão do analisador e o hash dos modelos atuais. Um extra entra só com o hash atual dele e
  sobre a mesma análise (mesma fonte de áudio). O resto é contado e descartado: seria refeito de qualquer jeito.
- Uma análise local válida (ok, versão atual) nunca é sobrescrita. Uma local com erro ou desatualizada é trocada.
- Nada de curtida, avaliação ou playlist: uma faixa importada continua NOVA para quem importa. Conhecida = curtida ∪
  playlist própria (`consultas.BIBLIOTECA`), e a base do z-score, das réguas e dos clusters é `espacos.da_biblioteca`.
- Cada linha guarda o `importacao_id` (migração 019). `remover` desfaz uma importação sem tocar no que foi feito aqui.
"""

import hashlib
import json
import re
import sqlite3
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from curator import __version__
from curator.audio import extras
from curator.audio.persistir import COLUNAS_FEATURES
from curator.banco import consultas
from curator.banco.conexao import agora

FORMATO = 1
MANIFESTO, BANCO = "manifesto.json", "analises.db"
MAX_BANCO = 4 * 1024**3  # 4 GiB descompactado (~25 KB por faixa: sobra para 100 mil faixas)
ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
FONTES_EXTERNAS = ("deezer", "musicbrainz")

# tabela → (colunas, chave primária). Nada de curtida, avaliação, tempos da máquina nem datas da biblioteca.
TABELAS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "faixas": (("video_id", "titulo", "artistas", "album_id", "album_nome", "duracao_s", "isrc", "explicita",
                "tipo_video", "visualizacoes", "publicada_em"), ("video_id",)),
    "albuns": (("album_id", "titulo", "ano", "descricao"), ("album_id",)),
    "artistas": (("artista_id", "nome"), ("artista_id",)),
    "faixa_artistas": (("video_id", "artista_id", "ordem"), ("video_id", "artista_id")),
    "analises": (("video_id", "versao_analisador", "hash_modelos", "fonte_audio", "fonte_ref", "score_casamento",
                  "duracao_analisada"), ("video_id",)),
    "features": (("video_id", *COLUNAS_FEATURES), ("video_id",)),
    "vetores": (("video_id", "tipo", "dim", "dados"), ("video_id", "tipo")),
    "rotulos_modelo": (("tipo", "indice", "rotulo"), ("tipo", "indice")),
    "analises_extras": (("video_id", "extra", "hash", "status", "fonte_ref", "erro"), ("video_id", "extra")),
    "externos_faixas": (("video_id", "fonte", "ref", "score", "dados", "status", "buscado_em"), ("video_id", "fonte")),
    "externos_entidades": (("fonte", "tipo", "ref", "nome", "dados", "buscado_em"), ("fonte", "tipo", "ref")),
    "externos_generos": (("video_id", "fonte", "nivel", "rotulo", "peso"), ("video_id", "fonte", "nivel", "rotulo")),
}  # fmt: skip
# o vetor que toda análise do núcleo tem (o motor não usa uma faixa sem ele)
VETOR_OBRIGATORIO = "effnet_emb"


class PacoteInvalido(ValueError):
    pass


def _ref_publica(col_ref: str, col_fonte: str) -> str:
    """O `fonte_ref` da pasta local é um caminho no disco de quem exportou: sai vazio."""
    return f"CASE WHEN {col_fonte} = 'pasta_local' THEN NULL ELSE {col_ref} END"


def _criar_esquema(con: sqlite3.Connection, esquema: str) -> None:
    for t, (cols, pk) in TABELAS.items():
        con.execute(f"CREATE TABLE {esquema}.{t} ({', '.join(cols)}, PRIMARY KEY ({', '.join(pk)}))")


def _sha256(caminho: Path) -> str:
    h = hashlib.sha256()
    with caminho.open("rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


# ------------------------------------------------------------------ exportar


@dataclass
class Exportacao:
    caminho: Path
    contagens: dict[str, int]
    bytes: int


def exportar(
    con: sqlite3.Connection,
    destino: Path,
    autor: str | None = None,
    so_biblioteca: bool = False,
    com_importadas: bool = False,
    versao_analisador: str | None = None,
    hash_modelos: str | None = None,
) -> Exportacao:
    """Gera o pacote em `destino` (.zip). Só lê o banco."""
    if versao_analisador is None or hash_modelos is None:
        from curator.audio.analisador import VERSAO_ANALISADOR
        from curator.audio.modelos import hash_modelos as hm

        versao_analisador, hash_modelos = VERSAO_ANALISADOR, hm()
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / BANCO
        con.execute("ATTACH DATABASE ? AS p", (str(db),))
        try:
            con.execute("BEGIN")  # uma foto só do banco, mesmo com o worker gravando
            try:
                contagens = _copiar_para_pacote(con, so_biblioteca, com_importadas)
            except BaseException:
                con.execute("ROLLBACK")
                raise
            con.execute("COMMIT")
        finally:
            con.execute("DETACH DATABASE p")
        manifesto = {
            "formato": FORMATO, "curator": __version__, "autor": autor or None,
            "exportado_em": datetime.now(UTC).strftime("%Y-%m-%d"),
            "versao_analisador": versao_analisador, "hash_modelos": hash_modelos,
            "extras": {n: extras.hash_extra(n) for n in extras.EXTRAS},
            "escopo": "biblioteca" if so_biblioteca else "tudo", "com_importadas": com_importadas,
            "contagens": contagens, "sha256": {BANCO: _sha256(db)},
        }  # fmt: skip
        parcial = destino.with_name(destino.name + ".parcial")
        with zipfile.ZipFile(parcial, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(MANIFESTO, json.dumps(manifesto, ensure_ascii=False, indent=2))
            z.write(db, BANCO)
        parcial.replace(destino)
    return Exportacao(destino, contagens, destino.stat().st_size)


def _copiar_para_pacote(con: sqlite3.Connection, so_biblioteca: bool, com_importadas: bool) -> dict[str, int]:
    _criar_esquema(con, "p")
    con.execute("DROP TABLE IF EXISTS temp.exp_ids")
    con.execute("CREATE TEMP TABLE exp_ids (video_id TEXT PRIMARY KEY)")
    escopo = f"AND a.video_id IN ({consultas.BIBLIOTECA})" if so_biblioteca else ""
    origem = "" if com_importadas else "AND a.importacao_id IS NULL"
    con.execute(
        f"""INSERT INTO temp.exp_ids SELECT a.video_id FROM main.analises a
            WHERE a.status = 'ok' AND a.video_id NOT IN ({consultas.IGNORADAS}) {origem} {escopo}
              AND EXISTS (SELECT 1 FROM main.vetores v WHERE v.video_id = a.video_id AND v.tipo = ?)""",
        (VETOR_OBRIGATORIO,),
    )
    cols = lambda t: ", ".join(TABELAS[t][0])  # noqa: E731
    dentro = "video_id IN (SELECT video_id FROM temp.exp_ids)"
    con.execute(
        f"INSERT INTO p.faixas SELECT {cols('faixas')} FROM main.faixas WHERE {dentro} ORDER BY video_id"
    )
    con.execute(
        f"INSERT INTO p.faixa_artistas SELECT {cols('faixa_artistas')} FROM main.faixa_artistas WHERE {dentro}"
    )
    con.execute(
        f"INSERT INTO p.artistas SELECT {cols('artistas')} FROM main.artistas "
        "WHERE artista_id IN (SELECT artista_id FROM p.faixa_artistas)"
    )
    con.execute(
        f"INSERT INTO p.albuns SELECT {cols('albuns')} FROM main.albuns "
        "WHERE album_id IN (SELECT album_id FROM p.faixas)"
    )
    ref = _ref_publica("fonte_ref", "fonte_audio")
    con.execute(
        f"""INSERT INTO p.analises SELECT video_id, versao_analisador, hash_modelos, fonte_audio, {ref},
                   score_casamento, duracao_analisada FROM main.analises WHERE {dentro} ORDER BY video_id"""
    )
    con.execute(f"INSERT INTO p.features SELECT {cols('features')} FROM main.features WHERE {dentro}")
    origem_x = "" if com_importadas else "AND e.importacao_id IS NULL"
    con.execute(
        f"""INSERT INTO p.analises_extras SELECT e.video_id, e.extra, e.hash, e.status,
                   {_ref_publica("e.fonte_ref", "a.fonte_audio")}, e.erro
            FROM main.analises_extras e JOIN main.analises a USING (video_id)
            WHERE e.{dentro} {origem_x}"""
    )
    # vetores do núcleo de toda análise exportada; os dos extras, só dos extras exportados
    marcas = ",".join("?" * len(extras.TIPOS_EXTRAS))
    con.execute(
        f"INSERT INTO p.vetores SELECT {cols('vetores')} FROM main.vetores WHERE {dentro} AND tipo NOT IN ({marcas})",
        extras.TIPOS_EXTRAS,
    )
    for nome, e in extras.EXTRAS.items():
        con.execute(
            f"""INSERT INTO p.vetores SELECT {cols("vetores")} FROM main.vetores v
                WHERE v.tipo IN ({",".join("?" * len(e.tipos))}) AND EXISTS (
                    SELECT 1 FROM p.analises_extras x WHERE x.video_id = v.video_id AND x.extra = ? AND x.status = 'ok')""",
            (*e.tipos, nome),
        )
    con.execute(
        f"INSERT INTO p.rotulos_modelo SELECT {cols('rotulos_modelo')} FROM main.rotulos_modelo "
        "WHERE tipo IN (SELECT DISTINCT tipo FROM p.vetores)"
    )
    origem_e = "" if com_importadas else "AND importacao_id IS NULL"
    con.execute(
        f"""INSERT INTO p.externos_faixas SELECT {cols("externos_faixas")} FROM main.externos_faixas
            WHERE {dentro} AND status IN ('ok', 'sem-casamento') {origem_e}"""
    )
    con.execute(
        f"""INSERT INTO p.externos_generos SELECT {cols("externos_generos")} FROM main.externos_generos g
            WHERE EXISTS (SELECT 1 FROM p.externos_faixas f WHERE f.video_id = g.video_id AND f.fonte = g.fonte)"""
    )
    con.execute(
        f"INSERT INTO p.externos_entidades SELECT {cols('externos_entidades')} FROM main.externos_entidades"
    )
    con.execute("DROP TABLE temp.exp_ids")
    return {t: con.execute(f"SELECT count(*) FROM p.{t}").fetchone()[0] for t in TABELAS}


# ------------------------------------------------------------------ conferir o pacote


@dataclass
class Pacote:
    manifesto: dict
    banco: Path  # cópia extraída, num diretório temporário
    sha256: str


def abrir_pacote(arquivo: Path, pasta_tmp: Path) -> Pacote:
    """Extrai e confere o pacote inteiro; levanta PacoteInvalido sem ter gravado nada no banco."""
    try:
        z = zipfile.ZipFile(arquivo)
    except (zipfile.BadZipFile, OSError) as e:
        raise PacoteInvalido(f"não é um pacote de análises (.zip): {e}") from e
    with z:
        nomes = sorted(i.filename for i in z.infolist())
        if nomes != sorted([MANIFESTO, BANCO]):
            raise PacoteInvalido(f"o pacote deve ter só {MANIFESTO} e {BANCO} (tem: {', '.join(nomes)})")
        info = z.getinfo(BANCO)
        if info.file_size > MAX_BANCO or z.getinfo(MANIFESTO).file_size > 1 << 20:
            raise PacoteInvalido("pacote grande demais")
        try:
            manifesto = json.loads(z.read(MANIFESTO))
        except ValueError as e:
            raise PacoteInvalido(f"{MANIFESTO} inválido: {e}") from e
        if not isinstance(manifesto, dict) or manifesto.get("formato") != FORMATO:
            raise PacoteInvalido(f"formato {manifesto.get('formato') if isinstance(manifesto, dict) else '?'} "
                                 f"desconhecido (este curator lê o formato {FORMATO})")  # fmt: skip
        destino = pasta_tmp / BANCO
        with z.open(info) as src, destino.open("wb") as out:
            lido = 0
            for bloco in iter(lambda: src.read(1 << 20), b""):
                lido += len(bloco)
                if lido > MAX_BANCO:
                    raise PacoteInvalido("pacote grande demais")
                out.write(bloco)
    sha = _sha256(destino)
    if (manifesto.get("sha256") or {}).get(BANCO) != sha:
        raise PacoteInvalido(
            f"o SHA-256 de {BANCO} não confere com o manifesto (arquivo alterado ou corrompido)"
        )
    _conferir_banco(destino, manifesto)
    return Pacote(manifesto, destino, sha)


def _conferir_banco(caminho: Path, manifesto: dict) -> None:
    con = sqlite3.connect(f"file:{caminho}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA trusted_schema = OFF")
        try:
            ic = con.execute("PRAGMA integrity_check").fetchall()
        except sqlite3.DatabaseError as e:
            raise PacoteInvalido(f"{BANCO} não é um SQLite válido: {e}") from e
        if ic != [("ok",)]:
            raise PacoteInvalido(f"{BANCO} corrompido: {ic[0][0]}")
        objetos = con.execute("SELECT type, name FROM sqlite_master").fetchall()
        tabelas = {n for t, n in objetos if t == "table"}
        estranhos = [f"{t} {n}" for t, n in objetos if t not in ("table", "index") or
                     (t == "index" and not n.startswith("sqlite_autoindex_"))]  # fmt: skip
        if estranhos or tabelas != set(TABELAS):
            extra = sorted(tabelas - set(TABELAS)) + estranhos
            raise PacoteInvalido(
                f"{BANCO} com esquema inesperado: {', '.join(extra or sorted(set(TABELAS) - tabelas))}"
            )
        for t, (cols, _) in TABELAS.items():
            atuais = tuple(r[1] for r in con.execute(f"PRAGMA table_info({t})"))
            if atuais != cols:
                raise PacoteInvalido(f"{BANCO}: colunas inesperadas em {t}")
        problemas = _conferir_conteudo(con)
        contagens = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABELAS}
        if contagens != manifesto.get("contagens"):
            problemas.append("as contagens não batem com o manifesto")
        if problemas:
            raise PacoteInvalido(f"{BANCO}: " + "; ".join(problemas[:5]))
    finally:
        con.close()


def _conferir_conteudo(con: sqlite3.Connection) -> list[str]:
    erros: list[str] = []
    for t, (cols, _) in TABELAS.items():
        if "video_id" in cols:
            ruins = [
                r[0]
                for r in con.execute(f"SELECT video_id FROM {t}")
                if not (isinstance(r[0], str) and ID.match(r[0]))
            ]
            if ruins:
                erros.append(f"{len(ruins)} video_id inválido(s) em {t}")
    for (artistas,) in con.execute("SELECT artistas FROM faixas"):
        try:
            ok = isinstance(json.loads(artistas), list)
        except TypeError, ValueError:
            ok = False
        if not ok:
            erros.append("faixas.artistas fora do formato")
            break
    rotulos = dict(con.execute("SELECT tipo, count(*) FROM rotulos_modelo GROUP BY tipo").fetchall())
    for vid, tipo, dim, dados in con.execute("SELECT video_id, tipo, dim, dados FROM vetores"):
        if not (isinstance(dim, int) and dim > 0 and isinstance(dados, bytes) and len(dados) == 4 * dim):
            erros.append(f"vetor {tipo} de {vid} com dimensão errada")
            break
        if tipo in rotulos and rotulos[tipo] != dim:
            erros.append(f"vetor {tipo} de {vid}: {dim} valores para {rotulos[tipo]} rótulos")
            break
        if not np.isfinite(np.frombuffer(dados, np.float32)).all():
            erros.append(f"vetor {tipo} de {vid} com NaN ou infinito")
            break
    numericas = [c for c in COLUNAS_FEATURES if c not in ("tom", "escala")]
    for linha in con.execute(f"SELECT video_id, {', '.join(numericas)} FROM features"):
        if any(v is not None and not (isinstance(v, (int, float)) and np.isfinite(v)) for v in linha[1:]):
            erros.append(f"features de {linha[0]} com valor não numérico, NaN ou infinito")
            break
    if con.execute(
        f"SELECT count(*) FROM analises_extras WHERE extra NOT IN ({','.join('?' * len(extras.EXTRAS))}) "
        "OR status NOT IN ('ok', 'erro')", tuple(extras.EXTRAS),
    ).fetchone()[0]:  # fmt: skip
        erros.append("extra desconhecido ou com status inválido")
    if con.execute(
        "SELECT count(*) FROM externos_faixas WHERE fonte NOT IN (?, ?) OR status NOT IN ('ok', 'sem-casamento')",
        FONTES_EXTERNAS,
    ).fetchone()[0]:
        erros.append("metadado externo de fonte ou status inválido")
    for sql, msg in (
        ("SELECT count(*) FROM analises WHERE video_id NOT IN (SELECT video_id FROM faixas)", "análise sem a faixa"),
        ("SELECT count(*) FROM faixas WHERE album_id IS NOT NULL AND album_id NOT IN (SELECT album_id FROM albuns)",
         "faixa com álbum que não veio no pacote"),
        ("SELECT count(*) FROM faixa_artistas WHERE artista_id NOT IN (SELECT artista_id FROM artistas) "
         "OR video_id NOT IN (SELECT video_id FROM faixas)", "artista de faixa que não veio no pacote"),
    ):  # fmt: skip
        if n := con.execute(sql).fetchone()[0]:
            erros.append(f"{n} {msg}")
    return erros


# ------------------------------------------------------------------ importar


@dataclass
class Importacao:
    importacao_id: int | None
    entrou: Counter = field(default_factory=Counter)
    fora: Counter = field(default_factory=Counter)  # motivo → quantas
    seco: bool = False
    de_novo: bool = False  # o pacote já tinha sido importado antes


def importar(
    con: sqlite3.Connection,
    arquivo: Path,
    seco: bool = False,
    versao_analisador: str | None = None,
    hash_modelos: str | None = None,
) -> Importacao:
    """Confere o pacote e importa numa transação só. `seco` faz tudo e desfaz no fim (só conta)."""
    if versao_analisador is None or hash_modelos is None:
        from curator.audio.analisador import VERSAO_ANALISADOR
        from curator.audio.modelos import hash_modelos as hm

        versao_analisador, hash_modelos = VERSAO_ANALISADOR, hm()
    arquivo = Path(arquivo)
    with tempfile.TemporaryDirectory() as tmp:
        pac = abrir_pacote(arquivo, Path(tmp))
        con.execute("ATTACH DATABASE ? AS p", (str(pac.banco),))
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                res = _importar(con, pac, arquivo.name, versao_analisador, hash_modelos)
            except BaseException:
                con.execute("ROLLBACK")
                raise
            # nada entrou (tudo já analisado aqui, outra versão do analisador…): nada muda, nem o registro
            vazio = not any(res.entrou.values())
            con.execute("ROLLBACK" if seco or vazio else "COMMIT")
        finally:
            con.execute("DETACH DATABASE p")
    res.seco = seco
    if seco or vazio:
        res.importacao_id = None
    return res


def _importar(con: sqlite3.Connection, pac: Pacote, arquivo: str, versao: str, hash_mod: str) -> Importacao:
    _conferir_rotulos(con)
    m = pac.manifesto
    momento = agora()
    # o mesmo pacote de novo (depois de atualizar o analisador, por exemplo) só traz o que faltava, na mesma importação
    antes = con.execute("SELECT importacao_id FROM importacoes WHERE sha256 = ?", (pac.sha256,)).fetchone()
    iid = antes[0] if antes else con.execute(
        "INSERT INTO importacoes (autor, arquivo, sha256, exportado_em, importado_em, contagens) VALUES (?, ?, ?, ?, ?, '{}')",
        (str(m.get("autor"))[:200] if m.get("autor") else None, arquivo[:200], pac.sha256,
         str(m.get("exportado_em") or "")[:10] or None, momento),
    ).lastrowid  # fmt: skip
    res = Importacao(iid, de_novo=antes is not None)
    con.execute("DROP TABLE IF EXISTS temp.imp_an")
    con.execute("CREATE TEMP TABLE imp_an (video_id TEXT PRIMARY KEY)")
    total = con.execute("SELECT count(*) FROM p.analises").fetchone()[0]
    con.execute(
        """INSERT INTO temp.imp_an SELECT a.video_id FROM p.analises a
           WHERE a.versao_analisador = ? AND a.hash_modelos = ?
             AND EXISTS (SELECT 1 FROM p.features f WHERE f.video_id = a.video_id)
             AND EXISTS (SELECT 1 FROM p.vetores v WHERE v.video_id = a.video_id AND v.tipo = ?)
             AND NOT EXISTS (SELECT 1 FROM main.analises l WHERE l.video_id = a.video_id AND l.status = 'ok'
                             AND l.versao_analisador = ? AND l.hash_modelos = ?)""",
        (versao, hash_mod, VETOR_OBRIGATORIO, versao, hash_mod),
    )
    n = con.execute("SELECT count(*) FROM temp.imp_an").fetchone()[0]
    versao_dif = con.execute(
        "SELECT count(*) FROM p.analises WHERE NOT (versao_analisador = ? AND hash_modelos = ?)",
        (versao, hash_mod),
    ).fetchone()[0]
    locais = con.execute(
        """SELECT count(*) FROM p.analises a JOIN main.analises l USING (video_id)
           WHERE l.status = 'ok' AND l.versao_analisador = ? AND l.hash_modelos = ?
             AND a.versao_analisador = ? AND a.hash_modelos = ?""",
        (versao, hash_mod, versao, hash_mod),
    ).fetchone()[0]
    res.fora["análise de outra versão do analisador ou dos modelos"] = versao_dif
    res.fora["já analisada aqui (a sua vale)"] = locais
    res.fora["análise incompleta no pacote"] = total - n - versao_dif - locais

    # faixas novas para este banco: só os metadados públicos, nunca curtida, avaliação ou playlist
    con.execute(
        """INSERT OR IGNORE INTO main.albuns (album_id, titulo, ano, descricao)
           SELECT album_id, titulo, ano, descricao FROM p.albuns WHERE album_id IN (
               SELECT album_id FROM p.faixas WHERE video_id IN (SELECT video_id FROM temp.imp_an))"""
    )
    cols_f = TABELAS["faixas"][0]
    novas = con.execute(
        f"""INSERT INTO main.faixas ({", ".join(cols_f)}, criada_em, atualizada_em, importacao_id)
            SELECT {", ".join("f." + c for c in cols_f)}, ?, ?, ? FROM p.faixas f
            WHERE f.video_id IN (SELECT video_id FROM temp.imp_an)
              AND NOT EXISTS (SELECT 1 FROM main.faixas l WHERE l.video_id = f.video_id)""",
        (momento, momento, iid),
    ).rowcount
    res.entrou["faixas novas neste banco"] = novas
    con.execute("INSERT OR IGNORE INTO main.artistas SELECT artista_id, nome FROM p.artistas")
    con.execute(
        "INSERT OR IGNORE INTO main.faixa_artistas SELECT video_id, artista_id, ordem FROM p.faixa_artistas "
        "WHERE video_id IN (SELECT video_id FROM main.faixas WHERE importacao_id = ?)",
        (iid,),
    )
    # análise local com erro ou desatualizada: sai inteira (com os extras, que dependem dela) e entra a do pacote
    dentro = "video_id IN (SELECT video_id FROM temp.imp_an)"
    con.execute(f"DELETE FROM main.vetores WHERE {dentro}")
    con.execute(f"DELETE FROM main.analises_extras WHERE {dentro}")
    con.execute(
        f"""INSERT OR REPLACE INTO main.analises (video_id, status, versao_analisador, hash_modelos, fonte_audio,
                   fonte_ref, score_casamento, duracao_analisada, analisada_em, importacao_id)
            SELECT video_id, 'ok', versao_analisador, hash_modelos, fonte_audio, fonte_ref, score_casamento,
                   duracao_analisada, ?, ? FROM p.analises WHERE {dentro}""",
        (momento, iid),
    )
    cols_ft = ", ".join(TABELAS["features"][0])
    con.execute(
        f"INSERT OR REPLACE INTO main.features ({cols_ft}) SELECT {cols_ft} FROM p.features WHERE {dentro}"
    )
    marcas = ",".join("?" * len(extras.TIPOS_EXTRAS))
    con.execute(
        f"INSERT INTO main.vetores SELECT video_id, tipo, dim, dados FROM p.vetores WHERE {dentro} "
        f"AND tipo NOT IN ({marcas})",
        extras.TIPOS_EXTRAS,
    )
    con.execute("INSERT OR IGNORE INTO main.rotulos_modelo SELECT tipo, indice, rotulo FROM p.rotulos_modelo")
    res.entrou["análises"] = n
    # quem estava na fila não precisa mais baixar
    con.execute(f"UPDATE main.fila_download SET status = 'ok', atualizada_em = ? WHERE {dentro} "
                "AND status IN ('pendente', 'erro')", (momento,))  # fmt: skip

    _importar_extras(con, iid, momento, res)
    _importar_externos(con, iid, res)
    con.execute("UPDATE importacoes SET contagens = ? WHERE importacao_id = ?",
                (json.dumps({"entrou": dict(res.entrou), "fora": {k: v for k, v in res.fora.items() if v}},
                            ensure_ascii=False), iid))  # fmt: skip
    con.execute("DROP TABLE temp.imp_an")
    return res


def _conferir_rotulos(con: sqlite3.Connection) -> None:
    """Os rótulos de cada tipo do pacote precisam ser os mesmos daqui (mesmo modelo → mesmos rótulos)."""
    for (tipo,) in con.execute("SELECT DISTINCT tipo FROM p.rotulos_modelo").fetchall():
        aqui = con.execute(
            "SELECT indice, rotulo FROM main.rotulos_modelo WHERE tipo = ? ORDER BY indice", (tipo,)
        ).fetchall()
        la = con.execute(
            "SELECT indice, rotulo FROM p.rotulos_modelo WHERE tipo = ? ORDER BY indice", (tipo,)
        ).fetchall()
        if aqui and [tuple(r) for r in aqui] != [tuple(r) for r in la]:
            raise PacoteInvalido(f"os rótulos de {tipo} no pacote não são os do seu analisador")


def _importar_extras(con: sqlite3.Connection, iid: int, momento: str, res: Importacao) -> None:
    for nome, e in extras.EXTRAS.items():
        h = extras.hash_extra(nome)
        res.fora[f"{nome}: outra versão do extra"] += con.execute(
            "SELECT count(*) FROM p.analises_extras WHERE extra = ? AND hash != ?", (nome, h)
        ).fetchone()[0]
        # só sobre a mesma análise (mesma fonte de áudio), e nunca por cima de um extra atual daqui
        con.execute("DROP TABLE IF EXISTS temp.imp_ex")
        con.execute(
            """CREATE TEMP TABLE imp_ex AS SELECT x.video_id FROM p.analises_extras x
               JOIN p.analises pa USING (video_id) JOIN main.analises l USING (video_id)
               WHERE x.extra = ? AND x.hash = ? AND l.status = 'ok'
                 AND l.versao_analisador = pa.versao_analisador AND l.hash_modelos = pa.hash_modelos
                 AND l.fonte_ref IS pa.fonte_ref AND x.fonte_ref IS pa.fonte_ref
                 AND NOT EXISTS (SELECT 1 FROM main.analises_extras m
                                 WHERE m.video_id = x.video_id AND m.extra = x.extra AND m.hash = x.hash)
                 AND (x.status != 'ok' OR (SELECT count(*) FROM p.vetores v
                                           WHERE v.video_id = x.video_id AND v.tipo IN ({})) = ?)""".format(
                ",".join("?" * len(e.tipos))
            ),
            (nome, h, *e.tipos, len(e.tipos)),
        )
        dentro = "video_id IN (SELECT video_id FROM temp.imp_ex)"
        tipos = ",".join("?" * len(e.tipos))
        con.execute(f"DELETE FROM main.vetores WHERE {dentro} AND tipo IN ({tipos})", e.tipos)
        n = con.execute(
            f"""INSERT OR REPLACE INTO main.analises_extras (video_id, extra, hash, status, fonte_ref, erro,
                       analisada_em, importacao_id)
                SELECT video_id, extra, hash, status, fonte_ref, erro, ?, ? FROM p.analises_extras
                WHERE extra = ? AND {dentro}""",
            (momento, iid, nome),
        ).rowcount
        con.execute(
            f"INSERT INTO main.vetores SELECT video_id, tipo, dim, dados FROM p.vetores WHERE {dentro} AND tipo IN ({tipos})",
            e.tipos,
        )
        res.entrou[f"extras {nome}"] = n
        con.execute("DROP TABLE temp.imp_ex")


def _importar_externos(con: sqlite3.Connection, iid: int, res: Importacao) -> None:
    cols = TABELAS["externos_faixas"][0]
    n = con.execute(
        f"""INSERT INTO main.externos_faixas ({", ".join(cols)}, tentativas, importacao_id)
            SELECT {", ".join("x." + c for c in cols)}, 0, ? FROM p.externos_faixas x
            WHERE x.video_id IN (SELECT video_id FROM main.faixas)
              AND NOT EXISTS (SELECT 1 FROM main.externos_faixas l WHERE l.video_id = x.video_id AND l.fonte = x.fonte)""",
        (iid,),
    ).rowcount
    con.execute(
        """INSERT OR IGNORE INTO main.externos_generos SELECT g.video_id, g.fonte, g.nivel, g.rotulo, g.peso
           FROM p.externos_generos g JOIN main.externos_faixas f ON f.video_id = g.video_id AND f.fonte = g.fonte
           WHERE f.importacao_id = ?""",
        (iid,),
    )
    con.execute(
        "INSERT OR IGNORE INTO main.externos_entidades SELECT fonte, tipo, ref, nome, dados, buscado_em FROM p.externos_entidades"
    )
    res.entrou["metadados externos"] = n


# ------------------------------------------------------------------ listar e desfazer


def listar(con: sqlite3.Connection) -> list[dict]:
    out = []
    for r in con.execute("SELECT * FROM importacoes ORDER BY importacao_id"):
        d = dict(r)
        d["contagens"] = json.loads(d["contagens"])
        d["analises_hoje"] = con.execute(
            "SELECT count(*) FROM analises WHERE importacao_id = ?", (r["importacao_id"],)
        ).fetchone()[0]
        out.append(d)
    return out


def importadas(con: sqlite3.Connection) -> set[str]:
    """Faixas cuja análise veio de outra pessoa."""
    return {r[0] for r in con.execute("SELECT video_id FROM analises WHERE importacao_id IS NOT NULL")}


def remover(con: sqlite3.Connection, importacao_id: int) -> dict[str, int]:
    """Desfaz uma importação: apaga o que veio dela e nada do que foi feito aqui. As faixas da sua biblioteca que
    perdem a análise voltam para a fila no próximo `analisar`."""
    if not con.execute("SELECT 1 FROM importacoes WHERE importacao_id = ?", (importacao_id,)).fetchone():
        raise KeyError(f"importação #{importacao_id} não existe")
    from curator.banco.conexao import transacao

    out: dict[str, int] = {}
    with transacao(con):
        an = "video_id IN (SELECT video_id FROM analises WHERE importacao_id = :i)"
        # a análise importada sai com os extras que dependem dela
        con.execute(f"DELETE FROM vetores WHERE {an}", {"i": importacao_id})
        con.execute(f"DELETE FROM features WHERE {an}", {"i": importacao_id})
        con.execute(f"DELETE FROM analises_extras WHERE {an}", {"i": importacao_id})
        con.execute(f"DELETE FROM fila_download WHERE status = 'ok' AND {an}", {"i": importacao_id})
        out["análises"] = con.execute(
            "DELETE FROM analises WHERE importacao_id = ?", (importacao_id,)
        ).rowcount
        # extras importados sobre análises daqui
        for nome, e in extras.EXTRAS.items():
            tipos = ",".join("?" * len(e.tipos))
            con.execute(
                f"""DELETE FROM vetores WHERE tipo IN ({tipos}) AND video_id IN (
                        SELECT video_id FROM analises_extras WHERE extra = ? AND importacao_id = ?)""",
                (*e.tipos, nome, importacao_id),
            )
        out["extras"] = con.execute(
            "DELETE FROM analises_extras WHERE importacao_id = ?", (importacao_id,)
        ).rowcount
        con.execute(
            """DELETE FROM externos_generos WHERE EXISTS (SELECT 1 FROM externos_faixas f WHERE f.importacao_id = ?
                   AND f.video_id = externos_generos.video_id AND f.fonte = externos_generos.fonte)""",
            (importacao_id,),
        )
        out["metadados externos"] = con.execute(
            "DELETE FROM externos_faixas WHERE importacao_id = ?", (importacao_id,)
        ).rowcount
        # a faixa criada pela importação sai se nada daqui a usa (curtida, avaliação, playlist, fila, recomendação…)
        out["faixas"] = con.execute(
            """DELETE FROM faixas WHERE importacao_id = ? AND curtida = 0 AND avaliacao = 'neutra'
                 AND NOT EXISTS (SELECT 1 FROM playlist_faixas x WHERE x.video_id = faixas.video_id)
                 AND NOT EXISTS (SELECT 1 FROM recomendacao_itens x WHERE x.video_id = faixas.video_id)
                 AND NOT EXISTS (SELECT 1 FROM deezer_casamentos x WHERE x.video_id = faixas.video_id)
                 AND NOT EXISTS (SELECT 1 FROM fila_download x WHERE x.video_id = faixas.video_id)
                 AND NOT EXISTS (SELECT 1 FROM audios_guardados x WHERE x.video_id = faixas.video_id)
                 AND NOT EXISTS (SELECT 1 FROM analises x WHERE x.video_id = faixas.video_id)
                 AND NOT EXISTS (SELECT 1 FROM analises_extras x WHERE x.video_id = faixas.video_id)
                 AND NOT EXISTS (SELECT 1 FROM externos_faixas x WHERE x.video_id = faixas.video_id)""",
            (importacao_id,),
        ).rowcount
        con.execute("UPDATE faixas SET importacao_id = NULL WHERE importacao_id = ?", (importacao_id,))
        con.execute("DELETE FROM importacoes WHERE importacao_id = ?", (importacao_id,))
    return out
