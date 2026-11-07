# Decisões de projeto: yt-music-curator

> Estado: **fase de design**, sem código ainda. Data: 2026-11-03. Atualizado em 2026-11-04 (ADR-13, avaliações e preservação no ADR-02/03).
> Este documento registra as decisões de arquitetura (ADRs), o motivo de cada uma e as alternativas rejeitadas.

## Sumário

- [Contexto](#contexto)
- [Requisitos](#requisitos)
- [Visão geral](#visão-geral)
- [ADR-01: Estrutura do repositório](#adr-01-estrutura-do-repositório)
- [ADR-02: Cache e modelo de dados](#adr-02-cache-e-modelo-de-dados)
- [ADR-03: Hierarquia e pertencimento](#adr-03-hierarquia-e-pertencimento)
- [ADR-04: Representação vetorial](#adr-04-representação-vetorial)
- [ADR-05: IA com reprodutibilidade (receitas)](#adr-05-ia-com-reprodutibilidade-receitas)
- [ADR-06: Classificar não alocadas nas playlists filhas](#adr-06-classificar-não-alocadas-nas-playlists-filhas)
- [ADR-07: Sugerir reorganização (clusters sobrepostos)](#adr-07-sugerir-reorganização-clusters-sobrepostos)
- [ADR-08: Obtenção de áudio sem bloqueio](#adr-08-obtenção-de-áudio-sem-bloqueio)
- [ADR-09: Analisador de áudio](#adr-09-analisador-de-áudio)
- [ADR-10: Interfaces (CLI e MCP)](#adr-10-interfaces-cli-e-mcp)
- [ADR-11: Visualizador gráfico](#adr-11-visualizador-gráfico)
- [ADR-12: Segurança e higiene](#adr-12-segurança-e-higiene)
- [ADR-13: Recomendações fora da biblioteca](#adr-13-recomendações-fora-da-biblioteca)
- [ADR-14: Afinidade de gênero-pai (#83)](#adr-14-afinidade-de-gênero-pai-83)
- [ADR-15: Análises extras e metadados externos (#88)](#adr-15-análises-extras-e-metadados-externos-88)
- [ADR-16: Compartilhar análises entre pessoas (#96)](#adr-16-compartilhar-análises-entre-pessoas-96)
- [ADR-17: Gênero-pai aprendido com os rótulos do usuário (#93)](#adr-17-gênero-pai-aprendido-com-os-rótulos-do-usuário-93)
- [Fluxos dos pedidos de exemplo](#fluxos-dos-pedidos-de-exemplo)
- [Roteiro](#roteiro)
- [Riscos e questões em aberto](#riscos-e-questões-em-aberto)
- [Referências](#referências)

---

## Contexto

Este projeto junta dois protótipos.

**[yt-music-helper](../../yt-music-helper)** tem 3 scripts Python e usa ytmusicapi.
- Baixa playlists para um cache SQLite com as tabelas `albuns`, `musicas`, `playlists` (com `mae_id`) e `playlist_musicas`.
- Gera um JSON com as músicas da playlist-mãe que não estão em nenhuma filha, para colar numa conversa com uma IA. A IA sugere em qual filha cada música entra, com base no título, nas tags e nos 15 artistas mais frequentes de cada filha.
- Limitações:
  - Sem autenticação: lê só playlists públicas, não vê curtidas e não escreve.
  - O pertencimento no cache nunca diminui.
  - Uma falha de API fica gravada como se fosse cache válido.
  - Não há expiração.
  - O passo de IA é manual.

**[yt-classifier](../../yt-classifier)** é um único `teste.py`.
- Baixa a faixa inteira em WAV com yt-dlp, sem throttling.
- Roda cerca de 20 modelos Essentia na CPU:
  - embeddings EffNet-Discogs (1280-d) e MusiCNN (200-d);
  - 12 cabeças binárias: danceability, aggressive, happy, sad, relaxed, party, acoustic, electronic, voice/instrumental, gender, engagement, approachability;
  - MTG-Jamendo genre, instrument e mood/theme;
  - Discogs400;
  - valência/excitação com 3 modelos;
  - BPM com RhythmExtractor e TempoCNN, e tom.
- Imprime um JSON no stdout. **Descarta os embeddings e os vetores completos** (guarda só top-5/top-10) e não tem cache.

O cyanite.ai foi descartado: é pago e libera poucas análises por mês (~5). O Essentia não tem limite de volume.

## Requisitos

1. Analisar gêneros, tags e features de áudio de **todas** as músicas curtidas no YouTube Music.
2. Ter cache de tudo: metadados, pertencimento a playlists e resultados do classificador.
3. Conhecer a **hierarquia de playlists** (mãe/filha) e em quais playlists cada música está. Estar só em "Curtidas" significa que a música ainda não foi alocada.
4. Atender pedidos recorrentes em linguagem natural, como "playlist pra dirigir/drift", "rap", "rock geral e subgêneros centrais", "onde colocar as não alocadas", "sugira uma reorganização" e "pra dormir, sem Hoyoverse".
5. Resultados **reprodutíveis**: o mesmo pedido deve gerar resultados praticamente iguais.
6. Uma música pode estar em **mais de uma playlist** quando for parecida o bastante com cada uma.
7. Download em massa sem tomar bloqueio do YouTube, sempre com a **faixa inteira**.
8. Um visualizador gráfico dos vetores e clusters, por curiosidade.

## Visão geral

```
            ┌────────────────────────── yt-music-curator ──────────────────────────┐
            │                                                                       │
 YT Music ──┤ coleta/ (ytmusicapi, autenticado) ──┐                                 │
 (conta)    │                                     ▼                                 │
            │                               banco/ (SQLite) ◄── audio/fila ◄── audio/provedores
            │                                     │              │                (YT, proxies,
            │                                     │              ▼                 SoundCloud,
            │                                     │         audio/analisador       Bandcamp, local)
            │                                     │          (Essentia)            │
            │                                     ▼                                 │
            │                               motor/ (vetores, receitas,              │
            │                                kNN, clusters, MMR)                    │
            │                                     │                                 │
            │                 ┌───────────────────┼───────────────────┐             │
            │                 ▼                   ▼                   ▼             │
            │           interfaces/cli      interfaces/mcp      visualizador (HTML) │
            └─────────────────┬───────────────────┬─────────────────────────────────┘
                              │                   │
                 plano aplicar (com confirmação)  Claude Code / Claude Desktop
                              ▼
                          YT Music
```

Princípio central: **a IA interpreta o pedido, e o motor determinístico escolhe as músicas.**

---

## ADR-01: Estrutura do repositório

**Decisão:** um repositório novo (`yt-music-curator`) com **um único programa modular**, como pacote Python gerenciado com `uv` e `pyproject.toml`, em Python 3.14.

```
src/curator/
  coleta/       # ytmusicapi: curtidas, playlists próprias, metadados, escrita (planos)
  audio/
    provedores/ # YouTube, YouTube por busca, SoundCloud, Bandcamp, pasta local
    fila.py     # fila retomável, cotas, disjuntores, pool de proxies
    analisador.py # Essentia (portado de yt-classifier/teste.py)
  banco/        # schema SQLite, migrações, acesso
  motor/        # normalização, vetores, receitas, kNN, clustering, MMR
  interfaces/
    cli.py      # typer
    mcp.py      # servidor MCP (FastMCP, SDK oficial `mcp`)
  visual/       # gerador do HTML do visualizador
```

- O Essentia (`essentia-tensorflow`, ~850 MB com dependências) fica num **extra opcional** `[analise]`. Assim, quem só consulta, gera playlists ou usa o MCP não precisa instalar o TensorFlow. O analisador roda como **worker separado**, um processo próprio acionado pela CLI ou por um systemd timer, e grava no mesmo banco.
- Os repositórios antigos ficam como referência histórica, sem alterações.

**Por quê:** os dois lados leem e escrevem o mesmo modelo de dados (faixas, playlists, features, embeddings). Com um programa único, há uma só camada de banco e um só schema versionado, e as consultas cruzam análise e pertencimento sem integração entre serviços.

**Alternativas rejeitadas:**
- *Repositórios separados* (o classificador como serviço e as playlists em outro repo): exigiria um contrato de schema entre os dois, duplicaria a camada de banco e obrigaria a sincronizar versões, sem ganho real num uso pessoal.
- *Evoluir o yt-music-helper*: herdaria os scripts soltos, o banco versionado no git e um schema que precisa ser refeito de qualquer forma.

## ADR-02: Cache e modelo de dados

**Decisão:** um **SQLite único**, fora do git (em `~/.local/share/yt-music-curator/curator.db`, configurável), com schema versionado e migrações numeradas.

| Tabela | Conteúdo |
|---|---|
| `faixas` | `video_id` PK, título, artistas (JSON), `album_id`, álbum, ano, duração, `isrc` (quando houver), `curtida`, `curtida_em`, `metadados_em` |
| `artistas`, `albuns` | ids e nomes (para busca exata nos filtros), ano, descrição |
| `playlists` | `playlist_id` PK, título, `propria`, `mae_id` FK → `playlists`, `tipo` (`agregadora`/`folha`), `snapshot_hash`, `sincronizada_em` |
| `playlist_faixas` | `playlist_id`, `video_id`, posição |
| `analises` | `video_id`, `versao_analisador`, `hash_modelos`, `fonte_audio`, `duracao_analisada`, status, erro, `tempo_s`, `analisada_em` |
| `features` | colunas escalares indexáveis: `bpm`, `tom`, `escala`, `key_strength`, `danceability`, `acousticness`, `aggressive`, `happy`, `sad`, `relaxed`, `party`, `electronic`, `instrumental`, `valence`, `arousal`, `energy`, `engagement`, `approachability` |
| `vetores` | `video_id`, `tipo` (`effnet_emb`, `musicnn_emb`, `discogs400`, `jamendo_genre`, `jamendo_mood`, `jamendo_instr`), `dim`, BLOB float32 |
| `rotulos_modelo` | `tipo`, índice → rótulo (o vocabulário fechado da IA; ver ADR-05) |
| `fila_download` | `video_id`, status, tentativas, `proxima_tentativa_em`, provedor e proxy usados, último erro |
| `proxies` | id, URL, cota diária, uso hoje, sucessos, falhas, `quarentena_ate` |
| `receitas` | id, nome, pedido original, `spec` (JSON), `hash_spec`, `hash_pedido`, criada e atualizada em |
| `execucoes` | `receita_id`, `versao_banco`, lista resultante, data (permite comparar execuções) |
| `planos` | id, origem (receita, classificação, reorganização), diff (JSON), status (`pendente`/`aplicado`/`descartado`), log |
| `projecoes` | coordenadas UMAP 2D/3D por espaço, com `hash_biblioteca` (ver ADR-11) |
| `coletas`, `recomendacoes`, `recomendacao_itens`, `deezer_casamentos` | recomendações fora da biblioteca (ADR-13) |

**Regras de cache e invalidação**

- **Pertencimento:** a sincronização de cada playlist é **total, dentro de uma transação**: apaga e reinsere `playlist_faixas`. Isso corrige o bug do helper, em que uma música removida continuava no cache. A playlist só é baixada de novo quando a contagem ou o `snapshot_hash` muda, ou com `--forcar`.
- **Metadados** (álbum, artista): TTL de 30 dias, configurável.
- **Uso e popularidade (#85, 2026-11-05):** a API não tem contagem de reproduções por usuário, e o histórico (`get_history`) só responde com o histórico do YouTube ligado na conta, desligado nesta (que é compartilhada). Ficam em `faixas` o que a API dá: `explicita` e `tipo_video` (faixa de áudio, clipe, vídeo de usuário), que vêm nas curtidas e playlists sem chamada extra, e `visualizacoes` (de todo o YouTube) e `publicada_em`, pelo `get_song`. Este é uma chamada por faixa, então o `sincronizar` busca no máximo `[metadados] estatisticas_por_rodada` (300) por vez, com validade de 30 dias e backoff na falha.
- **Falha não é cache.** Uma chamada que falha não grava resultado vazio. O item vai para a fila com backoff (bug do helper).
- **Análise:** é feita **uma vez por faixa**. Só é refeita quando `versao_analisador` ou `hash_modelos` mudam. O áudio não é guardado, a não ser no modo experimental da #84 (ver ADR-08.1).
- **Vetores completos.** Todos os vetores de probabilidade (400, 87, 56, 40 dimensões) e os embeddings ficam salvos, para que filtros e receitas novas não precisem de reanálise.
- **Uma faixa analisada nunca sai do banco (#59).** Sair das curtidas e de todas as playlists só muda o pertencimento: a análise e os vetores ficam, e são reaproveitados se a faixa voltar ou for recomendada. Uma trava (`BEFORE DELETE ON faixas`) recusa apagar faixa com análise; ela fica em `faixas`, e não trocando o `CASCADE` das tabelas de análise por `RESTRICT`, para não reescrever a tabela de vetores, a maior do banco. Faixa recomendada também não pode ser apagada (chave estrangeira sem cascata em `recomendacao_itens`).

**Busca vetorial:** com milhares de faixas, um cosseno *brute-force* em numpy (matriz N × 1280, float32) responde em milissegundos. Por isso não há sqlite-vec nem banco vetorial dedicado. Essa decisão é reavaliada acima de ~100 mil faixas.

## ADR-03: Hierarquia e pertencimento

**Decisão**

- As playlists próprias formam uma **árvore** (`mae_id`, com vários níveis). A árvore é declarada num arquivo versionável `hierarquia.toml`, com um comando de CLI para editar, e fica espelhada no banco.
  ```toml
  [[playlist]]
  id = "PL...rock-all"
  tipo = "agregadora"

  [[playlist]]
  id = "PL...arena-rock"
  mae = "PL...rock-all"
  ```
- **Playlist agregadora** (como "Rock - All"): deve conter todas as faixas das filhas, ou seja, **mãe ⊇ ∪ filhas**. O sistema avisa sobre inconsistências e as inclui num plano de correção.
- **Curtidas não é uma playlist da árvore.** É uma *fonte*. As definições:
  - **Biblioteca** = curtidas ∪ faixas de playlists próprias.
  - **Não alocada** = curtida que não está em nenhuma playlist própria.
  - **Exclusiva da mãe** = está na mãe, mas em nenhuma filha (o conceito do helper, mantido).
- Uma faixa pode estar em várias folhas ao mesmo tempo. Isso é esperado (ver ADR-06/07).
- **Biblioteca** exclui as playlists de descobertas (`playlists.descobertas`, criadas a partir de recomendações, ADR-13): o que só está nelas ainda não foi curtido.
- **Playlists ignoradas** (`playlists.ignorada`, #77; `curator playlists ignorar`): as que não são de música (vídeos do YouTube comum numa conta conjunta, aulas). Somem de tudo: listagens, mapa, receitas, classificação, clusters, recomendações (sementes e candidatas), MCP e planos (um plano que as altere é recusado). Os vídeos que só estão nelas viram lista negra, mesmo curtidos, e não entram na fila de análise; quem também está numa playlist válida continua valendo por ela. A marcação fica no banco, sobrevive a `sincronizar` e a `hierarquia aplicar`, e as análises já feitas são guardadas (ADR-02).
- **Faixas fora da biblioteca** (descurtidas, candidatas de recomendação) ficam guardadas com a análise (ADR-02), mas fora de todos os universos, do z-score, do mapa e dos clusters (`espacos.da_biblioteca`).

**Avaliações (#60, #61).** `faixas.avaliacao` é `like`, `dislike` ou `neutra`, lida do `likeStatus` do ytmusicapi.
- O `get_playlist` (e as curtidas, que são uma playlist) lê o botão de like de verdade: a sincronização das playlists traz likes e dislikes. No `get_watch_playlist` (rádio), o ytmusicapi lê o menu, que só sabe "like" ou não, e "INDIFFERENT" pode ser um dislike (a docstring avisa). De lá só o LIKE vale, e nada que venha do rádio desfaz um dislike.
- Não existe lista de "músicas com dislike" na API (nem no ytmusicapi 1.12). Mas a resposta crua do endpoint `next` (o mesmo do rádio) traz o **botão de like do player** do vídeo consultado, com `LIKE`, `DISLIKE` ou `INDIFFERENT` (verificado em 04/11 com uma faixa com dislike que não está em nenhuma playlist). `avaliacao_atual` faz essa leitura, uma chamada por faixa:
  - toda música nova de uma recomendação é conferida antes de ser salva; as que vierem com dislike (ou já curtidas) saem e o ranking é refeito. No modo lento, isso acontece antes de a candidata ir para a fila, e o dislike não gasta download;
  - cada `curator sincronizar` confere de novo as recomendadas ainda sem reação (`conferir_por_rodada`, 100), as que não foram conferidas há `conferir_validade_dias` (7). Assim um dislike dado ouvindo pelo app, fora das suas playlists, chega ao curator;
  - `avaliacao_conferida_em` guarda a última leitura confiável (playlists, curtidas ou botão do player).
- **Dislike é filtro duro**: fora de receitas (todos os universos), classificação (decisões e centroides), clusters, reorganização, mapa e recomendações. A faixa e a análise ficam. `curator plano dislikes` sugere tirá-las das suas playlists; nunca é automático.
- **Parecer com um dislike custa** nas recomendações (`fatores_dislike`), a partir dos seus dados sincronizados:
  - artista: o score é multiplicado por 1 − 0,5 × (dislikes do artista ÷ faixas avaliadas dele: curtidas + dislikes). Um dislike num artista com 400 curtidas quase não pesa; um artista só com dislikes perde metade;
  - som: se a candidata tem análise e a faixa mais parecida com ela (embedding) é um dislike e não uma da biblioteca, perde até mais 50%, proporcional à diferença de similaridade (margem 0,1). No modo rápido isso vale para candidatas já analisadas; no lento, para todas.

## ADR-04: Representação vetorial

**Decisão:** cada faixa existe em **dois espaços**.

1. **Espaço semântico interpretável.** Cada dimensão tem nome:
   - as features escalares (ADR-02);
   - os vetores de probabilidade de gênero (Discogs400, Jamendo genre);
   - humor e tema (Jamendo mood/theme);
   - instrumentos.

   Os valores são normalizados com **z-score sobre a própria biblioteca** e têm **pesos por grupo**, para que os 400 gêneros não dominem as ~15 features escalares. É neste espaço que a IA descreve um *perfil-alvo*, por exemplo "energy alta, aggressive alta, BPM 120–150, estilo industrial/eletrônico", porque cada eixo tem significado.
2. **Espaço de embedding.** É a média temporal do EffNet-Discogs (1280-d), normalizada L2. Serve para "parecidas com estas", kNN, centroides de playlist e clustering. Ele captura timbre e estilo melhor que as cabeças isoladas. O EffNet-Discogs foi treinado com metadados editoriais do Discogs e tem bons resultados em tarefas de música (Alonso-Jiménez, Serra & Bogdanov, 2022).

O MusiCNN (200-d) também fica salvo, mas é secundário. Serve para os modelos de valência/excitação e como espaço alternativo no visualizador.

**Por quê:** o espaço interpretável permite que um texto ("pra dormir") vire números verificáveis. O embedding dá a noção de "som parecido", que nenhuma lista de tags captura bem. Os dois juntos dispensam pedir à IA que "lembre" de milhares de músicas.

**Fundamentos:**
- Valência × excitação: modelo circumplexo de Russell (1980).
- Taxonomia de humor, gênero e instrumentos: MTG-Jamendo (Bogdanov et al., 2019).

## ADR-05: IA com reprodutibilidade (receitas)

**Problema:** LLMs variam entre execuções, até com temperatura 0 (Atil et al., 2024). Se a IA escolher as músicas, o mesmo pedido gera playlists diferentes.

**Decisão:** a IA **não escolhe músicas**. Ela traduz o pedido numa **receita**, um JSON validado por pydantic com esquema estrito, usando um **vocabulário fechado**: os rótulos que existem em `rotulos_modelo` e os artistas, álbuns e playlists que existem no banco, todos obtidos pelas ferramentas MCP. O **motor determinístico** executa a receita.

Exemplo de receita para "pra dormir, sem Hoyoverse":

```json
{
  "nome": "Dormir",
  "tamanho": 50,
  "universo": "biblioteca",
  "filtros": {
    "excluir_artistas": ["HOYO-MiX"],
    "excluir_albuns_contendo": ["Genshin Impact", "Honkai: Star Rail", "Zenless Zone Zero"],
    "bpm": [50, 100],
    "max": { "aggressive": 0.2, "party": 0.3 }
  },
  "perfil_alvo": {
    "relaxed":      { "alvo": 0.9, "peso": 2.0 },
    "arousal":      { "alvo": 0.15, "peso": 2.0 },
    "energy":       { "alvo": 0.1, "peso": 1.5 },
    "acousticness": { "alvo": 0.7, "peso": 0.5 }
  },
  "generos_preferidos": { "Electronic---Ambient": 1.0, "Classical---Modern": 0.5 },
  "sementes": [],
  "diversidade": { "lambda": 0.75, "max_por_artista": 3 }
}
```

**Execução da receita (determinística):**

1. **Universo:** a biblioteca, curtidas, uma playlist ou uma subárvore.
2. **Filtros duros:** artistas, álbuns ou playlists excluídos ou exigidos (comparação exata com ids do banco), intervalos de BPM e duração, mínimos e máximos de features, e gênero com probabilidade ≥ limiar.
3. **Pontuação:**
   - `score = w₁ · sim_perfil + w₂ · sim_sementes + w₃ · afinidade_gêneros`
   - `sim_perfil` é a distância ponderada ao perfil-alvo no espaço interpretável.
   - `sim_sementes` é o cosseno até o centroide das sementes no espaço de embedding, como na consulta por centroide de Rocchio (1971).
4. **Ordenação total:** por `score` decrescente, com desempate por `video_id`. Nenhuma etapa usa aleatoriedade.
5. **Diversidade:** MMR (Carbonell & Goldstein, 1998) com λ fixo e `max_por_artista`, para não sair "10 músicas do mesmo álbum".
6. **Corte** em `tamanho`.

**Garantias:**
- **Mesma receita e mesmo banco → resultado idêntico.** A receita fica salva em `receitas` e pode ser reexecutada quando entram músicas novas, como uma "playlist inteligente".
- **A variação só pode vir da tradução pedido → receita.** Ela é reduzida assim:
  - esquema estrito com enums vindos do banco;
  - exemplos *few-shot* de receitas;
  - cache por `hash_pedido` (pedido normalizado);
  - a receita é mostrada e pode ser editada ou fixada antes de virar playlist.
- **Métrica:** a similaridade de Jaccard entre os resultados de duas traduções do mesmo pedido deve ser **≥ 0,8**. Para a mesma receita, o Jaccard é 1,0 por construção.

**Exclusões exatas** (caso Hoyoverse): a IA chama `buscar_artistas("hoyo")` e `buscar_albuns("genshin")` no MCP, recebe os nomes que **existem no banco** e os coloca nos filtros. A exclusão é uma comparação exata, não uma decisão do modelo.

## ADR-06: Classificar não alocadas nas playlists filhas

**Decisão:** usar o **pertencimento atual como rótulo supervisionado**, com classificação multi-rótulo hierárquica baseada em vizinhos.

Para uma faixa *t* e uma folha *p*:

```
s(t, p) = α · média(cos(t, v)) para os k vizinhos mais próximos de t entre os membros de p
        + (1 − α) · cos(t, centroide(p))
```

Os valores iniciais são k = 10 e α = 0,6, ambos fixos e versionados.

- **Limiar por playlist (τₚ):** calculado por validação *leave-one-out* sobre os membros atuais de *p*. É o percentil 10 dos scores que os próprios membros recebem quando são retirados da playlist. Cada playlist tem seu critério: uma playlist "fechada" (som homogêneo) exige mais semelhança que uma "aberta".
- **Multi-rótulo:** a faixa vai para **todas** as folhas em que `s ≥ τₚ`. É assim que um crossover entra em duas playlists.
- **Hierárquico (top-down):** primeiro decide entre as mães, depois só entre as filhas das mães aceitas. Isso evita que uma faixa de rap seja comparada com subgêneros de metal.
- **Sem destino:** se a faixa não passa em nenhum limiar, aparece como "sem destino", com as três playlists mais próximas e as distâncias. Muitas faixas sem destino parecidas entre si indicam uma playlist nova (ADR-07).
- **Partida a frio:** uma folha com menos de 5 membros não tem estatística suficiente. Nesse caso usa uma receita descritiva, criada pela IA a partir do título, com aviso de baixa confiança.
- **Papel da IA:** só explica os casos ambíguos (scores perto do limiar) e nomeia o motivo. Não decide o caso geral.

**Fundamento:** no RecSys Challenge 2018 de continuação automática de playlists, os métodos de vizinhos mais próximos foram baselines fortes e fizeram parte de soluções no topo do ranking (Ludewig et al., 2018; Zamani et al., 2019).

**Substitui** a heurística do helper (os 15 artistas mais frequentes e o julgamento da IA), que não usava o som e variava a cada execução.

**Só nas playlists nomeadas (#80, 04/11):** as playlists existentes seguem a lógica do usuário (uma por jogo, uma por artista) e não são gabarito nem alvo por padrão. `curator classificar` exige `--em` (uma ou mais), `--mae` (as filhas) ou `--nas-gerais` (as playlists gerais do ADR-07); sem alvo, não sugere nada. Com `--nas-gerais`, as candidatas são as curtidas que ainda não estão em nenhuma geral, mesmo que estejam numa playlist antiga.

**Avaliação (#28, 2026-11-04, `docs/avaliacao.md`):** k = 10, α = 0,6 e τ no percentil 10 ficaram como estavam, porque não houve gabarito confiável que indicasse outra coisa.
- Nas filhas do Rock - All, montadas por uma IA, a concordância é muito puxada pelo artista principal. Elas serão refeitas pelo áudio (#74).
- Nas raízes, montadas pelo usuário, a revocação é ~0,9, mas cada faixa passa no limiar de muitas playlists. Boa parte delas é por franquia e não por som; a calibração disso fica na #75.

## ADR-07: Sugerir reorganização (clusters sobrepostos)

**Decisão:**

1. **Redução de dimensão:** UMAP (McInnes et al., 2018) do embedding para ~10 dimensões, com métrica cosseno e `random_state` fixo.
2. **Clustering:** HDBSCAN (Campello, Moulavi & Sander, 2013), via `sklearn.cluster.HDBSCAN`, com `min_cluster_size` configurável (por exemplo, 12 faixas). Roda na biblioteca toda ou dentro de uma mãe ("reorganizar o Rock").
3. **Pertencimento sobreposto:** uma música entra em todas as playlists sugeridas com que é parecida o bastante.
   - O HDBSCAN gera uma partição, que é só o ponto de partida. Cada cluster tem um núcleo (os membros com maior probabilidade) e um centroide.
   - Cada faixa entra em **todo cluster** em que `cos(t, centroide_c) ≥ τ_c`. O limiar τ_c é o **percentil 25 da similaridade dos membros do núcleo de c**. Cada cluster tem sua régua, não há uma régua global.
   - *Revisado na implementação (2026-11-03):* com o percentil 25 do núcleo, um quarto do próprio núcleo ficava de fora e cada cluster cobria só metade do estilo. Agora os membros são a **partição do HDBSCAN mais** quem passa no limiar, e τ_c é o **percentil 10 da similaridade de todos os membros do HDBSCAN** ao centroide do núcleo. Ninguém sai do próprio cluster; a sobreposição só acrescenta.
   - Os pontos que o HDBSCAN marca como ruído também podem entrar em clusters por essa regra. O que sobrar continua "sem cluster".
   - A sobreposição entre os clusters *a* e *b* é medida por Jaccard. Se passar de ~0,6, as duas playlists provavelmente são uma só, e o sistema sugere fundi-las.
   - Alternativa registrada: os *soft membership vectors* do pacote `hdbscan`. A regra por centroide foi escolhida por ser mais simples de explicar e de auditar.
4. **Descrição:** cada cluster recebe estatísticas agregadas: estilos dominantes (massa média do Discogs400), humor, BPM mediano, valência e excitação, e os artistas mais frequentes.
5. **Nome:** a IA dá nome a cada cluster a partir **apenas dessas estatísticas**, com nomes em cache por `hash` do conteúdo do cluster.
   - *Implementação (M5, antes do M4):* enquanto o MCP não existe, o nome é **automático e determinístico** (estilo dominante, o segundo só se tiver ao menos metade da força do primeiro, mais energia e humor: "Hard Rock · energia alta"). O cache é pelo hash do **núcleo**: um nome dado por você (`curator clusters renomear`) ou, depois, pela IA, sobrevive a reagrupamentos que mantenham o núcleo.
   - Fusão e divisão ficam como **sugestões em texto**; só "criar" e "completar" viram operações de plano, porque não mexem no que você já organizou.
6. **Saída:** um **plano** com playlists novas, fusões, divisões e movimentações. Cada faixa aparece em todas as playlists sugeridas a que pertence. Nada é aplicado sem confirmação (ADR-10).

**Determinismo:** os mesmos dados e os mesmos parâmetros (incluindo `random_state`) dão os mesmos clusters. Quando entram faixas novas, os clusters existentes são mantidos como âncoras: as faixas novas são atribuídas por centroide, e o reclustering completo só acontece quando você pede. Assim, os nomes e o pertencimento não mudam o tempo todo.

**Playlists gerais (#80, 04/11):** `curator playlists sugerir` agrupa só as curtidas analisadas (escopo `curtidas`), ignorando todas as playlists existentes, e o plano só cria playlists novas, marcadas como gerais (`playlists.geral`) ao ser aplicado. Para dar poucas playlists amplas, o HDBSCAN roda no modo "leaf" com tamanho mínimo 40. Na biblioteca real (2.647 curtidas), o modo padrão ("eom") juntava tudo em 2 blocos de 1.100–1.800 faixas com qualquer tamanho mínimo; "leaf" com 40 deu 14 grupos de 100 a 640 faixas, cobrindo 86% das curtidas (40% em mais de uma). `--min-cluster` muda a granularidade; com outros parâmetros, o agrupamento é refeito. Nomes automáticos repetidos são numerados no plano; a IA (ou o usuário) renomeia antes. `curator reorganizar`, que compara com as playlists existentes (completar, fundir, dividir), passou a exigir `--mae`.

**Recategorizar uma mãe (#74, 04/11):** `curator reorganizar --mae X --recategorizar` refaz as filhas de X pelo som, quando o usuário pede aquela playlist. As filhas atuais não servem de referência: cada cluster (HDBSCAN "leaf", mínimo 12; no Rock - All, 5 grupos de 14 a 80 faixas cobrindo 88% das 171) vira uma filha nova, e as antigas são esvaziadas. Nada sai da mãe. O plano é aplicado pelo usuário; ao aplicar, as novas ganham `mae_id` = X e a CLI imprime os blocos para o `hierarquia.toml`. Parâmetros diferentes dos do último agrupamento do escopo recalculam os clusters.

## ADR-08: Obtenção de áudio sem bloqueio

**Requisito:** sempre a **faixa inteira**. Ordem de tentativas: YouTube direto → YouTube por proxies → outros provedores sem DRM.

### 8.1 Configuração do yt-dlp

- **Formato:** o áudio de **menor bitrate** aceitável (`bestaudio[abr<=70]/worstaudio`, normalmente opus 249/250). Os modelos reamostram para 16 kHz (EffNet/MusiCNN), 11,025 kHz (TempoCNN) e 44,1 kHz (ritmo/tom), então a perda de qualidade é irrelevante para a análise e o tráfego cai bastante.
- **Sem conversão para WAV:** o áudio é decodificado pelo `ffmpeg` do sistema direto para a memória (pipe), sem arquivo intermediário.
  - *Revisado na implementação (2026-11-03):* o `MonoLoader` do essentia-tensorflow 2.1b6 **não** abre opus/webm ("Unsupported codec"). Por isso o ffmpeg decodifica na taxa e nos canais nativos, e a reamostragem continua com o `Resample` do Essentia (ver ADR-09).
- O áudio vai para um diretório temporário e é **apagado logo após a análise**.
  - *Exceção experimental (#84, 2026-11-05):* com `[audio] guardar = true` (ou `curator analisar --guardar-audio`), o áudio analisado vai para `guardar_dir` e fica registrado em `audios_guardados` com a origem do download. Quando um modelo novo ou uma mudança no analisador reabre a fila, o provedor `guardado` (sempre o primeiro) usa o arquivo: sem rede, sem cota e sem pausa, e a análise mantém a fonte original. Arquivo apagado à mão cai para os outros provedores. É uma ferramenta de desenvolvimento, desligada por padrão; o risco abaixo continua valendo, e `curator audios limpar` apaga tudo.
  - *Só baixar (#90, 2026-11-05):* `curator audios baixar` guarda o áudio das faixas da biblioteca que ainda não têm arquivo, sem analisar e sem mexer na fila de análise, para adiantar uma reanálise (#88). Baixa a fonte da análise atual (`fonte_audio`/`fonte_ref`), sem nova busca, porque o casamento já foi conferido e a reanálise deve usar o mesmo áudio. Uma faixa nunca analisada só tenta o próprio video_id, já que as buscas em outros provedores dependem da conferência de duração que a análise faz. Valem as regras de rede do worker (uma thread por saída, cota, quarentena e pausa) e a mesma trava, então o lote do timer não roda junto.
- **Divisão de tráfego:** os metadados vêm do ytmusicapi (barato, uma chamada por página). O yt-dlp é chamado **só para o áudio, uma vez por faixa e para sempre**, por causa do cache por versão do analisador (ADR-02).
- **Conta separada do download:** o ytmusicapi, que é autenticado na sua conta, sai sempre do IP de casa. O yt-dlp **não** usa cookies da conta principal, para não ligar um bloqueio por volume à sua conta. Se cookies forem necessários, usar uma conta secundária.
- yt-dlp sempre atualizado: o YouTube muda o player com frequência. Quando o YouTube exigir PO token, usar um plugin provedor de PO token (por exemplo, `bgutil-ytdlp-pot-provider`).
- **Desafio JavaScript do YouTube:** o yt-dlp atual precisa de um runtime JS (Deno, já instalado no sistema) e do script solucionador `yt-dlp-ejs`. A dependência é `yt-dlp[default]`, que traz o script local, sem baixá-lo em tempo de execução. Sem ele, o piloto teve "n challenge solving failed" e um HTTP 403; com ele, nenhum dos dois.

### 8.2 Ritmo e fila

- **Fila retomável e serial** (`fila_download`): sobrevive a quedas e reinícios e pode rodar como systemd timer.
- **Intervalo aleatório** entre downloads (`sleep_interval` = 20 s, `max_sleep_interval` = 60 s) e `sleep_requests` entre requisições internas.
- **Cota diária por saída** (direta ou proxy). Começar com 150–300 por dia e ajustar pela taxa de erro. O dia da cota é o **dia local** (em UTC ele viraria às 21h em -03 e daria duas cotas no mesmo dia).
  - *2026-11-03:* depois de 58 downloads sem bloqueio, a cota da saída direta foi para **500/dia**, com pausa de 10–30 s, rodando o dia todo pelo serviço do systemd.
- **Backoff exponencial** com *jitter* em HTTP 429 e 403.
- **Disjuntor:** a mensagem "Sign in to confirm you're not a bot" (ou erros parecidos) põe aquela saída em **quarentena por algumas horas**. A fila segue pelas outras saídas ou espera.

### 8.3 Pool de proxies

- Os proxies são configurados em `config.toml` (`http://`, `https://`, `socks5://`). A **conexão direta é a primeira saída** do pool.
- Cada download cria uma instância de `YoutubeDL` com a opção `proxy` da saída escolhida. Escolha: rodízio entre as saídas que têm cota e não estão em quarentena.
- A tabela `proxies` guarda, por saída, cota, uso do dia, sucessos, falhas e `quarentena_ate`. Uma saída com taxa de falha alta é rebaixada automaticamente.
- **Os proxies aumentam a vazão; não substituem o throttling.** Cada saída respeita o próprio ritmo.

| Tipo de saída | Observação |
|---|---|
| Direta (casa) | Gratuita, IP residencial. É a referência. |
| VPS própria / WireGuard para máquinas suas | Barato e controlado. IPs de datacenter, porém, costumam ser mais desconfiados pelo YouTube. |
| Proxy residencial pago | É o que mais funciona, mas cobra por GB. O formato de baixo bitrate (~3–4 MB por faixa) mantém o custo baixo. |
| Proxy de datacenter genérico / gratuito | Frequentemente já bloqueado. Não recomendado. |
| Tor | **Não usar.** Costuma estar bloqueado e onera uma rede voluntária. |

**Risco registrado:** baixar pelo yt-dlp já vai contra os termos do YouTube, e o proxy não muda isso. O uso fica restrito à **análise pessoal** das suas próprias curtidas, e o áudio é apagado logo depois.

- *Implementação (2026-11-03):* **uma thread de download por saída**, cada uma com o próprio ritmo (pausa de 10–20 s, `sleep_requests` 0,75 s, como o preset `-t sleep` do yt-dlp) e a própria cota (1200/dia). A fila é reservada com um `UPDATE … RETURNING` atômico, então duas saídas nunca pegam a mesma faixa. A análise do Essentia continua serializada por uma trava: uma cópia dos modelos na memória (~2 GB) e ~5 s por faixa dão conta de ~700 faixas/hora.
- As saídas extras rodam em containers (`contrib/saidas/compose.yml`), cada um expondo um SOCKS5 só em `127.0.0.1`. **VPN:** OpenVPN + dante com `external: tun0`; se o túnel cair, o proxy falha em vez de vazar pela conexão de casa. Os DNS são os que a VPN empurra, porque o DNS embutido do Docker não funciona com todo o tráfego no túnel. **Celular:** Tailscale em modo userspace usando o celular como exit node. Só vale nos dados móveis: no Wi-Fi de casa ele sai pelo mesmo IP da conexão direta.
- A rede do host não é alterada: a VPN e o exit node valem só dentro dos containers.

### 8.4 Fallback para outros provedores (faixa completa)

- Interface `ProvedorAudio`: `buscar(faixa) -> [candidatos]` e `baixar(candidato) -> arquivo`. A ordem dos provedores é configurável.
- **Casamento de faixa:**
  - artista e título normalizados (minúsculas, sem acentos, sem "(Official Video)", "feat." etc.);
  - **duração com tolerância de ±3 s**;
  - ISRC, quando disponível.

  Abaixo do limiar de confiança, a faixa fica marcada como **"sem áudio"**. **Analisar a música errada é pior que não analisar.**
- **Versões alternativas são recusadas:** se o título do candidato tem um marcador como MIDI, cover, remix, slowed, nightcore, karaoke, piano, live ou 歌ってみた, e a faixa da biblioteca não tem, o score vai a zero.
- **Validação (#19, 2026-11-03):** 20 faixas sorteadas das curtidas, só busca. Antes da regra de versões houve 1 falso positivo ("Yuzu - Reason MIDI" para "Reason"); depois, **0 falsos positivos**, e **10 das 20** recuperáveis pelo YouTube (busca) ou pelo SoundCloud. As que ficaram sem fallback são, na maioria, OSTs da HoYoverse reenviadas por terceiros com títulos longos, onde o casamento é conservador de propósito. A busca no YouTube recusou corretamente "The Strength of Sin Is the Law" para "The Sting of Death Is Sin".
- `analises.fonte_audio` registra de onde veio o áudio, para auditoria e reanálise.

Ordem proposta:

1. **YouTube Music, pelo `video_id`**: o mesmo áudio da curtida.
2. **Outro vídeo do YouTube com a mesma música**, achado por busca e casado pela duração. Útil quando o vídeo original está indisponível na região ou com restrição de idade.
3. **SoundCloud** e **Bandcamp**, que o yt-dlp já suporta: faixas completas e sem DRM quando o artista libera o stream. A cobertura é boa para independentes e ruim para catálogo de gravadora grande.
   - *Revisado (2026-11-03):* a busca do Bandcamp responde com um desafio anti-robô em JavaScript ("Client Challenge"). O projeto não contorna isso: o provedor saiu da ordem padrão e, se for recolocado no config, falha com essa mensagem.
4. **Pasta local**: arquivos que você já tem, comprados ou ripados. São indexados por tags e por fingerprint **Chromaprint/AcoustID**, o que permite casar mesmo com tags ruins.

**Fora como fonte de áudio: Spotify, Deezer, Apple Music e Tidal.** Nesses serviços a faixa completa é protegida por DRM (Widevine, FairPlay ou criptografia própria), e baixá-la exigiria burlar essa proteção, o que não faz parte do projeto. Ferramentas como o spotDL, na prática, pegam os **metadados** no Spotify e baixam o **áudio do YouTube**, o que cai de novo no 8.1–8.3.

**O Spotify como fonte de metadados** (opcional): a Web API (client credentials) ajuda a obter ISRC e a casar faixas entre provedores. **Atenção:** para apps criados depois de 27/11/2024, endpoints como `audio-features`, `audio-analysis`, `recommendations` e `related-artists` retornam 403, e o `preview_url` vem nulo ou foi descontinuado. Em 2026 há relatos de mais restrições no *Development Mode*. Antes de depender disso, conferir no Developer Dashboard.

### 8.5 Estimativa

```
dias para a análise inicial ≈ faixas na biblioteca ÷ (cota diária × saídas ativas)
ex.: 3.000 faixas ÷ (250/dia × 2 saídas) ≈ 6 dias
```

Depois da carga inicial, só as faixas novas entram na fila.

**Números reais (piloto de 2026-11-03, 8 faixas, só a conexão direta):**

| Medida | Valor |
|---|---|
| Biblioteca | 3417 faixas (2657 curtidas únicas + faixas só em playlists), 218 h de áudio |
| Download por faixa (opus ~66 kbps) | 1,4–2,5 MB, 2–8 s |
| Análise Essentia por faixa de ~3 min (CPU, 8 núcleos) | ~5–6 s; pico de ~2,1 GB de RAM |
| Download + análise | 11–20 s por faixa (média 14–18 s) |
| Bloqueios | nenhum em 8 downloads |

Com a cota inicial de 200/dia e só a saída direta: 3410 ÷ 200 ≈ **18 dias** para a carga inicial. O gargalo é a cota, não a CPU: no ritmo de pausas de 20–60 s, a máquina daria conta de ~1500 faixas/dia. Próximo passo: um lote de 50 (critério da #22) e, sem bloqueios, subir a cota para 300 ou acrescentar uma saída.

**Alternativas registradas e rejeitadas:** analisar só um trecho (60–90 s) e usar as prévias de 30 s do Deezer/iTunes. As duas reduziriam muito o risco de bloqueio, mas foram descartadas porque você quer a análise da faixa inteira. Ficam como plano B se o bloqueio persistir.

## ADR-09: Analisador de áudio

Porte de `yt-classifier/teste.py` (modelos e `CUSTOM_GENRES` em `:44-80`, `Analyzer` em `:126-311`), com estas mudanças:

- **Decodificar uma vez** e reamostrar para 16 kHz, 11,025 kHz e 44,1 kHz, em vez de decodificar três vezes.
  - *Revisado na implementação:* a decodificação é do ffmpeg (ver ADR-08.1), mas a **reamostragem é do Essentia** (`Resample`, qualidade 4 para 16/11 kHz e 1 para 44,1 kHz, como o `MonoLoader` do protótipo). Os modelos são sensíveis ao reamostrador: com o do ffmpeg, `happy` variou 0,14 na mesma faixa.
  - A **mixagem para mono é a média dos canais.** O `-ac 1` do ffmpeg soma com ganho √2 (+3 dB, picos acima de 1,0), o que deslocava várias saídas. Há um teste de regressão para isso.
  - **Validação:** na mesma faixa, o analisador novo e o `teste.py` diferem no máximo 0,0003 (arredondamento do protótipo), com o mesmo tom, BPM e top-3 do Discogs400. Duas execuções seguidas dão resultados idênticos, byte a byte.
- **Guardar tudo:** os embeddings EffNet e MusiCNN (média temporal; opcionalmente também desvio-padrão) e **todos os vetores de probabilidade**, não só o top-k.
- **Separar logs de dados:** progresso no stderr ou no `logging`, nunca no stdout.
- **Modelos:** num caminho absoluto configurável (`~/.local/share/yt-music-curator/models`), com um manifesto (`modelos.toml`) que lista **todos** os modelos, URLs e **checksums SHA-256**. Os `.pb` deixam de ser versionados no git. `hash_modelos` vem do manifesto.
- **`versao_analisador`** é incrementada sempre que o pipeline muda, o que marca as análises antigas para refazer.
- **Medição:** tempo por faixa e por etapa, gravado em `analises.tempo_s`.
- **Heurísticas mantidas, mas marcadas como heurísticas:** `energy` e `CUSTOM_GENRES`. As saídas que hoje são calculadas e descartadas (engagement, approachability, electronic) passam a ser salvas.
- Os `Analyzer`s ficam carregados uma vez por processo do worker, que processa a fila inteira.

## ADR-10: Interfaces (CLI e MCP)

**CLI** (`curator …`):

| Comando | Função |
|---|---|
| `auth` | Configura o `browser.json` do ytmusicapi |
| `sincronizar [--forcar]` | Curtidas, playlists próprias, pertencimento e metadados |
| `hierarquia aplicar/mostrar` | Lê o `hierarquia.toml` e mostra a árvore |
| `analisar [--limite N]` | Roda o worker da fila (download e Essentia) |
| `fila status` | Pendentes, erros, quarentenas e estimativa |
| `classificar [--mae X]` | ADR-06; gera um plano |
| `receita criar/executar/listar` | ADR-05 |
| `reorganizar [--mae X]` | ADR-07; gera um plano |
| `plano mostrar/aplicar/descartar/dislikes` | Diff e aplicação **com confirmação**; `dislikes` sugere tirar das playlists o que levou dislike |
| `visualizar` | ADR-11 |
| `recomendar rapido/lento [--misturar-curtidas]` | ADR-13: músicas novas (ou misturadas, só com a flag), com confirmação |
| `recomendacoes listar/mostrar/concluir/plano/acerto` | ADR-13: histórico, status após sincronizar e taxa de acerto |

**Servidor MCP** (para o Claude Code e o Claude Desktop, usando a sua assinatura, sem custo de API):

| Ferramenta | Função |
|---|---|
| `arvore_playlists` | Hierarquia com contagens |
| `buscar_faixas`, `buscar_artistas`, `buscar_albuns` | Busca textual no banco (os nomes reais, para os filtros) |
| `descrever_vocabulario` | Rótulos válidos (gêneros, humores, features) e distribuição na biblioteca |
| `estatisticas` | Biblioteca, não alocadas e estado da análise |
| `executar_receita` | Valida e executa uma receita; devolve a lista e a explicação dos scores |
| `salvar_receita` | Persiste a receita |
| `classificar_nao_alocadas` | ADR-06 |
| `sugerir_reorganizacao` | ADR-07 |
| `criar_plano` | Gera um diff pendente |
| `recomendar_novas`, `recomendar_misturando_curtidas` | ADR-13; `modo` sem padrão: na dúvida, a IA pergunta |
| `concluir_recomendacao`, `mostrar_recomendacao`, `historico_recomendacoes`, `criar_plano_da_recomendacao`, `exemplos_de_recomendacoes` | ADR-13 |

- **Não existe ferramenta MCP que aplique planos.** A aplicação no YouTube Music só acontece por `curator plano aplicar`, depois que você revisa o diff. Isso evita que uma conversa altere suas playlists por engano.
- **Escrita no YT Music:** `create_playlist`, `add_playlist_items` e `remove_playlist_items` do ytmusicapi, com log no plano. Depois de aplicar, a playlist é sincronizada de novo para confirmar.
- *Implementação do MCP (M4, 2026-11-03):* SDK `mcp` 2.x (`MCPServer`, o antigo FastMCP), 18 ferramentas, `curator mcp` em stdio. Além das listadas acima, o servidor tem `exemplos_de_receitas`, `obter_receita_por_pedido` (cache por frase normalizada), `renomear_cluster` (a IA nomeia os clusters do M5), `mostrar_plano`, `faltantes_nas_agregadoras` e um `criar_plano_*` por origem. Erros de receita voltam como dados (`erros` com a sugestão do nome mais próximo) para a IA corrigir sozinha. As instruções proíbem afrouxar filtros sem o usuário pedir: foi essa iniciativa que derrubou o Jaccard do pedido "dormir" para 0,58 no teste de estabilidade (ver `docs/avaliacao.md`).
- *Implementação da escrita (M7):* o plano guarda o `snapshot_hash` de cada playlist afetada ao ser criado. Antes de aplicar, cada playlist é relida no YT; se mudou, o plano é recusado. O estado de cada uma (faixas, setVideoId, título) vai para um backup no próprio plano, e `curator plano desfazer` volta a ele, apagando também as playlists que o plano criou (sempre privadas). A aplicação para na primeira falha (status `parcial`). Planos acima de `[planos] limite_faixas` (200) exigem `--grande`, e fora de um terminal só aplicam com `--sim`. Ao adicionar numa filha, o plano adiciona também nas agregadoras acima dela, mantendo mãe ⊇ filhas (ADR-03).

## ADR-11: Visualizador gráfico

Uma ferramenta de curiosidade: ver como a biblioteca "se organiza" sozinha.

- `curator visualizar` gera **um HTML autocontido**, que abre offline no navegador. A primeira opção é Plotly; deck.gl se o número de pontos pesar.
  - *Implementação:* Plotly com o `plotly.min.js` embutido (~5 MB no total). O 2D usa WebGL (`scattergl`) e cai para SVG quando o navegador não tem WebGL; o 3D exige WebGL. Os 8 vizinhos de cada faixa vêm pré-calculados no arquivo, para as linhas ao clicar.
- **Projeção UMAP 2D e 3D**, separada da UMAP de ~10-d usada no clustering, com `random_state` fixo. Fica em cache em `projecoes` e só é recalculada quando a biblioteca muda (`hash_biblioteca`).
- **Cada ponto é uma música.** A cor pode ser trocada entre:
  - cluster sugerido (ADR-07);
  - playlist atual (as faixas que estão em várias playlists ganham contorno e podem ser filtradas num modo "multi");
  - gênero dominante;
  - valência × excitação;
  - BPM;
  - "não alocada".
- **Interação:**
  - hover com título, artista, playlists e as principais tags;
  - busca por nome, que destaca o ponto;
  - ao clicar numa música, linhas até os k vizinhos;
  - camada opcional com os centroides das playlists.
- **Segunda vista:** um mapa **valência × excitação** (Russell), com eixos reais e sem UMAP, interpretável diretamente.
- **Espaço selecionável:** embedding EffNet, embedding MusiCNN ou espaço semântico interpretável (ADR-04), para comparar como cada um agrupa as músicas.
- **Significado dos eixos (#57):** os eixos do UMAP não significam nada por construção (ele só preserva vizinhanças, e a projeção pode sair girada ou espelhada). Então, depois de projetar:
  - a nuvem é centrada, girada para os eixos principais dela (PCA dos próprios pontos) e cada eixo é orientado para crescer com a energia; mesmos pontos → mesma orientação;
  - cada eixo ganha a correlação de Spearman com as 16 features escalares e com a probabilidade de cada gênero-pai (soma do Discogs400). O rótulo cita as 2–3 mais fortes com o sentido (↑/↓); abaixo de |ρ| = 0,3, "sem direção clara";
  - isso é calculado no Python e guardado em `projecao_eixos`, com o mesmo `hash_biblioteca` da projeção. Uma projeção em cache sem os eixos da versão atual é refeita.
- **Vista PCA do espaço interpretável (#57):** SVD exato do espaço interpretável, em 2D e 3D. Cada eixo é uma combinação exata das features, mostrada com os maiores pesos, o sinal e a variância explicada; o sinal de cada componente põe o maior peso positivo, então mesmos dados → mesmos eixos. Na biblioteca real (04/11), o PC1 (32% da variância) é energia/envolvimento contra tristeza/relaxamento, o mesmo que o eixo 1 do UMAP acompanha nos três espaços (ρ 0,84–0,90).
- O painel "Como ler este mapa" explica a vista atual e traz a tabela completa das correlações.

## ADR-12: Segurança e higiene

- `.gitignore` para o banco, `browser.json`, `config.toml` com proxies, modelos, áudio, `.venv` e `__pycache__`. Os dois repos antigos versionavam o banco e os modelos; isso não se repete.
- As credenciais do ytmusicapi, as URLs de proxy com usuário e senha e as chaves opcionais (Spotify) ficam fora do repositório, em `~/.config/yt-music-curator/`.
- Toda escrita no YouTube Music passa por um plano revisável, com log e status.
- O áudio baixado nunca é persistido.
- **Backup (M8):** `curator backup criar` gera um zip com o banco, copiado pela API de backup online do SQLite (consistente mesmo com o worker gravando), mais `config.toml`, `hierarquia.toml` e um manifesto com versões, contagens e SHA-256 de cada arquivo. Credenciais (`browser.json`, `saidas.env`) só entram com `--com-credenciais`, e aí o zip fica com permissão 600. `curator backup restaurar` confere tudo antes de tocar em qualquer coisa, recusa backups de schema mais novo, migra os mais antigos e nunca apaga: com `--forcar`, os dados atuais são renomeados para `*.antes-da-restauracao-<data>`. Também recusa restaurar por cima com o worker rodando.

## ADR-13: Recomendações fora da biblioteca

**Problema:** pedir músicas que o usuário ainda **não** conhece, a partir do que ele já gosta, sem misturar com as curtidas (salvo pedido explícito) e sem perder o que já foi sugerido.

**Fonte principal: o rádio do YouTube Music, com várias sementes** (`get_watch_playlist(radio=True)` e a aba "relacionadas", `get_song_related`). Os IDs já são do YT, e o rádio considera o que as pessoas ouvem juntas, não só o som. Num teste com 3 curtidas sorteadas, 43, 32 e 18 de 49 faixas estavam fora da biblioteca e eram coerentes (Anamanaguchi, Reol, Mili, Yu-Peng Chen, Yuki Kajiura, Lorien Testard).
- Sementes: faixas; as k mais próximas do centroide de uma playlist; os núcleos de um cluster; as curtidas mais recentes de um artista. Dislikes nunca são sementes.
- Cada resposta vira uma **coleta** salva (snapshot). O rádio muda com o tempo, e isso é bom: depois de `cache_h` (12 h) a mesma semente é coletada de novo. Mas o ranking sempre pode ser refeito sobre a coleta salva.
- **Deezer** como fonte extra, para variar além do que o YT sugere: artistas relacionados (`/artist/{id}/related`) → faixas mais tocadas (`/top`). API pública e sem chave. Cada faixa é casada com um video_id pela busca do ytmusicapi, com as regras do ADR-08.4 (título, artista, duração ±3 s, sem variantes). Sem casamento confiável, é descartada. Os casamentos ficam em cache, inclusive os que falharam. O artista da semente só é usado se o nome no Deezer for igual (sem acento/caixa): um artista errado traria recomendações de outro mundo.

**Alternativas rejeitadas**
- **ReccoBeats** (API grátis que imita o `recommendations` do Spotify): testada em 04/11. Recomenda só por features de áudio, sem gênero nem contexto. Sementes HOYO-MiX trouxeram jazz (Barry Harris Trio) e Carole King; LISA trouxe funk e Taylor Swift. Além disso, a busca é só por título (metade das curtidas de uma amostra não foi encontrada) e não há ISRC na biblioteca.
- **Spotify `recommendations`/`related-artists`:** 403 para apps criados depois de 27/11/2024.
- **Last.fm `track.getSimilar`:** bom para nichos, mas exige chave; fica para depois, se o Deezer não bastar.

**Escopo e modo: sempre explícitos, nunca com padrão**

| Intenção | Terminal | MCP |
|---|---|---|
| Só curtidas | `curator receita executar` | `executar_receita` |
| Só músicas novas | `curator recomendar rapido\|lento` | `recomendar_novas` |
| Misturar | `curator recomendar … --misturar-curtidas` | `recomendar_misturando_curtidas` |

- Na **menor dúvida** sobre escopo ou modo, confirmar antes de executar. O terminal mostra o resumo (modo, escopo, sementes, fontes, destino) e pede confirmação (`--sim` pula; fora de um terminal, sem `--sim`, recusa). No MCP, `modo` não tem padrão: sem ele, a ferramenta devolve um erro pedindo para confirmar com o usuário. As instruções do servidor e `exemplos_de_recomendacoes` trazem os três casos de referência (ver Fluxos).
- **Modo rápido:** sem baixar nada. Cada aparição numa coleta vale `peso_da_fonte × (1 − ½·posição/tamanho)` (YT rádio 1,0; relacionadas 0,8; Deezer 0,6), somado entre sementes. Limite por artista, desempate por video_id.
- **Modo lento:** as `analisar_top` (60) melhores do ranking rápido entram na fila de download com **prioridade 1**: só baixam quando não há faixa da biblioteca pendente para a saída, sem cota própria. A recomendação fica "aguardando_analise" até a fila terminar (`curator recomendacoes concluir`, `--aguardar` ou `--parcial`). Ordem final: 0,6 × motor (similaridade com as sementes analisadas, perfil-alvo e gêneros, como numa receita; z-score sobre a biblioteca, para as candidatas não mexerem na régua) + 0,4 × coleta. `perfil_alvo` e `generos_preferidos` só existem no modo lento.
- **Popularidade e novidade (#87):** valem nos dois modos.
  - `popularidade` (alvo 0..1 no percentil das visualizações da biblioteca, em escala log) multiplica o score por exp(−peso·dz²/2). O dz é a distância ao alvo dividida pelo desvio do percentil (≈ 0,29), e uma faixa sem o dado custa um desvio, como uma feature ausente na receita.
  - `publicadas_desde` deixa só o que foi publicado a partir da data, e a faixa sem data sai.
  - As candidatas não têm esses dados: antes de ordenar, as `estatisticas_top` (100) melhores pelas coletas recebem visualizações e data pelo `get_song`, uma chamada por faixa, guardada com a validade do `sincronizar`. Refazer a recomendação usa o que foi guardado, sem rede.
  - A régua é sempre a biblioteca utilizável: uma candidata não muda o percentil das outras.
- **Misturar:** as faixas da biblioteca que os rádios trouxeram, completadas pelo motor (parecidas com as sementes, com as análises que já existem) e intercaladas às novas na proporção pedida (padrão 50%).

**Destino:** conforme o comando. Por padrão, só a lista. Com `--criar-playlist` (MCP: `criar_plano_da_recomendacao`), um plano de playlist **nova** "Descobertas · <pedido>", que é marcada `descobertas` ao ser aplicada. Acrescentar recomendações a uma playlist sua existente é recusado pelo próprio plano (`NovasNaBiblioteca`), salvo com `--adicionar-em X --permitir-novas`.

**Descobertas curtidas (#73):** `curator recomendacoes curtidas --plano` junta numa playlist "Descobertas curtidas" o que foi recomendado como novo (`conhecida = 0`) e virou curtida depois de sincronizar, na ordem da primeira recomendação. Ela é de descobertas (fora de receitas, classificação e clusters); se já existe, o plano só acrescenta as que faltam.

**Histórico e repetição**
- Toda recomendação fica salva, com as coletas usadas e a posição, score, fontes e votos de cada faixa.
- "Já ouvi" = curti depois. O status de cada item é recalculado a cada leitura, depois de `curator sincronizar`: `curtida`, `dislike`, `em_playlist` (pus numa playlist minha comum) ou `sem_reacao`.
- Curtida ou dislike saem das próximas recomendações (a curtida passa a ser biblioteca; o dislike, para sempre). O dislike é conferido no botão de like do player, antes de salvar e a cada sincronização (ADR-03), e o que parece com dislikes (mesmo artista, som parecido) é penalizado.
- Sem reação: pode voltar, com 15% a menos de score a cada recomendação anterior; depois de 3 vezes, fica fora por 30 dias.
- `curator recomendacoes acerto` dá a taxa de acerto (curtida ou posta numa playlist) e de dislike por fonte e por modo, para ajustar `[recomendacoes] pesos_fontes`. Um pedido repetido com as mesmas sementes diz quantas faixas são novas em relação à última vez.

**Custos e ritmo:** tudo é leitura no YT pela mesma conta da sincronização (ytmusicapi, com a pausa do cliente); uma chamada de rádio serve ao rádio e às relacionadas. As candidatas não geram buscas de metadados de álbum (só os álbuns da biblioteca são completados). Downloads só no modo lento, depois da biblioteca.

## ADR-14: Afinidade de gênero-pai (#83)

**Problema:** o gênero-pai de uma receita (`genero_pai_min`) era o **maior estilo** do Discogs400 daquele pai. Num pedido de rap, "Without Me" (Eminem) ficou de fora com `{"Hip Hop": 0.3}`: o maior estilo dela é 0,18 (Gangsta), mas a soma dos estilos de Hip Hop dá 1,31. Além disso, o mesmo número valia coisas diferentes em cada gênero, e as instruções do MCP diziam à IA que "0,3 já é forte".

**Decisão**
1. **Fração do Discogs400:** soma dos estilos do pai ÷ soma dos 400 estilos, ou seja, quanto da "atenção" do modelo cai naquele pai.
   - As saídas do modelo são sigmoides independentes (o JSON do modelo declara `Sigmoid`; ROC-AUC 0,954 e PR-AUC 0,206 no Discogs-4M). Elas ordenam bem, mas o valor absoluto de um estilo não é uma probabilidade calibrada.
   - Os rótulos do Discogs são por lançamento e descem para as faixas (Bogdanov et al., 2019): estilos irmãos aparecem juntos, e o modelo aprende a dividir a massa entre eles.
   - Em classificação hierárquica multirrótulo, a saída coerente de um pai é pelo menos a maior das filhas (o MCM de Giunchiglia & Lukasiewicz, 2020). O máximo é, portanto, um **limite inferior**, e subestima justamente os pais cujos estilos dividem a massa. O noisy-OR, `1 − ∏(1 − p)`, supõe estilos independentes e cresce com o número de estilos do pai: com o mesmo 0,6 passam 985 faixas de Electronic (106 estilos) e 65 de Hip Hop (26). A fração não depende do tamanho do pai nem da escala geral das saídas.
2. **Fusão com o MTG-Jamendo:** média da fração do Discogs com a fração dos rótulos equivalentes do Jamendo (tabela fixa `MAPA_JAMENDO`, sem os rótulos de década). Ressalva: as duas cabeças usam o mesmo embedding EffNet, e as frações concordam muito (ρ de 0,66 a 0,95 por gênero). Ensembles só ganham com diversidade entre os membros (Kuncheva & Whitaker, 2003), e aqui ela é baixa: no rap, a fusão soma +0,014 de AUC (IC 95% de 0,000 a 0,037), e em Electronic piora (0,891 → 0,866). Fica porque não custa nada e ajuda nos outros gêneros.
3. **Régua relativa por gênero:** a afinidade é dividida pela mediana da afinidade no **núcleo** do pai, as faixas da biblioteca que todas as cabeças põem nesse pai em 1º lugar. 1,0 = tão típica quanto a faixa mediana do núcleo; os níveis são amplo 0,7, normal 0,8 e estrito 1,0.
   - É o limite por categoria da literatura de classificação multirrótulo (Yang, 2001), necessário porque cada gênero tem a sua escala.
   - Sem gabarito, o núcleo faz o papel de rótulos fracos: faixas em que duas visões concordam, como no co-training (Blum & Mitchell, 1998). A ressalva é a mesma do item 2: as visões não são independentes.
   - Núcleo com menos de 10 faixas: núcleo amplo (1º lugar em qualquer cabeça). Sem isso, a referência global (mediana das medianas dos núcleos). Na biblioteca atual, Blues, Latin, Funk/Soul e Reggae ficam na global, e `descrever_vocabulario` avisa.
4. **A fronteira é conferida pela IA:** `examinar_genero(pai, limite)` mostra quantas faixas passam em cada nível e as faixas logo acima e logo abaixo do corte. Gênero é ambíguo também para pessoas (Lippens et al., 2004), e um sistema pode acertar uma métrica sem saber o que é o gênero (Sturm, 2014). A fronteira rap/pop não está no som: Heat Waves e Stressed Out passam do corte, e Houdini fica abaixo dele. A IA usa o que sabe dos artistas para ajustar o limite, e a receita guarda o número (ADR-05).
5. **Versão do motor:** `VERSAO_MOTOR = "2"` fica gravada em cada execução de receita (migração 016). Mesma receita + mesmo banco + mesma versão = mesma lista.

**Testado e rejeitado** (medições em `docs/avaliacao.md`)
- **Suavizar pelos vizinhos no embedding**, como na propagação de rótulos por similaridade de conteúdo (Sordo et al., 2007):
  - no EffNet não acrescenta nada, porque as cabeças já saem desse embedding;
  - no MusiCNN, +0,012 de AUC, com IC de −0,003 a +0,037;
  - de 24% a 28% dos 15 vizinhos são do mesmo artista. O próprio Sordo et al. mostra essa dependência: com filtro de artista, a precisão cai de 0,56 para 0,41.
  - Ganho dentro do ruído, custo N × N e o score de uma faixa passaria a depender das outras: fica fora.
- **Puxar para a média do artista:**
  - o ganho medido (rap 0,946 → 0,970) vem do gabarito, que dá o mesmo rótulo a todas as faixas do artista. É o efeito do artista na avaliação de gênero (Pampalk et al., 2005; Flexer, 2007);
  - na HOYO-MiX (848 faixas, de vários gêneros), ele tirou 6 das 11 faixas de Rock acima do corte;
  - com um peso por bayes empírico, que só puxa artistas coerentes, o efeito praticamente some. Fica fora.
- **Noisy-OR:** ver item 1.

**Limites conhecidos:**
- Pop e Electronic quase não se separam pelo áudio (AUC 0,85 e 0,87): estes modelos chamam a produção pop moderna de Electronic. Para eles, as instruções recomendam perfil-alvo, sementes e estilos específicos.
- Rap com cara de pop fica abaixo do corte (Houdini 0,57; Can't Hold Us 0,55).
- Os caminhos para melhorar estão na #88: modelos de outra rede e outro treino (YAMNet, que tem a classe "Rapping"; tags do MSD-MusiCNN; MAEST) e metadados de gênero.

## ADR-15: Análises extras e metadados externos (#88)

**Problema:** na #83, a fusão do Discogs400 com o MTG-Jamendo ganhou pouco porque as duas cabeças usam o mesmo embedding EffNet (ADR-14). Ensembles ganham com diversidade entre os membros (Kuncheva & Whitaker, 2003). Faltava uma visão de outra rede, de outro treino ou de fora do áudio. Ao mesmo tempo, qualquer modelo novo reabria a fila inteira (`hash_modelos`), e o download é a parte demorada.

**Decisão**
1. **Análises extras com versão própria** (`audio/extras.py`, tabela `analises_extras`, migração 017). O manifesto ganhou o campo `grupo`: o `hash_modelos` cobre só o `nucleo`, e cada extra tem um hash próprio (versão do cálculo + SHA-256 dos modelos). Um extra novo ou trocado refaz só ele, a partir dos áudios guardados (#84/#90), sem rede e sem reabrir a fila. Faixas novas recebem os extras na mesma passada do núcleo, sem decodificar o áudio de novo. A falha de um extra (ex.: faixa curta demais para o MAEST) fica registrada e não derruba os outros.
   - **Tags do MSD-MusiCNN** (`msd_tags`, 50): o modelo já rodava para o embedding, e as tags do Last.fm (Hip-Hop, rnb, pop, rock…) eram descartadas. ~2,4 s por faixa (uma segunda passada do MusiCNN).
   - **YAMNet** (`yamnet`, `yamnet_p90`, `yamnet_emb`): 521 classes do AudioSet (Gemmeke et al., 2017), treinado em vídeos do YouTube, com "Rapping", "Singing" e classes de gênero. Média e percentil 90 dos quadros de ~1 s, porque um trecho de rap numa faixa cantada some na média. ~0,9 s.
   - **MAEST 30 s** (`maest519`, `maest519_max`): transformer com 519 estilos do Discogs (Alonso-Jiménez et al., 2023). A mesma taxonomia do Discogs400, mas outra rede e outro conjunto de treino. Média e maior trecho de 30 s. ~12 s, o grosso do custo. O embedding dele custaria outra passada inteira e ficou de fora; com o áudio guardado, dá para calcular depois.
2. **Metadados externos** (`coleta/externos.py`, migração 018): Deezer (sem chave) → ISRC, BPM, ganho, popularidade, datas e gêneros do álbum; MusicBrainz (sem chave, 1 pedido/s) pelo ISRC ou por busca → gêneros e tags da gravação, do release group e do artista. O Last.fm ficou de fora: pede chave de API e cadastro para pouco além das tags que o MusicBrainz já traz. O casamento usa as regras do ADR-08 (título, artista, duração ±3 s, sem variantes). Erro de rede nunca vira cache (nova tentativa em 1 h, 2 h, 4 h… até 7 dias); `sem-casamento` e `ok` valem 90 dias. Para propagar gênero, a similaridade por tags sociais e por filtragem colaborativa foi melhor que a de áudio em Kim, Tomasik & Turnbull (2009); e as taxonomias de gênero de fontes diferentes (AllMusic, Discogs, Last.fm, Tagtraum) não coincidem, o que leva a tratá-las como visões separadas (Bogdanov et al., 2019).
3. **A IA vê as outras visões na fronteira:** `examinar_genero` mostra, em cada faixa perto do corte, a fração do pai no MAEST, "Rapping"/"Singing" do YAMNet (no Hip Hop), os gêneros externos do artista e do álbum e a `afinidade_com_maest` (item 4). A afinidade da receita não muda.
4. **A régua com o MAEST fica ao lado, e a das receitas segue a do ADR-14** (motor v2). A régua com o MAEST (`generos.relativa_com_maest`) é a média de duas réguas, a do Discogs + Jamendo e a da fração do MAEST, cada uma relativa ao mesmo núcleo (ADR-14). Uma faixa sem MAEST fica só com a primeira, e o mesmo vale para um pai com menos de 10 faixas do núcleo com MAEST.
   - **Onde ela ganha:** no rap faixa a faixa (os 64 casos difíceis da #83), a AUC vai de 0,946 para 0,993 [0,977; 1,000], com ganho pareado de +0,011 a +0,100. No 0,8, os falsos positivos do rap caem de 14% para 0% (Heat Waves 0,95 → 0,62; Stressed Out 1,03 → 0,72). No Electronic por artista, a revocação no 0,8 sobe de 0,59 para 0,78, com os mesmos falsos positivos.
   - **Onde ela perde:** no rock japonês. O MAEST dá "Pop---J-pop" de 0,5 a 0,8 a kessoku band, the peggies, Houkago Teatime e BABYMETAL, e no Discogs J-pop é etiqueta de mercado (música popular japonesa), que cobre rock, idol e anime. No 0,8, a revocação do Rock no gabarito cai de 0,84 para 0,73 na cópia do banco e para 0,79 no banco real, com o núcleo da biblioteca inteira; lá, 28 das 790 faixas em escrita japonesa saem do Rock (`docs/avaliacao.md`). Nas 96 faixas da biblioteca com J-pop forte no MAEST, o Rock vai de 34 para 12 faixas e o Pop de 34 para 58.
   - **Por que não um ajuste à mão:** tratar J-pop e K-pop como neutros conserta o rock japonês, mas tira do Pop o pop idol e de anime (as faixas sem nenhum pai ≥ 0,8 vão de 16 para 36 das 96), e o gabarito não tem pop japonês para medir isso. Qualquer regra fixa erra um dos lados.
   - **O caminho:** um gênero aprendido com os rótulos do usuário (#93). No teste de viabilidade (validação deixando o artista inteiro de fora), ele levou o Pop por artista de 0,848 para 0,982 e pôs todo o rock japonês no Rock. Mas caiu para 0,704 no rap faixa a faixa, porque rótulos por artista ensinam o som do artista; faltam rótulos por faixa nas fronteiras.
   - **Ressalva:** o Discogs400 e o MAEST aprenderam com rótulos do Discogs, que provavelmente inclui essas mesmas músicas conhecidas. Os ganhos comparam os modelos entre si; não medem generalização para música nova.
   - As duas réguas são calculadas na hora a partir dos dados guardados. Trocar a das receitas depois não exige reanalisar nada.

**Testado e não adotado** (medições em `docs/avaliacao.md`; os dados ficam guardados e visíveis):
- **YAMNet nas classes de gênero:** 0,911 no rap, pior que o motor. "Rapping" − "Singing" deu 0,966, dentro do ruído. Aparece como outra visão no `examinar_genero`.
- **Tags do MSD-MusiCNN:** 0,975 [0,939; 0,997], dentro do ruído contra o motor. A fusão com MAEST, YAMNet e MSD (0,992) não ganha da régua com o MAEST (0,993).
- **Metadados externos na régua:** deram 1,000 no rap e 0,996 no Pop por artista. Só que os gêneros são do artista e do álbum, e o gabarito por artista os favorece por construção (Flexer, 2007). Na régua, eles puxariam todas as faixas de um artista para o mesmo gênero, o oposto do que ela faz hoje, faixa a faixa. Ficam para a IA na fronteira e para uso futuro (#87). Se valem o que custam fica para a #91.
- **Suavizar pelos vizinhos no EffNet** (sem rótulo): mesma AUC, e o rock japonês não sobe, porque os vizinhos dele são outro rock japonês, com a mesma nota.
- **As outras cabeças do Essentia** (FMA, MagnaTagATune, MTG-Jamendo top50tags, MTG-Jamendo sobre o embedding "multi" e as antigas Dortmund, Rosamerica e GTZAN): nenhuma passa o MAEST no Rock, no Electronic ou no rap. A melhor no Pop (top50tags, 0,912) fica abaixo do modelo aprendido (0,982). Todas usam o mesmo EffNet do núcleo. Ficam como candidatas a atributo na #93.
- **Last.fm:** pede chave de API e cadastro para pouco além das tags que o MusicBrainz já traz.

**Limites conhecidos:**
- Os gêneros externos são do artista e do álbum muito mais que da faixa (no MusicBrainz, poucas gravações têm voto). Eles não separam as faixas de um mesmo artista, que é justamente o caso difícil (Alec Benjamin, Twenty One Pilots).
- O mapa de rótulos externos para os pais do Discogs (`generos.pais_de_rotulo`) é uma heurística por palavras.
- O MAEST custa ~12 s por faixa: a primeira passada na biblioteca inteira leva cerca de 15 h (uma vez só). Ela foi feita em 06/11 com uma opção provisória, `--so-extras`, que saiu na #92: hoje o `curator analisar` completa sozinho, depois da fila, os extras que faltam nas faixas com áudio guardado.
- Pop continua mal separado do Electronic e do pop japonês em qualquer régua fixa (AUC por artista de 0,848 a 0,875). Para ele, as instruções seguem recomendando perfil-alvo, sementes e estilos específicos. A régua aprendida (ADR-17) chega a 0,966, mas ainda fica ao lado.

---

## ADR-16: Compartilhar análises entre pessoas (#96)

**Contexto:**
- Baixar e analisar uma biblioteca leva dias: a da #90 levou ~19 h só de download, e a análise do núcleo leva ~7 s por faixa.
- Dois amigos do usuário querem usar o projeto, e as bibliotecas se sobrepõem.
- Uma análise depende só do áudio e do analisador, não de quem ouve. Pode ser feita uma vez e passada adiante.

**Decisão:** `curator analises exportar` gera um pacote, e `curator analises importar` traz o de outra pessoa (`banco/compartilhar.py`, migração 019).

1. **O que vai.** Só o que é igual para todo mundo:
   - os metadados públicos da faixa (título, artistas, álbum, duração, ISRC, explícita, tipo de vídeo, visualizações, data de publicação);
   - a análise do núcleo (features, vetores, rótulos dos modelos) e os extras da #88;
   - os metadados externos (Deezer, MusicBrainz).
2. **O que nunca vai:**
   - curtidas e a posição nelas, likes e dislikes, playlists;
   - receitas, planos, recomendações e coletas (as sementes revelam a biblioteca), clusters e projeções;
   - fila, proxies e saídas (os IPs), config e credenciais;
   - o áudio e qualquer caminho local (o `fonte_ref` da pasta local sai vazio);
   - os tempos da máquina e as datas que mostram quando a faixa entrou na biblioteca;
   - as faixas de playlists ignoradas (#77).

   O esquema do pacote é uma lista fechada de tabelas e colunas (`TABELAS`). Uma coluna nova no banco não vai junto sem alguém pôr ela ali.
3. **Formato:** um zip com `manifesto.json` (versões, contagens, SHA-256) e `analises.db`, um SQLite só com essas tabelas.
4. **Importar é tratar dado não confiável.** Antes de gravar qualquer coisa, a importação confere:
   - o zip (só os dois arquivos, tamanho máximo);
   - o SHA-256;
   - o `integrity_check`, com `trusted_schema = OFF`;
   - o esquema (sem view nem trigger);
   - os ids, a dimensão de cada vetor contra os rótulos, os valores finitos e as referências entre as tabelas.

   Qualquer falha recusa o pacote inteiro.
5. **Precedência:**
   - Só entra análise com a versão do analisador e o hash dos modelos atuais.
   - Um extra entra só com o hash atual dele e sobre a mesma análise (mesma fonte de áudio).
   - Uma análise local válida nunca é sobrescrita. Uma local com erro ou desatualizada é trocada, porque seria refeita de qualquer jeito.
   - Quem estava na fila sai dela.
   - O mesmo pacote de novo, depois de uma atualização do analisador por exemplo, traz só o que faltava, na mesma importação.
6. **Importada continua NOVA.**
   - Conhecida é só curtida ∪ playlist própria (`consultas.BIBLIOTECA`). Ter análise ou estar em `faixas` não conta.
   - A base do z-score, das réguas de gênero, dos clusters e do mapa é `espacos.da_biblioteca`.
   - A importação nunca escreve curtida, avaliação nem playlist. Por isso a biblioteca, as recomendações, a régua e as receitas ficam idênticas antes e depois (teste em `tests/test_compartilhar.py`).
   - O que muda é bom: uma candidata de recomendação já analisada não vai para a fila do modo lento, e uma faixa importada que o usuário curtir depois entra na biblioteca sem download.
7. **Origem e desfazer.**
   - Cada linha importada guarda o `importacao_id`. Uma análise refeita aqui volta a ser local sozinha, porque o worker regrava a linha inteira.
   - `curator analises remover` apaga só o que veio da importação. As faixas da biblioteca que perdem a análise voltam para a fila.
8. **Escopo e privacidade.**
   - A lista de faixas do pacote, sozinha, mostra o que a pessoa escuta: é o preço da ideia.
   - `--so-biblioteca` deixa de fora as candidatas de recomendação analisadas.
   - As análises importadas só são repassadas com `--com-importadas`, para a origem não se perder.
9. **Tamanho** (biblioteca real, 07/11):
   - 3.327 faixas dão 86 MB zipado e 152 MB descompactado, ~26 KB por faixa;
   - sem os extras, 47 MB;
   - a exportação leva ~3 s, e a importação num banco vazio ~1 s.

**Alternativas rejeitadas:**
- **Mandar o backup (#50):** leva o banco inteiro, com curtidas, playlists e receitas.
- **Exportar em JSON:** os vetores em float32 ficariam ~4× maiores e mais lentos de conferir.
- **Uma API ou servidor compartilhado:** é infraestrutura demais para um grupo de amigos.

---

## ADR-17: Gênero-pai aprendido com os rótulos do usuário (#93)

**Contexto:** as réguas do ADR-14 e do ADR-15 são zero-shot. Usam a taxonomia do Discogs e não sabem o que o usuário chama de cada gênero. No Discogs, "J-pop" é etiqueta de mercado, e por isso a régua com o MAEST tira o rock japonês do Rock. Nenhuma regra fixa acerta os dois lados (ADR-15).

**Decisão:** um modelo por gênero-pai, aprendido com os rótulos do usuário (`motor/aprendido.py`, migração 020). Por ora, ele fica **ao lado**, e as receitas seguem a régua v2.

1. **Rótulos.**
   - Ficam em `rotulos_genero` ("esta faixa é / não é Rock") e **nunca** vêm das playlists existentes.
   - Começam pelo gabarito pessoal (`curator generos importar-gabarito`): um artista do gabarito rotula todas as faixas dele, e as faixas do gabarito faixa a faixa entram uma a uma. Um rótulo da faixa vence um do artista.
   - Os negativos implícitos: uma faixa marcada como de outro pai, e sem rótulo neste, conta como "não".
   - Os rótulos são pessoais e ficam fora do pacote da #96.
2. **Modelo.**
   - Regressão logística L2 (λ = 20), com as classes balanceadas e resolvida por IRLS na forma dual, em numpy.
   - Atributos: o embedding do EffNet e as 519 saídas do MAEST, em z-score sobre a biblioteca.
   - É determinística: mesmos rótulos + mesma biblioteca → mesma afinidade. A versão é o hash dos rótulos.
   - Só tem modelo o pai com 20 positivos de 3 artistas e 20 negativos. Os outros ficam com a régua do ADR-14.
3. **Escala.** A mesma ideia da régua: 1,0 = tão típica quanto a mediana do núcleo aprendido, que são as faixas da biblioteca com p ≥ 0,5. Os níveis 0,7 / 0,8 / 1,0 valem igual.
4. **Portão.** O modelo só conhece os pais rotulados e, sem portão, decide entre eles até para o que não é nenhum: O Lago dos Cisnes ia para o Hip Hop, e 70% das faixas clássicas e de trilha sonora iam para o Electronic. Por isso ele só julga a faixa que a régua v2 põe em primeiro lugar num pai com modelo. Isso é reconhecimento em conjunto aberto, como em Scheirer et al. (2013).
5. **Os rótulos vencem.** Uma faixa marcada "sim" tem pelo menos 1,0, e uma marcada "não" fica com 0, com ou sem portão.
6. **Aprendizado ativo** (Settles, 2009). `curator generos rotular PAI` e as ferramentas `faixas_para_rotular`/`rotular_genero` do MCP perguntam primeiro as faixas sem rótulo que as réguas (v2, com o MAEST, aprendida) põem de lados opostos do corte 0,8. Depois vêm as perto do corte, com no máximo 2 por artista e sem repetir a mesma música. A IA só grava a resposta do usuário.
7. **Por que ao lado, e não nas receitas.** Deixando o artista inteiro de fora (10 dobras por artista), o modelo passa nos três critérios da #93:
   - rap faixa a faixa com AUC 1,000;
   - Pop por artista com AUC 0,966 (régua v2: 0,854);
   - 98% do rock japonês ≥ 0,8 no Rock (régua com o MAEST: 54%).

   Mas o gabarito só tem rock japonês e nenhum pop japonês nem de anime. O modelo aprendeu que "produção japonesa de anime = Rock": põe no Rock músicas de idol e de personagem (Petit Rabbit's, 七森中☆ごらく部) e tira do Pop ClariS e YOASOBI. Também põe trilhas de jogo (HOYO-MiX) no Hip Hop. Trocar a régua das receitas agora trocaria um erro por outro.

   Essas faixas são exatamente as primeiras que o aprendizado ativo pergunta. Com elas rotuladas, a decisão volta a ser medida (`contrib/avaliacao/genero_aprendido.py`). Para adotar a régua: `generos.REGUA_RECEITAS = "aprendida"` e um `VERSAO_MOTOR` novo. A versão do banco da execução já leva o hash dos rótulos.

**Testado e não adotado:**
- **YAMNet e as tags do MSD como atributos a mais:** dentro do ruído.
- **A média da aprendida com uma das réguas:** devolve parte do rock japonês ao Pop (87–89%) e derruba a revocação do Pop no 0,8 para 0,36.
- **Negativos de fundo** (o núcleo dos pais sem rótulo como "não"): pioram o Electronic (AUC 0,966 → 0,908), porque trilha de jogo e eletrônica se parecem.
- **Aprendizado por reforço:** exige milhares de tentativas com recompensa e explora ao acaso.

---

## Fluxos dos pedidos de exemplo

| Pedido | Fluxo |
|---|---|
| "Quero uma playlist para dirigir, músicas que me deem vontade de sair fazendo drift" | A IA consulta `descrever_vocabulario` e monta uma receita com `perfil_alvo` de energy, aggressive e arousal altos, BPM 120–160 e danceability média/alta, e `generos_preferidos` com os estilos eletrônicos e agressivos existentes (como industrial, phonk e drum & bass, se estiverem no vocabulário). Pode usar como `sementes` 3–5 faixas que você citar. → `executar_receita` → você ajusta → `salvar_receita` → plano. |
| "Quero uma playlist de rap" | Receita com filtro duro: gênero-pai **Hip Hop** ≥ 0,8 na régua relativa (ADR-14), conferida antes com `examinar_genero` (que mostra também a régua com o MAEST, ADR-15), e `max.instrumental` 0,5 para tirar o lo-fi instrumental. Sem perfil-alvo, ordenada pela afinidade do gênero e com diversidade por artista. |
| "Quero uma playlist de rock geral, e uma playlist com meus subgêneros centrais de rock" | (1) Receita com o gênero-pai **Rock** ≥ 0,7 (nível amplo, ADR-14). (2) Dentro desse universo, o motor soma a massa dos estilos `Rock---*`, escolhe os estilos que concentram a maior parte da sua biblioteca, ou roda `sugerir_reorganizacao` restrito a esse universo, e gera uma playlist por subgênero central. Uma faixa pode entrar em várias. Se a árvore já tiver "Rock - All" e filhas, o plano propõe ajustes nela em vez de duplicar. |
| "Veja todas minhas músicas curtidas que ainda não estão em uma playlist, e me diga onde colocar" | `classificar_nao_alocadas` (ADR-06): multi-rótulo hierárquico. As faixas "sem destino" vêm agrupadas como possíveis playlists novas. → plano. |
| "Veja todas as minhas músicas. Sugira playlists para eu poder organizar melhor" | `sugerir_reorganizacao` (ADR-07): clusters sobrepostos, nomes dados pela IA a partir das estatísticas, sugestões de fusão e divisão comparando com a árvore atual. → plano. |
| "Quero uma playlist de pop MISTURANDO minhas curtidas e músicas que não conheço" | Pedido explícito de misturar: `recomendar_misturando_curtidas` com a playlist de pop como semente (ADR-13). Se o modo (rápido/lento) não foi dito, a IA pergunta antes. |
| "Me recomende uma playlist pra dirigir" | Não diz se é das curtidas, de músicas novas ou das duas: a IA **pergunta antes de chamar qualquer ferramenta** (ADR-13). |
| "Quero músicas parecidas com a música X, ou com a playlist Y" | "Parecidas com" = músicas novas: `recomendar_novas` com X (`buscar_faixas`) ou Y como semente. Pergunta o modo se não foi dito. |
| "Quero uma playlist de músicas para dormir, mas não inclua nenhuma da Hoyoverse" | A IA chama `buscar_artistas("hoyo")` e `buscar_albuns` (Genshin, Star Rail, ZZZ…) e coloca os nomes reais em `excluir_*`. O perfil-alvo é relaxed alto e arousal e energy baixos (exemplo de receita no ADR-05). A exclusão é uma comparação exata. |

## Roteiro

| Fase | Entrega | Critério de pronto |
|---|---|---|
| 1. Coleta e banco | `auth`, `sincronizar`, schema e migrações, `hierarquia.toml` | Curtidas e todas as playlists próprias no banco. Remover uma música no YT e sincronizar a remove do banco. A lista de não alocadas confere com o app. |
| 2. Fila e analisador | Fila, provedores, proxies, analisador portado | 50 faixas analisadas sem bloqueio. Vetores completos salvos. Reanalisar não baixa nada. Tempo por faixa medido. |
| 3. Motor | Normalização, receitas, kNN, ADR-06 | A mesma receita 2× dá resultado idêntico. Classificação *leave-one-out* nas filhas do "Rock - All" com precisão aceitável. |
| 4. MCP | Servidor e ferramentas | Os 6 pedidos de exemplo funcionam pelo Claude Code. A mesma frase, traduzida duas vezes, dá Jaccard ≥ 0,8. |
| 5. Reorganização | UMAP, HDBSCAN, sobreposição, nomes | Os clusters são estáveis entre execuções e as sugestões de fusão aparecem. |
| 6. Visualizador | HTML 2D/3D e valência × excitação | Abre offline e as cores e buscas funcionam. |
| 7. Aplicar planos | Escrita no YT Music | O plano aplicado aparece no app e a ressincronização confirma. |
| 9. Avaliações e preservação | Dislikes, trava das análises | Faixa que sai de tudo mantém a análise; dislike fora de toda lista. |
| 10. Recomendações | Rádio do YT + Deezer, rápido/lento, histórico | O mesmo pedido em dias diferentes gera recomendações salvas; curtidas e dislikes saem das próximas; nada entra nas playlists das curtidas sem pedido explícito. |

## Riscos e questões em aberto

- **Viés dos modelos:** os modelos de gênero do Essentia têm viés para o catálogo ocidental. J-Rock, kawaii metal e OSTs de jogos podem receber rótulos estranhos. Mitigações: o embedding e o kNN dependem menos dos rótulos; a régua do gênero-pai é relativa à própria biblioteca (ADR-14); modelos de outra rede (YAMNet, tags do MSD-MusiCNN) e metadados de gênero estão na #88.
- **Mudanças no YouTube:** o yt-dlp pode quebrar ou passar a exigir PO token, e a fila precisa aguentar parar por dias.
- **Tamanho da biblioteca:** medido em 3417 faixas (218 h). O brute-force em numpy sobra (ADR-02); a cota define os ~18 dias da carga inicial (ADR-08.5).
- **Custo do Essentia na CPU:** medido em ~5–6 s por faixa de 3 min e ~2,1 GB de RAM. Não é o gargalo (a cota é), então paralelismo e GPU ficam fora por enquanto.
- **Playlists com poucos membros:** o kNN fica frágil, e por isso há o fallback descritivo do ADR-06.
- **Leitura do botão de like do player:** usa a resposta crua do endpoint `next`, que o ytmusicapi não expõe (ADR-03). Se o YouTube mudar o formato, a conferência para de achar o botão (`sem_resposta` no resumo) e os dislikes fora das playlists deixam de ser vistos, sem quebrar o resto.
- **Hierarquia com mais de uma mãe:** hoje é uma árvore (uma mãe só). Se precisar de DAG, troca-se `mae_id` por uma tabela de arestas.

## Referências

- Alonso-Jiménez, P., Serra, X., & Bogdanov, D. (2022). *Music Representation Learning Based on Editorial Metadata from Discogs*. ISMIR 2022, pp. 825–833. doi:10.5281/zenodo.7316790. Modelos em <https://essentia.upf.edu/models.html>.
- Bogdanov, D., Won, M., Tovstogan, P., Porter, A., & Serra, X. (2019). *The MTG-Jamendo Dataset for Automatic Music Tagging*. ML4MD Workshop, ICML 2019. <http://hdl.handle.net/10230/42015>.
- Russell, J. A. (1980). *A circumplex model of affect*. Journal of Personality and Social Psychology, 39(6), 1161–1178.
- Rocchio, J. J. (1971). *Relevance feedback in information retrieval*. In G. Salton (Ed.), The SMART Retrieval System.
- Carbonell, J., & Goldstein, J. (1998). *The use of MMR, diversity-based reranking for reordering documents and producing summaries*. SIGIR 1998.
- Ludewig, M., Kamehkhosh, I., Landia, N., & Jannach, D. (2018). *Effective Nearest-Neighbor Music Recommendations*. RecSys Challenge 2018 Workshop. doi:10.1145/3267471.3267474.
- Zamani, H., Schedl, M., Lamere, P., & Chen, C.-W. (2019). *An Analysis of Approaches Taken in the ACM RecSys Challenge 2018 for Automatic Music Playlist Continuation*. ACM TIST, 10(5). arXiv:1810.01520.
- McInnes, L., Healy, J., & Melville, J. (2018). *UMAP: Uniform Manifold Approximation and Projection for Dimension Reduction*. arXiv:1802.03426.
- Campello, R. J. G. B., Moulavi, D., & Sander, J. (2013). *Density-Based Clustering Based on Hierarchical Density Estimates*. PAKDD 2013.
- Atil, B., et al. (2024). *Non-Determinism of "Deterministic" LLM Settings*. arXiv:2408.04667.
- Bogdanov, D., Porter, A., Schreiber, H., Urbano, J., & Oramas, S. (2019). *The AcousticBrainz Genre Dataset: Multi-Source, Multi-Level, Multi-Label, and Large-Scale*. ISMIR 2019, pp. 360–367.
- Giunchiglia, E., & Lukasiewicz, T. (2020). *Coherent Hierarchical Multi-Label Classification Networks*. NeurIPS 2020. arXiv:2010.10151.
- Yang, Y. (2001). *A study of thresholding strategies for text categorization*. SIGIR 2001, pp. 137–145. doi:10.1145/383952.383975.
- Blum, A., & Mitchell, T. (1998). *Combining labeled and unlabeled data with co-training*. COLT 1998.
- Kuncheva, L. I., & Whitaker, C. J. (2003). *Measures of diversity in classifier ensembles and their relationship with the ensemble accuracy*. Machine Learning, 51(2), 181–207.
- Sordo, M., Laurier, C., & Celma, Ò. (2007). *Annotating Music Collections: How Content-Based Similarity Helps to Propagate Labels*. ISMIR 2007, pp. 531–534.
- Pampalk, E., Flexer, A., & Widmer, G. (2005). *Improvements of audio-based music similarity and genre classification*. ISMIR 2005.
- Flexer, A. (2007). *A closer look on artist filters for musical genre classification*. ISMIR 2007.
- Lippens, S., Martens, J.-P., & De Mulder, T. (2004). *A comparison of human and automatic musical genre classification*. ICASSP 2004.
- Gemmeke, J. F., Ellis, D. P. W., Freedman, D., Jansen, A., Lawrence, W., Moore, R. C., Plakal, M., & Ritter, M. (2017). *Audio Set: An ontology and human-labeled dataset for audio events*. ICASSP 2017. YAMNet: <https://github.com/tensorflow/models/tree/master/research/audioset/yamnet>.
- Alonso-Jiménez, P., Serra, X., & Bogdanov, D. (2023). *Efficient Supervised Training of Audio Transformers for Music Representation Learning*. ISMIR 2023. arXiv:2309.16418.
- Kim, J. H., Tomasik, B., & Turnbull, D. (2009). *Using Artist Similarity to Propagate Semantic Information*. ISMIR 2009.
- Settles, B. (2009). *Active Learning Literature Survey*. Computer Sciences Technical Report 1648, University of Wisconsin–Madison.
- Scheirer, W. J., de Rezende Rocha, A., Sapkota, A., & Boult, T. E. (2013). *Toward Open Set Recognition*. IEEE Transactions on Pattern Analysis and Machine Intelligence, 35(7), 1757–1772.
- Sturm, B. L. (2014). *A simple method to determine if a music information retrieval system is a "horse"*. IEEE Transactions on Multimedia, 16(6), 1636–1644. doi:10.1109/TMM.2014.2330697.
