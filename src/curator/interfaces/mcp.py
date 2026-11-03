"""Servidor MCP (ADR-05 e ADR-10, M4): o Claude conversa com a biblioteca por estas ferramentas.

Divisão de papéis: a IA traduz o pedido numa receita (vocabulário fechado, conferido com o banco) e o
motor escolhe as músicas de forma determinística. Nenhuma ferramenta aplica nada no YouTube Music: o
máximo é criar um plano, que o usuário revisa e aplica com `curator plano aplicar`.

Cada ferramenta abre o banco de novo (leitura barata) para sempre ver o estado atual, inclusive o que o
worker de análise acabou de gravar. Nada aqui escreve no stdout: o stdio é o canal do protocolo.
"""

import json
from importlib import resources
from typing import Any, Literal

import numpy as np
from pydantic import ValidationError

from curator.banco import consultas, hierarquia
from curator.interfaces.comum import banco
from curator.motor import classificar as cl
from curator.motor import espacos, planos, receitas
from curator.motor.executor import executar
from curator.motor.receita import ReceitaInvalida, carregar, resolver_playlist
from curator.texto import normalizar

INSTRUCOES = """\
Você organiza a biblioteca do YouTube Music do usuário. Fluxo para um pedido de playlist:
1. obter_receita_por_pedido(pedido): se já existe receita para essa frase, reutilize (mesmo pedido → mesma playlist).
2. Monte uma RECEITA (JSON). Use só nomes que existem: descrever_vocabulario (features, gêneros-pai, estilos,
   humores), buscar_artistas / buscar_albuns (para exclusões exatas, ex.: "sem Hoyoverse" → buscar_artistas("hoyo")),
   arvore_playlists. Veja exemplos_de_receitas para os 6 pedidos de referência.
3. executar_receita(receita): o motor escolhe as músicas (determinístico). Se vier "erros", corrija com a sugestão.
   Se vierem POUCAS faixas, NÃO afrouxe filtros nem pesos por conta própria (isso faz o mesmo pedido dar playlists
   diferentes): mostre o resultado, diga quantas faixas ainda estão sem análise ('sem_analise') e ofereça o ajuste.
4. Mostre a lista, ajuste a receita SÓ se o usuário pedir, e salvar_receita(receita, pedido) quando ele gostar.
   Para pedidos parecidos com um exemplo de exemplos_de_receitas, parta do exemplo e mude só o que o pedido exige.
5. Só crie plano (criar_plano_*) se o usuário pedir. NUNCA diga que algo foi aplicado no YouTube Music: a aplicação
   é feita pelo usuário com `curator plano aplicar`.
Outros pedidos: "onde colocar as não alocadas" → classificar_nao_alocadas; "sugira playlists / reorganize" →
sugerir_reorganizacao (dê nomes melhores com renomear_cluster). Faixas ainda não analisadas ficam de fora de tudo
(o worker analisa ~500 por dia); diga isso quando 'sem_analise' for grande.
Escalas: features em 0..1 (bpm em BPM); valence/arousal 0..1; gêneros são probabilidades (0,3 já é forte no Discogs400).
"""


def _faixas(con, ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    out = {}
    for r in con.execute(
        f"SELECT video_id, titulo, artistas, album_nome FROM faixas WHERE video_id IN ({','.join('?' * len(ids))})",
        ids,
    ):
        out[r["video_id"]] = {
            "video_id": r["video_id"],
            "titulo": r["titulo"],
            "artistas": [a["nome"] for a in json.loads(r["artistas"])],
            "album": r["album_nome"],
        }
    return out


def _escopo(con, mae: str | None) -> str:
    if not mae:
        return "biblioteca"
    pid = resolver_playlist(con, mae)
    if pid is None:
        raise ValueError(f"playlist '{mae}' não encontrada; use arvore_playlists para ver os nomes")
    return pid


# ------------------------------------------------------------------ leitura (#31)


def arvore_playlists() -> list[dict]:
    """Playlists próprias com mãe, tipo (agregadora/folha), número de faixas e quantas já foram analisadas."""
    con = banco()
    analisadas = set(espacos.analisadas(con))
    out = []
    for p in con.execute(
        """SELECT p.playlist_id, p.titulo, p.tipo, m.titulo AS mae FROM playlists p
           LEFT JOIN playlists m ON m.playlist_id = p.mae_id WHERE p.propria = 1 ORDER BY p.titulo"""
    ):
        membros = {
            r[0]
            for r in con.execute(
                "SELECT video_id FROM playlist_faixas WHERE playlist_id = ?", (p["playlist_id"],)
            )
        }
        out.append({**dict(p), "faixas": len(membros), "analisadas": len(membros & analisadas)})
    return out


def estatisticas() -> dict:
    """Totais da biblioteca, não alocadas e andamento da análise de áudio."""
    est = consultas.estatisticas(banco())
    est.pop("playlists", None)  # está em arvore_playlists
    return est


def buscar_faixas(texto: str, limite: int = 20) -> list[dict]:
    """Faixas cujo título, artista ou álbum contém o texto (sem acento/caixa). Devolve video_id para usar como semente."""
    con = banco()
    q = normalizar(texto)
    analisadas = set(espacos.analisadas(con))
    achadas = []
    for r in con.execute(
        "SELECT video_id, titulo, artistas, album_nome FROM faixas ORDER BY coalesce(posicao_curtida, 1e9), video_id"
    ):
        alvo = normalizar(
            f"{r['titulo']} {' '.join(a['nome'] for a in json.loads(r['artistas']))} {r['album_nome'] or ''}"
        )
        if q in alvo:
            achadas.append(r["video_id"])
            if len(achadas) >= limite:
                break
    meta = _faixas(con, achadas)
    return [{**meta[v], "analisada": v in analisadas} for v in achadas]


def buscar_artistas(texto: str, limite: int = 20) -> list[dict]:
    """Artistas cujo nome contém o texto, com o nome EXATO do banco (use-o em excluir_artistas/exigir_artistas)."""
    con = banco()
    q = normalizar(texto)
    linhas = con.execute(
        """SELECT a.nome, count(fa.video_id) AS faixas FROM artistas a JOIN faixa_artistas fa USING (artista_id)
           GROUP BY a.artista_id ORDER BY faixas DESC, a.nome"""
    ).fetchall()
    return [dict(r) for r in linhas if q in normalizar(r["nome"])][:limite]


def buscar_albuns(texto: str, limite: int = 20) -> list[dict]:
    """Álbuns cujo nome contém o texto; use um trecho em excluir_albuns_contendo."""
    con = banco()
    q = normalizar(texto)
    linhas = con.execute(
        "SELECT album_nome AS album, count(*) AS faixas FROM faixas WHERE album_nome IS NOT NULL GROUP BY album_nome ORDER BY faixas DESC, album_nome"
    ).fetchall()
    return [dict(r) for r in linhas if q in normalizar(r["album"])][:limite]


def descrever_vocabulario(
    grupo: Literal[
        "features", "generos_pai", "estilos", "humores", "instrumentos", "generos_jamendo"
    ] = "features",
    filtro: str = "",
    limite: int = 60,
) -> dict:
    """Nomes válidos para a receita, com a distribuição na biblioteca analisada.

    features: p10/mediana/p90 de cada feature (para escolher alvos e limiares realistas).
    generos_pai / estilos: quantas faixas têm aquele gênero como dominante (Discogs400).
    humores / instrumentos / generos_jamendo: probabilidade média (Jamendo).
    `filtro` restringe por trecho do nome.
    """
    con = banco()
    f = normalizar(filtro)
    if grupo == "features":
        esc = espacos.escalares(con)
        out = []
        for i, c in enumerate(esc.colunas):
            x = esc.X[:, i]
            x = x[~np.isnan(x)]
            if f and f not in normalizar(c):
                continue
            out.append({"feature": c, **({"p10": round(float(np.percentile(x, 10)), 3), "mediana": round(float(np.median(x)), 3),
                                          "p90": round(float(np.percentile(x, 90)), 3)} if len(x) else {})})  # fmt: skip
        return {"grupo": grupo, "faixas_analisadas": len(esc.ids), "itens": out}
    if grupo in ("generos_pai", "estilos"):
        d = espacos.probabilidades(con, "discogs400")
        dominantes = [d.colunas[int(np.argmax(row))] for row in d.X] if len(d.ids) else []
        chave = (lambda r: r.split("---")[0]) if grupo == "generos_pai" else (lambda r: r)
        contagem: dict[str, int] = {}
        for r in dominantes:
            contagem[chave(r)] = contagem.get(chave(r), 0) + 1
        todos = sorted({chave(r) for r in d.colunas})
        itens = [
            {"nome": n, "faixas_dominante": contagem.get(n, 0)} for n in todos if not f or f in normalizar(n)
        ]
        itens.sort(key=lambda x: (-x["faixas_dominante"], x["nome"]))
        return {"grupo": grupo, "faixas_analisadas": len(d.ids), "itens": itens[:limite]}
    tipo = {"humores": "jamendo_mood", "instrumentos": "jamendo_instr", "generos_jamendo": "jamendo_genre"}[
        grupo
    ]
    m = espacos.probabilidades(con, tipo)
    media = m.X.mean(axis=0) if len(m.ids) else np.zeros(len(m.colunas))
    itens = [
        {"nome": r, "prob_media": round(float(media[i]), 3)}
        for i, r in enumerate(m.colunas)
        if not f or f in normalizar(r)
    ]
    itens.sort(key=lambda x: (-x["prob_media"], x["nome"]))
    return {"grupo": grupo, "faixas_analisadas": len(m.ids), "itens": itens[:limite]}


def exemplos_de_receitas() -> list[dict]:
    """As receitas de referência para os 6 pedidos do documento de decisões (few-shot)."""
    return json.loads(
        resources.files("curator.interfaces").joinpath("exemplos_receitas.json").read_text(encoding="utf-8")
    )


# ------------------------------------------------------------------ ação (#32)


def obter_receita_por_pedido(pedido: str) -> dict | None:
    """Receita já salva para a mesma frase (normalizada). Se existir, reutilize: é o que garante o mesmo resultado."""
    r = receitas.por_pedido(banco(), pedido)
    return r.model_dump(mode="json", by_alias=True) if r else None


def executar_receita(receita: dict[str, Any], limite_itens: int = 60) -> dict:
    """Valida e executa uma receita. Devolve as faixas escolhidas (com score e componentes) ou `erros` para corrigir."""
    con = banco()
    try:
        r = carregar(receita)
        ex = executar(con, r)
    except ValidationError as e:
        return {"erros": [f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()]}
    except ReceitaInvalida as e:
        return {"erros": e.erros}
    meta = _faixas(con, ex.ids[:limite_itens])
    return {
        "receita_hash": ex.receita_hash,
        "total": len(ex.ids),
        "universo": ex.universo,
        "sem_analise": ex.sem_analise,
        "apos_filtros": ex.apos_filtros,
        "descartes": dict(ex.descartes),
        "itens": [
            {**meta[i.video_id], "score": i.score, "componentes": i.componentes}
            for i in ex.itens[:limite_itens]
        ],
    }


def salvar_receita(receita: dict[str, Any], pedido: str | None = None) -> dict:
    """Salva a receita (substitui a de mesmo nome) e associa o pedido, para o cache por frase."""
    con = banco()
    try:
        r = carregar(receita)
        receitas.salvar(con, r, pedido)
    except ValidationError as e:
        return {"erros": [f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()]}
    except ReceitaInvalida as e:
        return {"erros": e.erros}
    return {"nome": r.nome, "hash": r.hash()}


def classificar_nao_alocadas(mae: str | None = None, limite: int = 60) -> dict:
    """Onde colocar as curtidas não alocadas (kNN + centroide, limiar por playlist). `mae` restringe às filhas dela."""
    con = banco()
    c = cl.classificar(con, mae=_escopo(con, mae) if mae else None)
    meta = _faixas(con, [d.video_id for d in c.decisoes[:limite]])
    return {
        "com_destino": sum(1 for d in c.decisoes if d.destinos),
        "total": len(c.decisoes),
        "sem_analise": c.sem_analise,
        "playlists_com_poucos_membros": [{"titulo": t, "analisados": n} for t, n in c.frias],
        "decisoes": [
            {
                **meta[d.video_id],
                "destinos": [vars(x) for x in d.destinos],
                "mais_proximas": [vars(x) for x in d.proximas],
                "ambigua": d.ambigua,
            }  # fmt: skip
            for d in c.decisoes[:limite]
        ],
    }


def sugerir_reorganizacao(mae: str | None = None, reagrupar: bool = False) -> dict:
    """Clusters sobrepostos da biblioteca (ou de uma mãe) e sugestões de criar, completar, fundir e dividir playlists."""
    from curator.motor import clusters, reorganizar

    con = banco()
    escopo = _escopo(con, mae)
    try:
        aid, cs, novos = clusters.obter(con, escopo, clusters.Parametros(), reagrupar)
    except clusters.PoucasFaixas as e:
        return {"erros": [str(e)]}
    sug = reorganizar.sugerir(con, cs, escopo)
    return {
        "agrupamento": aid,
        "faixas_novas_encaixadas": novos,
        "clusters": [
            {
                "cluster_id": c.cluster_id,
                "nome": c.nome,
                "faixas": len(c.membros),
                "estatisticas": c.estatisticas,
            }
            for c in cs
        ],
        "sugestoes": [{"tipo": s.tipo, "texto": s.texto} for s in sug],
    }


def renomear_cluster(cluster_id: int, nome: str, mae: str | None = None) -> dict:
    """Dá um nome melhor a um cluster (a partir das estatísticas). O nome sobrevive a reagrupamentos com o mesmo núcleo."""
    from curator.motor import clusters

    con = banco()
    aid = clusters.ultimo(con, _escopo(con, mae))
    if aid is None:
        return {"erros": ["nenhum agrupamento ainda; rode sugerir_reorganizacao"]}
    clusters.renomear(con, aid, cluster_id, nome, origem="ia")
    return {"agrupamento": aid, "cluster_id": cluster_id, "nome": nome}


def criar_plano_da_receita(nome_receita: str, playlist_destino: str | None = None) -> dict:
    """Plano (não aplicado) com o resultado de uma receita salva: playlist nova com o nome da receita, ou completa `playlist_destino`."""
    con = banco()
    r = receitas.obter(con, nome_receita)
    if r is None:
        return {"erros": [f"receita '{nome_receita}' não existe; use salvar_receita antes"]}
    destino = _escopo(con, playlist_destino) if playlist_destino else None
    ex, _ = receitas.executar_e_registrar(con, nome_receita)
    try:
        pid = planos.de_receita(con, nome_receita, ex.ids, {i.video_id: i.score for i in ex.itens}, destino)
    except ValueError as e:
        return {"erros": [str(e)]}
    return mostrar_plano(pid)


def criar_plano_da_classificacao(mae: str | None = None) -> dict:
    """Plano (não aplicado) adicionando as não alocadas às playlists sugeridas por classificar_nao_alocadas."""
    con = banco()
    c = cl.classificar(con, mae=_escopo(con, mae) if mae else None)
    try:
        return mostrar_plano(planos.de_classificacao(con, c.decisoes))
    except ValueError as e:
        return {"erros": [str(e)]}


def criar_plano_da_reorganizacao(mae: str | None = None) -> dict:
    """Plano (não aplicado) com as sugestões de criar e completar do último agrupamento."""
    from curator.motor import clusters, reorganizar

    con = banco()
    escopo = _escopo(con, mae)
    try:
        _, cs, _ = clusters.obter(con, escopo)
        return mostrar_plano(planos.de_reorganizacao(con, reorganizar.sugerir(con, cs, escopo), escopo))
    except (clusters.PoucasFaixas, ValueError) as e:
        return {"erros": [str(e)]}


def mostrar_plano(plano_id: int | None = None) -> dict:
    """O diff de um plano (o pendente mais recente, se não informar), agrupado por playlist."""
    con = banco()
    if plano_id is None:
        pend = planos.listar(con, "pendente")
        if not pend:
            return {"erros": ["nenhum plano pendente"]}
        plano_id = pend[0]["plano_id"]
    achado = planos.obter(con, plano_id)
    if achado is None:
        return {"erros": [f"plano #{plano_id} não existe"]}
    meta, ops = achado
    return {
        "plano_id": plano_id, "status": meta["status"], "origem": meta["origem"],
        "diff": planos.diff_legivel(con, ops),
        "como_aplicar": f"o usuário revisa e aplica no terminal: curator plano aplicar {plano_id}",
    }  # fmt: skip


def faltantes_nas_agregadoras() -> dict:
    """Faixas das filhas que ainda não estão na playlist-mãe agregadora."""
    con = banco()
    out = {}
    for mae, ids in hierarquia.faltantes_nas_agregadoras(con).items():
        titulo = con.execute("SELECT titulo FROM playlists WHERE playlist_id = ?", (mae,)).fetchone()[0]
        out[titulo] = len(ids)
    return out


FERRAMENTAS = [
    arvore_playlists, estatisticas, buscar_faixas, buscar_artistas, buscar_albuns, descrever_vocabulario,
    exemplos_de_receitas, obter_receita_por_pedido, executar_receita, salvar_receita, classificar_nao_alocadas,
    sugerir_reorganizacao, renomear_cluster, criar_plano_da_receita, criar_plano_da_classificacao,
    criar_plano_da_reorganizacao, mostrar_plano, faltantes_nas_agregadoras,
]  # fmt: skip

# Nenhuma destas existe de propósito: aplicar no YouTube Music é só pela CLI, com confirmação.
PROIBIDAS = {"aplicar_plano", "plano_aplicar", "desfazer_plano"}


def criar_servidor():
    from mcp.server.mcpserver import MCPServer

    from curator import __version__

    servidor = MCPServer(
        "yt-music-curator", instructions=INSTRUCOES, version=__version__, log_level="WARNING"
    )
    for f in FERRAMENTAS:
        servidor.tool()(f)
    return servidor
