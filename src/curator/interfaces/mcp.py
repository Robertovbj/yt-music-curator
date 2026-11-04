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
RECOMENDAÇÕES (músicas que o usuário ainda não curtiu; veja exemplos_de_recomendacoes):
- Antes de chamar qualquer coisa, saiba (1) o ESCOPO: só curtidas (receita), só músicas novas (recomendar_novas)
  ou misturar (recomendar_misturando_curtidas, SÓ se o usuário pediu para misturar); e (2) o MODO: rápido (sem
  baixar nada, em segundos) ou lento (baixa e analisa as candidatas; leva horas, o worker precisa rodar).
- Na MENOR dúvida sobre escopo ou modo, PERGUNTE ao usuário antes de chamar. "Me recomende uma playlist pra
  dirigir" não diz o escopo: pergunte. "Parecidas com a música X / a playlist Y" = músicas novas.
- Recomendações NUNCA entram nas playlists das curtidas: criar_plano_da_recomendacao cria uma playlist nova;
  playlist_destino + permitir_novas só quando o usuário pediu isso com todas as letras.
- O histórico (historico_recomendacoes) mostra o que já foi sugerido e o que virou curtida ou dislike.
PLAYLISTS EXISTENTES do usuário seguem a lógica dele (uma por jogo, por artista...): NUNCA as use como gabarito
e NUNCA sugira mexer numa delas sem ele nomear qual. Por isso:
- "onde colocar as não alocadas" → classificar_nao_alocadas SÓ com as playlists que ele nomeou (playlists=[...]
  ou mae=...), ou nas_gerais=True se ele falar das gerais. Se ele não disse quais, PERGUNTE.
- "sugira playlists" / "organize minhas curtidas" sem citar playlist → sugerir_playlists_gerais (clusters de todas
  as curtidas, ignorando as playlists que existem; o plano só cria playlists novas). Dê nomes melhores com
  renomear_cluster(gerais=True) a partir das estatísticas, antes de criar_plano_das_gerais.
- "reorganize a playlist X" → sugerir_reorganizacao(mae=X). Faixas ainda não analisadas ficam de fora de tudo
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
           LEFT JOIN playlists m ON m.playlist_id = p.mae_id
           WHERE p.propria = 1 AND p.ignorada = 0 ORDER BY p.titulo"""
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
    """Faixas cujo título, artista ou álbum contém o texto (sem acento/caixa). Devolve video_id para usar como semente.

    `dislike`: o usuário não gosta; nunca use como semente nem sugira. `na_biblioteca`: curtida ou numa playlist dele."""
    con = banco()
    q = normalizar(texto)
    analisadas = set(espacos.analisadas(con))
    achadas = []
    for r in con.execute(
        f"SELECT video_id, titulo, artistas, album_nome FROM faixas WHERE video_id NOT IN ({consultas.IGNORADAS}) "
        "ORDER BY coalesce(posicao_curtida, 1e9), video_id"
    ):
        alvo = normalizar(
            f"{r['titulo']} {' '.join(a['nome'] for a in json.loads(r['artistas']))} {r['album_nome'] or ''}"
        )
        if q in alvo:
            achadas.append(r["video_id"])
            if len(achadas) >= limite:
                break
    meta = _faixas(con, achadas)
    biblioteca = set(consultas.biblioteca(con))
    dislikes = consultas.com_dislike(con)
    return [
        {**meta[v], "analisada": v in analisadas, "na_biblioteca": v in biblioteca, "dislike": v in dislikes}
        for v in achadas
    ]


def buscar_artistas(texto: str, limite: int = 20) -> list[dict]:
    """Artistas cujo nome contém o texto, com o nome EXATO do banco (use-o em excluir_artistas/exigir_artistas)."""
    con = banco()
    q = normalizar(texto)
    linhas = con.execute(
        f"""SELECT a.nome, count(fa.video_id) AS faixas FROM artistas a JOIN faixa_artistas fa USING (artista_id)
            WHERE fa.video_id NOT IN ({consultas.IGNORADAS}) GROUP BY a.artista_id ORDER BY faixas DESC, a.nome"""
    ).fetchall()
    return [dict(r) for r in linhas if q in normalizar(r["nome"])][:limite]


def buscar_albuns(texto: str, limite: int = 20) -> list[dict]:
    """Álbuns cujo nome contém o texto; use um trecho em excluir_albuns_contendo."""
    con = banco()
    q = normalizar(texto)
    linhas = con.execute(
        f"SELECT album_nome AS album, count(*) AS faixas FROM faixas WHERE album_nome IS NOT NULL "
        f"AND video_id NOT IN ({consultas.IGNORADAS}) GROUP BY album_nome ORDER BY faixas DESC, album_nome"
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


def classificar_nao_alocadas(
    playlists: list[str] | None = None, mae: str | None = None, nas_gerais: bool = False, limite: int = 60
) -> dict:
    """Em quais das playlists NOMEADAS pelo usuário colocar curtidas (kNN + centroide, limiar por playlist).

    Exige um alvo: `playlists` (ids ou títulos), `mae` (as filhas dela) ou `nas_gerais` (as playlists gerais).
    Com playlists/mae, as curtidas não alocadas; com nas_gerais, as curtidas que ainda não estão em nenhuma geral."""
    con = banco()
    try:
        c = cl.classificar(con, mae=_escopo(con, mae) if mae else None,
                           em=[_escopo(con, x) for x in playlists or []], nas_gerais=nas_gerais)  # fmt: skip
    except (cl.SemAlvo, ValueError) as e:
        return {"erros": [str(e)]}
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


def sugerir_playlists_gerais(reagrupar: bool = False, min_cluster: int | None = None) -> dict:
    """Playlists gerais a partir de TODAS as curtidas, ignorando as playlists existentes (clusters sobrepostos).

    `min_cluster` menor dá mais playlists (padrão 40). Dê nomes com renomear_cluster(gerais=True) antes do plano."""
    from curator.motor import clusters, gerais

    con = banco()
    try:
        aid, cs, novos = gerais.obter(con, gerais.parametros(min_cluster), reagrupar)
    except clusters.PoucasFaixas as e:
        return {"erros": [str(e)]}
    return {
        "agrupamento": aid,
        "faixas_novas_encaixadas": novos,
        "curtidas": len(clusters._universo(con, clusters.CURTIDAS)),
        "cobertas": len({v for c in cs for v in c.membros}),
        "playlists": [
            {
                "cluster_id": c.cluster_id,
                "nome": c.nome,
                "faixas": len(c.membros),
                "estatisticas": c.estatisticas,
            }
            for c in sorted(cs, key=lambda c: (-len(c.membros), c.cluster_id))
        ],
    }


def criar_plano_das_gerais(min_cluster: int | None = None) -> dict:
    """Plano (não aplicado) que CRIA as playlists gerais do último agrupamento. Não toca nenhuma playlist existente."""
    from curator.motor import clusters, gerais

    con = banco()
    try:
        aid, cs, _ = gerais.obter(con, gerais.parametros(min_cluster))
        return mostrar_plano(gerais.plano(con, aid, cs))
    except (clusters.PoucasFaixas, ValueError) as e:
        return {"erros": [str(e)]}


def sugerir_reorganizacao(mae: str, reagrupar: bool = False) -> dict:
    """Dentro da playlist `mae` que o usuário nomeou: clusters e sugestões de criar, completar, fundir e dividir."""
    from curator.motor import clusters, reorganizar

    con = banco()
    try:
        escopo = _escopo(con, mae)
    except ValueError as e:
        return {"erros": [str(e)]}
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


def renomear_cluster(cluster_id: int, nome: str, mae: str | None = None, gerais: bool = False) -> dict:
    """Dá um nome melhor a um cluster (a partir das estatísticas). O nome sobrevive a reagrupamentos com o mesmo núcleo.

    `gerais=True` para os clusters de sugerir_playlists_gerais; `mae` para os de sugerir_reorganizacao."""
    from curator.motor import clusters

    con = banco()
    aid = clusters.ultimo(con, clusters.CURTIDAS if gerais else _escopo(con, mae))
    if aid is None:
        return {"erros": ["nenhum agrupamento ainda; rode sugerir_playlists_gerais ou sugerir_reorganizacao"]}
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


def criar_plano_da_classificacao(
    playlists: list[str] | None = None, mae: str | None = None, nas_gerais: bool = False
) -> dict:
    """Plano (não aplicado) com as sugestões de classificar_nao_alocadas, com os MESMOS alvos nomeados."""
    con = banco()
    try:
        c = cl.classificar(con, mae=_escopo(con, mae) if mae else None,
                           em=[_escopo(con, x) for x in playlists or []], nas_gerais=nas_gerais)  # fmt: skip
        return mostrar_plano(planos.de_classificacao(con, c.decisoes))
    except (cl.SemAlvo, ValueError) as e:
        return {"erros": [str(e)]}


def criar_plano_da_reorganizacao(mae: str) -> dict:
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


# ------------------------------------------------------------------ recomendações (#69, ADR-13)


def exemplos_de_recomendacoes() -> list[dict]:
    """Como decidir entre receita, recomendar_novas e recomendar_misturando_curtidas (e quando perguntar)."""
    return json.loads(
        resources.files("curator.interfaces")
        .joinpath("exemplos_recomendacoes.json")
        .read_text(encoding="utf-8")
    )


def _cliente_yt():
    from curator.coleta.cliente import abrir_cliente
    from curator.config import caminhos, carregar_config

    return abrir_cliente(caminhos().credenciais, carregar_config()["metadados"]["intervalo_s"])


def _cliente_deezer():
    from curator.coleta.deezer import ClienteDeezer

    return ClienteDeezer()


def _recomendar(escopo: str, modo: str | None, pedido: str | None, **campos) -> dict:
    from curator.coleta.candidatas import SementeInvalida
    from curator.coleta.cliente import CredencialAusente
    from curator.config import carregar_config
    from curator.motor import recomendar as rr

    if modo not in ("rapido", "lento"):
        return {"erros": [
            "modo não informado: CONFIRME com o usuário se quer a recomendação 'rapido' (sem baixar nada, em "
            "segundos) ou 'lento' (baixa e analisa as candidatas antes; leva horas) e chame de novo"
        ]}  # fmt: skip
    cfg = carregar_config()
    try:
        p = rr.Pedido.model_validate(
            {"modo": modo, "escopo": escopo, **{k: v for k, v in campos.items() if v not in (None, [], {})}}
        )
    except ValidationError as e:
        return {"erros": [f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()]}
    con = banco()
    try:
        cli = _cliente_yt()
        dz = _cliente_deezer() if "deezer" in p.com_padroes(cfg).fontes else None
        res = rr.recomendar(con, cli, dz, p, cfg, pedido)
    except (SementeInvalida, CredencialAusente) as e:
        return {"erros": [str(e)]}
    if res.status != "pronta":
        return {
            "recomendacao_id": res.recomendacao_id, "status": res.status, "pendentes": res.pendentes,
            "proximo_passo": "as candidatas estão na fila de análise (depois da biblioteca); chame "
                             "concluir_recomendacao mais tarde. O worker `curator analisar` precisa estar rodando.",
        }  # fmt: skip
    return _mostrar(con, res.recomendacao_id, dict(res.descartes))


def _mostrar(con, rid: int, descartes: dict | None = None) -> dict:
    from curator.motor import recomendacoes as rc

    r = rc.obter(con, rid)
    if r is None:
        return {"erros": [f"recomendação #{rid} não existe"]}
    return {
        "recomendacao_id": rid, "status": r["status"], "modo": r["modo"], "escopo": r["escopo"],
        "pedido": r["pedido"], "criada_em": r["criada_em"], **({"descartes": descartes} if descartes else {}),
        "itens": [
            {"posicao": i["posicao"], "video_id": i["video_id"], "titulo": i["titulo"], "artistas": i["artistas"],
             "da_biblioteca": i["conhecida"], "fontes": i["fontes"], "votos": i["votos"],
             "ja_sugerida_antes": i["vezes_antes"], "status": i["status"]}
            for i in rc.itens(con, rid)
        ],
    }  # fmt: skip


def recomendar_novas(
    modo: Literal["rapido", "lento"] | None = None,
    faixas: list[str] | None = None,
    playlist: str | None = None,
    cluster: int | None = None,
    artista: str | None = None,
    tamanho: int | None = None,
    excluir_artistas: list[str] | None = None,
    perfil_alvo: dict[str, Any] | None = None,
    generos_preferidos: dict[str, float] | None = None,
    pedido: str | None = None,
) -> dict:
    """Recomenda SÓ músicas novas (fora da biblioteca) parecidas com as sementes: faixas (video_ids de
    buscar_faixas), uma playlist, um cluster ou um artista. `modo` é obrigatório: se o usuário não disse rápido
    ou lento, PERGUNTE antes. perfil_alvo/generos_preferidos (como na receita) só no modo lento."""
    return _recomendar("novas", modo, pedido, faixas=faixas, playlist=playlist, cluster=cluster, artista=artista,
                       tamanho=tamanho, excluir_artistas=excluir_artistas, perfil_alvo=perfil_alvo,
                       generos_preferidos=generos_preferidos)  # fmt: skip


def recomendar_misturando_curtidas(
    modo: Literal["rapido", "lento"] | None = None,
    proporcao_novas: float = 0.5,
    faixas: list[str] | None = None,
    playlist: str | None = None,
    cluster: int | None = None,
    artista: str | None = None,
    tamanho: int | None = None,
    excluir_artistas: list[str] | None = None,
    perfil_alvo: dict[str, Any] | None = None,
    generos_preferidos: dict[str, float] | None = None,
    pedido: str | None = None,
) -> dict:
    """Mistura músicas novas com as da biblioteca (proporcao_novas = fração de novas). Use SÓ quando o usuário
    pediu explicitamente para misturar. `modo` é obrigatório: na dúvida, PERGUNTE."""
    return _recomendar("misturar", modo, pedido, proporcao_novas=proporcao_novas, faixas=faixas, playlist=playlist,
                       cluster=cluster, artista=artista, tamanho=tamanho, excluir_artistas=excluir_artistas,
                       perfil_alvo=perfil_alvo, generos_preferidos=generos_preferidos)  # fmt: skip


def concluir_recomendacao(recomendacao_id: int, parcial: bool = False) -> dict:
    """Fecha uma recomendação lenta quando as análises terminam (`parcial`: já, com o que estiver analisado)."""
    from curator.config import carregar_config
    from curator.motor import lento

    con = banco()
    try:
        res = lento.concluir(con, recomendacao_id, carregar_config(), parcial)
    except KeyError as e:
        return {"erros": [str(e)]}
    if res.status != "pronta":
        return {"recomendacao_id": recomendacao_id, "status": res.status, "pendentes": res.pendentes}
    return _mostrar(con, recomendacao_id)


def mostrar_recomendacao(recomendacao_id: int) -> dict:
    """Uma recomendação salva, com o status atual de cada faixa (curtida, dislike, em_playlist, sem_reacao)."""
    return _mostrar(banco(), recomendacao_id)


def historico_recomendacoes(limite: int = 10) -> list[dict]:
    """Recomendações já feitas (mais recentes primeiro) e quantas faixas de cada viraram curtida ou dislike."""
    from curator.motor import recomendacoes as rc

    return [
        {"recomendacao_id": r["recomendacao_id"], "criada_em": r["criada_em"], "pedido": r["pedido"],
         "modo": r["modo"], "escopo": r["escopo"], "status": r["status"], "status_itens": r["status_itens"]}
        for r in rc.listar(banco(), limite)
    ]  # fmt: skip


def criar_plano_da_recomendacao(
    recomendacao_id: int,
    nome: str | None = None,
    playlist_destino: str | None = None,
    permitir_novas: bool = False,
) -> dict:
    """Plano (não aplicado) com uma recomendação: por padrão, uma playlist NOVA "Descobertas · …".
    playlist_destino + permitir_novas=true acrescenta numa playlist existente: SÓ se o usuário pediu isso."""
    con = banco()
    destino = _escopo(con, playlist_destino) if playlist_destino else None
    try:
        return mostrar_plano(planos.de_recomendacao(con, recomendacao_id, nome, destino, permitir_novas))
    except ValueError as e:
        return {"erros": [str(e)]}


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
    sugerir_playlists_gerais, criar_plano_das_gerais, sugerir_reorganizacao, renomear_cluster, criar_plano_da_receita, criar_plano_da_classificacao,
    criar_plano_da_reorganizacao, mostrar_plano, faltantes_nas_agregadoras, exemplos_de_recomendacoes,
    recomendar_novas, recomendar_misturando_curtidas, concluir_recomendacao, mostrar_recomendacao,
    historico_recomendacoes, criar_plano_da_recomendacao,
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
