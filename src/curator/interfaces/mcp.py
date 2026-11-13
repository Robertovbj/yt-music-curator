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
from curator.motor import espacos, generos, planos, receitas
from curator.motor.executor import executar
from curator.motor.receita import ReceitaInvalida, carregar, resolver_playlist
from curator.texto import normalizar

INSTRUCOES = """\
Você organiza a biblioteca do YouTube Music do usuário. Fluxo para um pedido de playlist:
1. obter_receita_por_pedido(pedido): se já existe receita para essa frase, reutilize (mesmo pedido → mesma playlist).
2. Monte uma RECEITA (JSON). Use só nomes que existem: descrever_vocabulario (features, gêneros-pai, estilos,
   humores), examinar_genero (fronteira de um gênero-pai), buscar_artistas / buscar_albuns (para exclusões exatas, ex.: "sem Hoyoverse" → buscar_artistas("hoyo")),
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
- "separe minhas curtidas em N playlists" / "playlists master" → sugerir_playlists_gerais(quantas=N) e o
  criar_plano_das_gerais com o mesmo quantas (#105).
- "reorganize a playlist X" → sugerir_reorganizacao(mae=X). Faixas ainda não analisadas ficam de fora de tudo
(o worker analisa ~500 por dia); diga isso quando 'sem_analise' for grande.
Escalas: features em 0..1 (bpm em BPM); valence/arousal 0..1. Estilos, humores e instrumentos são saídas dos
modelos (0..1, cada uma isolada; um estilo raramente passa de 0,5). genero_pai_min é outra régua: a afinidade
RELATIVA ao núcleo do gênero na biblioteca (1,0 = tão típica quanto a faixa mediana do núcleo; 0,7 amplo, 0,8
normal, 1,0 estrito), igual para todos os gêneros. O examinar_genero mostra ao lado a afinidade_com_maest (ADR-15) e,
nos gêneros que o usuário rotulou, a afinidade_aprendida com os rótulos DELE (ADR-17). A receita usa a v2, salvo nos
gêneros em que ele adotou a aprendida (regua.aprendida.na_receita): compare na fronteira, e um rotulo_do_usuario vale
mais que qualquer régua. aprendida_nao_julga = o modelo dele não opina naquela faixa (longe do que foi rotulado).
RÓTULOS DE GÊNERO (#93, #101): NÃO proponha sessões de rotular faixa a faixa (o usuário não quer). Os rótulos nascem
no uso: quando ele disser, vendo uma playlist ou a fronteira, que uma faixa é ou não é de um gênero ("isso não é
rock"), grave com rotular_genero. SÓ a resposta dele: nunca rotule pelo que você acha, nem a partir de uma playlist.
faixas_para_rotular só se ELE pedir para ensinar um gênero.
Antes de fixar o limite, chame examinar_genero(pai, limite) e olhe as faixas logo acima e logo abaixo do corte:
use o que você sabe dos artistas e as outras_visoes (MAEST, Rapping/Singing do YAMNet, gêneros do MusicBrainz e do
Deezer) para subir ou descer o limite (é a receita que guarda o número, então o resultado continua reprodutível). Pedido "só com vocal"/"sem lo-fi":
filtros.max.instrumental 0,5. Pop e Electronic quase não se separam pelo áudio (estes modelos chamam a produção
pop de Electronic): para eles prefira perfil_alvo, sementes e estilos específicos a genero_pai_min.
POPULARIDADE (visualizações de todo o YouTube, não as do usuário): `popularidade` é o percentil na biblioteca, de
0 (lado B) a 1 (as mais vistas). Na receita, vale em perfil_alvo e em filtros.min/max, como uma feature. Nas
recomendações, use o campo `popularidade` {"alvo"} e `publicadas_desde` (lançamentos). "Só a versão de estúdio",
"sem clipe", "sem cover" → filtros.tipos_video ["faixa"]. Veja a cobertura em descrever_vocabulario("features"):
sem dados, ofereça `curator sincronizar` em vez de usar.
MARCAÇÕES (#107): etiquetas que o USUÁRIO dá a artistas ("k-pop", "vtuber", "pop-ocidental"…), com exceção por
faixa. O áudio não separa pop ocidental de J-pop nem K-pop de pop; a marcação separa. São dados para você usar
conforme o pedido: filtros.exigir_marcacoes / excluir_marcacoes e marcacoes_preferidas (fator) na receita. Ex.:
"J-pop sem K-pop" → excluir_marcacoes ["k-pop"]. Veja as que existem em descrever_vocabulario("marcacoes") ou
listar_marcacoes; buscar_faixas mostra as de cada faixa. Quando ele disser que um artista É de uma categoria
("a Moona é VTuber"), grave com marcar_artista; uma faixa só ("essa do HoYo-MiX é K-pop"), com marcar_faixa. SÓ a
resposta dele: nunca marque pelo que você acha. Artista que faz de tudo (HoYo-MiX) não recebe etiqueta: só a faixa.
As regras de destino (etiqueta → master) ficam no config.toml dele; classificar_nao_alocadas(nas_gerais=True) e
criar_plano_das_marcacoes as aplicam.
ESCRITA (#111): cada faixa tem a escrita do título, do álbum e dos artistas (japonesa, coreana, chinesa, cirilica,
latina), herdada do artista quando a faixa não tem escrita própria ("MONSTER" da Reol é japonesa). O som não diz o
idioma: um cluster que soa como J-pop junta pop ocidental, eletrônica de jogo e nightcore. Por isso, quando o pedido
for de J-pop, J-rock, anime ou K-pop, use a escrita: filtros.exigir_escritas ["japonesa"] (ou ["coreana"]) na
receita, ou escritas_preferidas como fator quando o pedido for mais solto; nas recomendações, exigir_escritas. Junte
com o gênero (J-rock = escrita japonesa + Rock) e com as marcações do usuário, que valem mais que a escrita. É um
sinal, não um rótulo: artista japonês que só usa títulos em inglês fica "latina". buscar_faixas mostra a escrita de
cada faixa; descrever_vocabulario("escritas") dá a distribuição. Ao nomear clusters, só cite idioma ou mercado
(J-pop, K-pop…) se estatisticas.escritas sustentar (maioria das faixas); senão, descreva o som.
FAIXAS IGNORADAS (#100): se o usuário disser que uma faixa não conta ("é meme", "desconsidere", "não é música"),
use ignorar_faixa(video_id, motivo) com as palavras dele; desfazer=True volta atrás. NUNCA ignore por conta própria.
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

    `dislike`: o usuário não gosta; nunca use como semente nem sugira. `na_biblioteca`: curtida ou numa playlist dele.
    `importada`: a análise veio de outra pessoa (#96); fora da biblioteca, é uma música NOVA para o usuário."""
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
    uso = _dados_de_uso(con, achadas)
    from curator.banco.compartilhar import importadas
    from curator.motor import escrita, marcacoes

    de_fora = importadas(con)
    marcas = marcacoes.da_faixa(con, achadas)
    escritas = escrita.calcular(con, achadas)
    return [
        {**meta[v], "analisada": v in analisadas, "na_biblioteca": v in biblioteca, "dislike": v in dislikes,
         **({"importada": True} if v in de_fora else {}), **({"marcacoes": sorted(marcas[v])} if v in marcas else {}),
         **({"escritas": sorted(escritas[v])} if escritas.get(v) else {}), **uso[v]}
        for v in achadas
    ]  # fmt: skip


def _dados_de_uso(con, ids: list[str]) -> dict[str, dict]:
    """Tipo do vídeo, visualizações, percentil na biblioteca e data de publicação (#85/#87), só o que existir."""
    from curator.motor import popularidade

    pct = popularidade.percentis(con, ids)
    tipo = popularidade.tipos(con, ids)
    out = {v: {} for v in ids}
    for r in con.execute(
        f"SELECT video_id, visualizacoes, publicada_em FROM faixas WHERE video_id IN ({','.join('?' * len(ids))})",
        ids,
    ):
        out[r["video_id"]] = {k: v for k, v in {
            "tipo_video": tipo[r["video_id"]], "visualizacoes": r["visualizacoes"], "publicada_em": r["publicada_em"],
        }.items() if v is not None}  # fmt: skip
    for v, p in zip(ids, pct, strict=True):
        if not np.isnan(p):
            out[v]["popularidade"] = round(float(p), 3)
    return out


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
        "features",
        "generos_pai",
        "estilos",
        "humores",
        "instrumentos",
        "generos_jamendo",
        "marcacoes",
        "escritas",
    ] = "features",
    filtro: str = "",
    limite: int = 60,
) -> dict:
    """Nomes válidos para a receita, com a distribuição na biblioteca analisada.

    features: p10/mediana/p90 de cada feature (para escolher alvos e limiares realistas).
    generos_pai / estilos: quantas faixas têm aquele gênero como dominante (Discogs400). Em generos_pai, também
      a régua do genero_pai_min: `regua` (nucleo, nucleo_amplo ou global — global = poucas faixas desse gênero, limite
      menos confiável) e quantas faixas passam em cada nível (amplo 0,7, normal 0,8, estrito 1,0).
    humores / instrumentos / generos_jamendo: probabilidade média (Jamendo).
    marcacoes: as etiquetas do usuário (#107), com quantas faixas cada uma tem.
    escritas: a escrita do título/álbum/artistas (#111), com quantas faixas da biblioteca têm cada uma.
    `filtro` restringe por trecho do nome.
    """
    con = banco()
    f = normalizar(filtro)
    if grupo == "escritas":
        from curator.motor import escrita

        bib = consultas.biblioteca(con)
        esc = escrita.calcular(con, bib)
        props = escrita.proporcoes(esc, bib)
        itens = [{"nome": e, "faixas": sum(1 for v in bib if e in esc.get(v, ())), "fracao": props.get(e, 0.0)}
                 for e in escrita.ESCRITAS if not f or f in e]  # fmt: skip
        return {"grupo": grupo, "itens": itens}
    if grupo == "marcacoes":
        from curator.motor import marcacoes

        itens = [{"nome": r["etiqueta"], "faixas": r["faixas"], "artistas": len(r["artistas"])}
                 for r in marcacoes.resumo(con) if not f or f in normalizar(r["etiqueta"])]  # fmt: skip
        return {"grupo": grupo, "itens": itens[:limite]}
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
        from curator.motor import popularidade

        if not f or f in "popularidade":
            out.append({"feature": "popularidade", "escala": "percentil das visualizações na biblioteca (0 = lado B, "
                        "1 = as mais vistas; p10/mediana/p90 = 0,1/0,5/0,9 por construção)",
                        "cobertura": popularidade.cobertura(con)})  # fmt: skip
        return {"grupo": grupo, "faixas_analisadas": len(esc.ids), "itens": out,
                "tipos_video": popularidade.TIPOS_VIDEO}  # fmt: skip
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
        if grupo == "generos_pai" and itens:
            afin = generos.Afinidades(con)
            base = espacos.da_biblioteca(con)
            for it in itens:
                rel = afin.relativa_receita(it["nome"], base)
                it["regua"] = _regua(afin, it["nome"])
                it["faixas_por_nivel"] = {n: int((rel >= t).sum()) for n, t in generos.NIVEIS.items()}
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


def examinar_genero(
    pai: str, limite: float = 0.8, n: int = 8, universo: Literal["biblioteca", "curtidas"] = "biblioteca"
) -> dict:
    """A fronteira de um gênero-pai antes de fixar genero_pai_min: quantas faixas passam em cada limite e as `n`
    faixas logo acima e logo abaixo de `limite` (afinidade relativa, 1,0 = típica do núcleo do gênero).

    Use o que você sabe dos artistas: se as de baixo são do gênero, desça o limite; se as de cima não são, suba.
    `instrumental` alto (> 0,5) costuma ser lo-fi/trilha sem voz. `outras_visoes` (quando a faixa tem): `maest`, a
    fração do gênero num modelo de outra rede; `rapping`/`singing` do YAMNet (só no Hip Hop); `generos_externos`,
    os gêneros mais votados do artista (MusicBrainz) e do álbum (Deezer). `afinidade_com_maest`: a mesma régua com
    o MAEST, que a receita não usa (separa melhor rap e Electronic, mas chama rock japonês de J-pop). Quando elas
    discordam da afinidade, olhe a faixa com mais cuidado."""
    con = banco()
    afin = generos.Afinidades(con)
    if pai not in afin.pais:
        perto = [p for p in afin.pais if normalizar(pai) in normalizar(p)]
        return {"erro": f"gênero-pai '{pai}' não existe" + (f" (quis dizer: {', '.join(perto)}?)" if perto else ""),
                "generos_pai": afin.pais}  # fmt: skip
    if universo == "curtidas":
        analisadas, fora = set(espacos.analisadas(con)), consultas.bloqueadas(con)
        ids = [
            r[0]
            for r in con.execute("SELECT video_id FROM faixas WHERE curtida = 1 ORDER BY video_id")
            if r[0] in analisadas and r[0] not in fora
        ]
    else:
        ids = espacos.da_biblioteca(con)
    rel, rel_m = afin.relativa_receita(pai, ids), afin.relativa_com_maest(pai, ids)
    na_receita = afin.usa_aprendida(pai)
    outra = afin.relativa(pai, ids) if na_receita else afin.relativa_aprendida(pai, ids)
    nome_outra = "afinidade_v2" if na_receita else "afinidade_aprendida"
    tem_m = (
        afin.fracao_maest(ids)[1]
        if afin.reguas[pai].referencia_maest is not None
        else np.zeros(len(ids), bool)
    )
    ordem = sorted(range(len(ids)), key=lambda i: (-rel[i], ids[i]))
    corte = sum(1 for i in ordem if rel[i] >= limite)
    esc = espacos.escalares(con)
    j = esc.colunas.index("instrumental") if "instrumental" in esc.colunas else None
    vistos = [ids[i] for i in ordem[max(0, corte - n) : corte + n]]
    meta = _faixas(con, vistos)
    visoes = generos.OutrasVisoes(con)
    # onde o modelo aprendido não opina (portão ou guarda, #101) e a régua aprendida repete a v2
    nao_julga: dict[str, str] = {}
    if pai in afin.aprendido.modelos and vistos:
        portao, dentro = afin.fora_do_aprendido(vistos), afin.aprendido.dentro_da_guarda(vistos)
        for v, p_, d in zip(vistos, portao, dentro, strict=True):
            if p_:
                nao_julga[v] = "a v2 põe a faixa num gênero sem rótulos (portão): vale a v2"
            elif not d:
                nao_julga[v] = "longe de tudo o que foi rotulado (guarda): vale a v2"

    def item(i: int) -> dict:
        x = esc.X[esc.indice[ids[i]], j] if j is not None and ids[i] in esc.indice else float("nan")
        out = {**meta[ids[i]], "afinidade": round(float(rel[i]), 3),
               "instrumental": None if np.isnan(x) else round(float(x), 2)}  # fmt: skip
        if not np.isnan(outra[i]):
            out[nome_outra] = round(float(outra[i]), 3)
        if (r := afin.aprendido.rotulo(pai, ids[i])) is not None:
            out["rotulo_do_usuario"] = "sim" if r else "não"
        elif ids[i] in nao_julga:
            out["aprendida_nao_julga"] = nao_julga[ids[i]]
        if tem_m[i]:
            out["afinidade_com_maest"] = round(float(rel_m[i]), 3)
        if v := visoes.da_faixa(ids[i], pai):
            out["outras_visoes"] = v
        return out

    return {
        "pai": pai,
        "limite": limite,
        "regua": _regua(afin, pai),
        "faixas_por_limite": {str(t): int((rel >= t).sum()) for t in (0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.2)},
        "acima": [item(i) for i in ordem[max(0, corte - n) : corte]],
        "abaixo": [item(i) for i in ordem[corte : corte + n]],
    }


def _regua(afin, pai: str) -> dict:
    reg = afin.reguas[pai]
    out = {"tipo": "aprendida" if afin.usa_aprendida(pai) else reg.tipo, "faixas_no_nucleo": reg.nucleo,
           "com_maest": reg.referencia_maest is not None}  # fmt: skip
    if m := afin.aprendido.modelos.get(pai):
        out["aprendida"] = {"rotulos": {"positivos": m.positivos, "negativos": m.negativos, "artistas": m.artistas},
                            "faixas_no_nucleo": m.nucleo, "na_receita": afin.usa_aprendida(pai)}  # fmt: skip
    return out


def faixas_para_rotular(pai: str, quantas: int = 10) -> dict:
    """SÓ quando o usuário pedir para ensinar um gênero: as faixas sem rótulo em que a resposta dele mais ensina
    (primeiro as que as réguas põem de lados opostos do corte 0,8, depois as perto do corte; no máximo 2 por
    artista). Pergunte e grave a resposta com rotular_genero. Não responda por ele nem proponha sessões."""
    from curator.motor import aprendido

    con = banco()
    try:
        lista = aprendido.duvidosas(con, pai, quantas)
    except KeyError:
        return {"erros": [f"gênero-pai '{pai}' não existe; veja descrever_vocabulario('generos_pai')"]}
    meta = _faixas(con, [d["video_id"] for d in lista])
    return {"pai": pai, "faixas": [{**meta[d["video_id"]], **d} for d in lista],
            "como_rotular": "rotular_genero(video_id, pai, pertence=True/False), só com a resposta do usuário"}  # fmt: skip


def rotular_genero(video_id: str, pai: str, pertence: bool | None) -> dict:
    """Grava a resposta do USUÁRIO: a faixa é (pertence=True) ou não é (False) do gênero-pai; None apaga o rótulo.
    Nunca rotule pelo que você acha nem a partir de uma playlist: só com o que o usuário disse."""
    from curator.motor import aprendido, generos

    con = banco()
    if pai not in generos.Afinidades(con).pais:
        return {"erros": [f"gênero-pai '{pai}' não existe"]}
    if not con.execute("SELECT 1 FROM faixas WHERE video_id = ?", (video_id,)).fetchone():
        return {"erros": [f"faixa {video_id} não está no banco"]}
    aprendido.rotular(con, video_id, pai, pertence, "mcp")
    return {"ok": True, "video_id": video_id, "pai": pai, "pertence": pertence}


# ------------------------------------------------------------------ marcações (#107)


def _regras(con) -> list:
    """As regras de destino do config.toml; levanta ValueError (MarcacaoInvalida) se estiverem erradas."""
    from curator.config import carregar_config
    from curator.motor import marcacoes

    return marcacoes.regras(con, carregar_config())


def listar_marcacoes(etiqueta: str | None = None) -> dict:
    """As marcações do usuário: por etiqueta, os artistas marcados, as exceções por faixa e quantas faixas ficam com
    ela; e as regras de destino do config.toml (etiqueta → masters em que entra e de que sai)."""
    from curator.motor import marcacoes

    con = banco()
    try:
        linhas = marcacoes.resumo(con, etiqueta)
        rs = _regras(con)
    except ValueError as e:
        return {"erros": [str(e)]}
    titulos = dict(con.execute("SELECT playlist_id, titulo FROM playlists"))
    return {
        "marcacoes": linhas,
        "regras": [{"etiqueta": r.etiqueta, "entrar_em": [titulos.get(p, p) for p in r.entrar_em],
                    "sair_de": [titulos.get(p, p) for p in r.sair_de]} for r in rs],
    }  # fmt: skip


def marcar_artista(artista: str, etiqueta: str, desfazer: bool = False) -> dict:
    """SÓ com o que o USUÁRIO disse ("a Moona é VTuber"): dá a etiqueta a todas as faixas do artista (nome exato de
    buscar_artistas, todos os canais com esse nome). `desfazer=True` tira. Nunca marque pelo que você acha, e não
    marque artista que faz de tudo (HoYo-MiX): aí é marcar_faixa."""
    from curator.motor import marcacoes

    con = banco()
    try:
        achados = marcacoes.marcar_artista(con, artista, etiqueta, "mcp", desfazer)
    except ValueError as e:
        return {"erros": [str(e)]}
    return {"ok": True, "etiqueta": marcacoes.etiqueta(etiqueta), "artista": achados[0][1], "canais": len(achados),
            "desfeita": desfazer}  # fmt: skip


def marcar_faixa(video_id: str, etiqueta: str, tem: bool | None) -> dict:
    """SÓ com o que o USUÁRIO disse: exceção da faixa, que vence o artista. tem=True dá a etiqueta, False tira
    (mesmo que o artista tenha), None apaga a exceção."""
    from curator.motor import marcacoes

    con = banco()
    try:
        marcacoes.marcar_faixa(con, video_id, etiqueta, tem, "mcp")
    except ValueError as e:
        return {"erros": [str(e)]}
    return {"ok": True, "video_id": video_id, "etiqueta": marcacoes.etiqueta(etiqueta), "tem": tem,
            "etiquetas_da_faixa": sorted(marcacoes.da_faixa(con, [video_id]).get(video_id, set()))}  # fmt: skip


def criar_plano_das_marcacoes() -> dict:
    """Plano (não aplicado) que corrige as masters atuais pelas regras de destino do config.toml: só o que as
    contraria. Só se o usuário pedir."""
    from curator.motor import marcacoes

    con = banco()
    try:
        return mostrar_plano(marcacoes.plano(con, _regras(con)))
    except ValueError as e:
        return {"erros": [str(e)]}


def ignorar_faixa(video_id: str, motivo: str | None = None, desfazer: bool = False) -> dict:
    """SÓ quando o USUÁRIO pedir para desconsiderar uma faixa (meme, cover de piada, não é música): ela sai de tudo
    (biblioteca, receitas, classificação, clusters, mapa, recomendações, buscas, rótulos de gênero), mas continua
    curtida no YouTube. `desfazer=True` devolve a faixa ao normal. Nunca ignore por conta própria."""
    con = banco()
    if not con.execute("SELECT 1 FROM faixas WHERE video_id = ?", (video_id,)).fetchone():
        return {"erros": [f"faixa {video_id} não está no banco"]}
    if desfazer:
        r = consultas.considerar_faixas(con, [video_id])
        return {"ok": True, "video_id": video_id, "ignorada": video_id in consultas.ignoradas(con),
                "estava_ignorada": bool(r["consideradas"])}  # fmt: skip
    r = consultas.ignorar_faixas(con, [video_id], motivo)
    return {"ok": True, "video_id": video_id, "ignorada": True, "ja_estava": bool(r["ja_estavam"])}


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
    Com playlists/mae, as curtidas não alocadas; com nas_gerais, as curtidas que ainda não estão em nenhuma geral,
    e as regras das marcações do config.toml valem depois do áudio (destino com `regra`; `barradas` = tiradas)."""
    con = banco()
    try:
        c = cl.classificar(con, mae=_escopo(con, mae) if mae else None,
                           em=[_escopo(con, x) for x in playlists or []], nas_gerais=nas_gerais,
                           regras=_regras(con) if nas_gerais else None)  # fmt: skip
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
                **({"barradas": [{**vars(x), "etiqueta": e} for x, e in d.barradas]} if d.barradas else {}),
            }  # fmt: skip
            for d in c.decisoes[:limite]
        ],
    }


def sugerir_playlists_gerais(
    reagrupar: bool = False,
    min_cluster: int | None = None,
    quantas: int | None = None,
    fronteira: float | None = None,
) -> dict:
    """Playlists gerais a partir de TODAS as curtidas, ignorando as playlists existentes (clusters sobrepostos).

    `min_cluster` menor dá mais playlists (padrão 40). `quantas` = N dá as playlists master (#105): exatamente N
    playlists amplas cobrindo todas as curtidas; cada faixa na sua e, na fronteira (`fronteira`, padrão 0,02), também
    na segunda. Leva cerca de 1 minuto. Dê nomes com renomear_cluster(gerais=True) antes do plano."""
    from curator.motor import clusters, gerais

    con = banco()
    try:
        aid, cs, novos = gerais.obter(con, gerais.parametros(min_cluster, quantas, fronteira), reagrupar)
    except (clusters.PoucasFaixas, ValueError) as e:
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
                "da_fronteira": len(c.membros) - len(c.nucleo) if quantas else None,
                "estatisticas": c.estatisticas,
            }
            for c in sorted(cs, key=lambda c: (-len(c.membros), c.cluster_id))
        ],
    }


def criar_plano_das_gerais(
    min_cluster: int | None = None, quantas: int | None = None, fronteira: float | None = None
) -> dict:
    """Plano (não aplicado) que CRIA as playlists gerais do último agrupamento. Não toca nenhuma playlist existente.
    Use os mesmos `min_cluster`/`quantas`/`fronteira` do sugerir_playlists_gerais (outros recalculam)."""
    from curator.motor import clusters, gerais

    con = banco()
    try:
        aid, cs, _ = gerais.obter(con, gerais.parametros(min_cluster, quantas, fronteira))
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
                           em=[_escopo(con, x) for x in playlists or []], nas_gerais=nas_gerais,
                           regras=_regras(con) if nas_gerais else None)  # fmt: skip
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
    popularidade: dict[str, float] | None = None,
    publicadas_desde: str | None = None,
    exigir_escritas: list[str] | None = None,
    excluir_escritas: list[str] | None = None,
    pedido: str | None = None,
) -> dict:
    """Recomenda SÓ músicas novas (fora da biblioteca) parecidas com as sementes: faixas (video_ids de
    buscar_faixas), uma playlist, um cluster ou um artista. `modo` é obrigatório: se o usuário não disse rápido
    ou lento, PERGUNTE antes. perfil_alvo/generos_preferidos (como na receita) só no modo lento.
    `popularidade` {"alvo": 0..1, "peso"}: 1 = as mais vistas, 0 = lado B (percentil das visualizações na
    biblioteca). `publicadas_desde` "AAAA-MM-DD": só lançamentos a partir da data. Os dois valem nos dois modos.
    `exigir_escritas`/`excluir_escritas` (japonesa, coreana, chinesa, cirilica, latina): escrita do título, álbum e
    artistas, nos dois modos; para pedidos de J-pop/J-rock use exigir_escritas ["japonesa"], para K-pop ["coreana"]."""
    return _recomendar("novas", modo, pedido, faixas=faixas, playlist=playlist, cluster=cluster, artista=artista,
                       tamanho=tamanho, excluir_artistas=excluir_artistas, perfil_alvo=perfil_alvo,
                       generos_preferidos=generos_preferidos, popularidade=popularidade,
                       publicadas_desde=publicadas_desde, exigir_escritas=exigir_escritas,
                       excluir_escritas=excluir_escritas)  # fmt: skip


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
    popularidade: dict[str, float] | None = None,
    publicadas_desde: str | None = None,
    exigir_escritas: list[str] | None = None,
    excluir_escritas: list[str] | None = None,
    pedido: str | None = None,
) -> dict:
    """Mistura músicas novas com as da biblioteca (proporcao_novas = fração de novas). Use SÓ quando o usuário
    pediu explicitamente para misturar. `modo` é obrigatório: na dúvida, PERGUNTE. `popularidade`,
    `publicadas_desde` e as escritas como em recomendar_novas."""
    return _recomendar("misturar", modo, pedido, proporcao_novas=proporcao_novas, faixas=faixas, playlist=playlist,
                       cluster=cluster, artista=artista, tamanho=tamanho, excluir_artistas=excluir_artistas,
                       perfil_alvo=perfil_alvo, generos_preferidos=generos_preferidos, popularidade=popularidade,
                       publicadas_desde=publicadas_desde, exigir_escritas=exigir_escritas,
                       excluir_escritas=excluir_escritas)  # fmt: skip


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
    arvore_playlists, estatisticas, buscar_faixas, buscar_artistas, buscar_albuns, descrever_vocabulario, examinar_genero,
    faixas_para_rotular, rotular_genero, ignorar_faixa, listar_marcacoes, marcar_artista, marcar_faixa,
    criar_plano_das_marcacoes,
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
