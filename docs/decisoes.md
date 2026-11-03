# Decisões de projeto: yt-music-curator

> Estado: **fase de design**, sem código ainda. Data: 2026-11-03.
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

**Regras de cache e invalidação**

- **Pertencimento:** a sincronização de cada playlist é **total, dentro de uma transação**: apaga e reinsere `playlist_faixas`. Isso corrige o bug do helper, em que uma música removida continuava no cache. A playlist só é baixada de novo quando a contagem ou o `snapshot_hash` muda, ou com `--forcar`.
- **Metadados** (álbum, artista): TTL de 30 dias, configurável.
- **Falha não é cache.** Uma chamada que falha não grava resultado vazio. O item vai para a fila com backoff (bug do helper).
- **Análise:** é feita **uma vez por faixa**. Só é refeita quando `versao_analisador` ou `hash_modelos` mudam. O áudio nunca é guardado.
- **Vetores completos.** Todos os vetores de probabilidade (400, 87, 56, 40 dimensões) e os embeddings ficam salvos, para que filtros e receitas novas não precisem de reanálise.

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

## ADR-07: Sugerir reorganização (clusters sobrepostos)

**Decisão:**

1. **Redução de dimensão:** UMAP (McInnes et al., 2018) do embedding para ~10 dimensões, com métrica cosseno e `random_state` fixo.
2. **Clustering:** HDBSCAN (Campello, Moulavi & Sander, 2013), via `sklearn.cluster.HDBSCAN`, com `min_cluster_size` configurável (por exemplo, 12 faixas). Roda na biblioteca toda ou dentro de uma mãe ("reorganizar o Rock").
3. **Pertencimento sobreposto:** uma música entra em todas as playlists sugeridas com que é parecida o bastante.
   - O HDBSCAN gera uma partição, que é só o ponto de partida. Cada cluster tem um núcleo (os membros com maior probabilidade) e um centroide.
   - Cada faixa entra em **todo cluster** em que `cos(t, centroide_c) ≥ τ_c`. O limiar τ_c é o **percentil 25 da similaridade dos membros do núcleo de c**. Cada cluster tem sua régua, não há uma régua global.
   - Os pontos que o HDBSCAN marca como ruído também podem entrar em clusters por essa regra. O que sobrar continua "sem cluster".
   - A sobreposição entre os clusters *a* e *b* é medida por Jaccard. Se passar de ~0,6, as duas playlists provavelmente são uma só, e o sistema sugere fundi-las.
   - Alternativa registrada: os *soft membership vectors* do pacote `hdbscan`. A regra por centroide foi escolhida por ser mais simples de explicar e de auditar.
4. **Descrição:** cada cluster recebe estatísticas agregadas: estilos dominantes (massa média do Discogs400), humor, BPM mediano, valência e excitação, e os artistas mais frequentes.
5. **Nome:** a IA dá nome a cada cluster a partir **apenas dessas estatísticas**, com nomes em cache por `hash` do conteúdo do cluster.
6. **Saída:** um **plano** com playlists novas, fusões, divisões e movimentações. Cada faixa aparece em todas as playlists sugeridas a que pertence. Nada é aplicado sem confirmação (ADR-10).

**Determinismo:** os mesmos dados e os mesmos parâmetros (incluindo `random_state`) dão os mesmos clusters. Quando entram faixas novas, os clusters existentes são mantidos como âncoras: as faixas novas são atribuídas por centroide, e o reclustering completo só acontece quando você pede. Assim, os nomes e o pertencimento não mudam o tempo todo.

## ADR-08: Obtenção de áudio sem bloqueio

**Requisito:** sempre a **faixa inteira**. Ordem de tentativas: YouTube direto → YouTube por proxies → outros provedores sem DRM.

### 8.1 Configuração do yt-dlp

- **Formato:** o áudio de **menor bitrate** aceitável (`bestaudio[abr<=70]/worstaudio`, normalmente opus 249/250). Os modelos reamostram para 16 kHz (EffNet/MusiCNN), 11,025 kHz (TempoCNN) e 44,1 kHz (ritmo/tom), então a perda de qualidade é irrelevante para a análise e o tráfego cai bastante.
- **Sem conversão para WAV:** o áudio é decodificado pelo `ffmpeg` do sistema direto para a memória (pipe), sem arquivo intermediário.
  - *Revisado na implementação (2026-11-03):* o `MonoLoader` do essentia-tensorflow 2.1b6 **não** abre opus/webm ("Unsupported codec"). Por isso o ffmpeg decodifica na taxa e nos canais nativos, e a reamostragem continua com o `Resample` do Essentia (ver ADR-09).
- O áudio vai para um diretório temporário e é **apagado logo após a análise**.
- **Divisão de tráfego:** os metadados vêm do ytmusicapi (barato, uma chamada por página). O yt-dlp é chamado **só para o áudio, uma vez por faixa e para sempre**, por causa do cache por versão do analisador (ADR-02).
- **Conta separada do download:** o ytmusicapi, que é autenticado na sua conta, sai sempre do IP de casa. O yt-dlp **não** usa cookies da conta principal, para não ligar um bloqueio por volume à sua conta. Se cookies forem necessários, usar uma conta secundária.
- yt-dlp sempre atualizado: o YouTube muda o player com frequência. Quando o YouTube exigir PO token, usar um plugin provedor de PO token (por exemplo, `bgutil-ytdlp-pot-provider`).
- **Desafio JavaScript do YouTube:** o yt-dlp atual precisa de um runtime JS (Deno, já instalado no sistema) e do script solucionador `yt-dlp-ejs`. A dependência é `yt-dlp[default]`, que traz o script local, sem baixá-lo em tempo de execução. Sem ele, o piloto teve "n challenge solving failed" e um HTTP 403; com ele, nenhum dos dois.

### 8.2 Ritmo e fila

- **Fila retomável e serial** (`fila_download`): sobrevive a quedas e reinícios e pode rodar como systemd timer.
- **Intervalo aleatório** entre downloads (`sleep_interval` = 20 s, `max_sleep_interval` = 60 s) e `sleep_requests` entre requisições internas.
- **Cota diária por saída** (direta ou proxy). Começar com 150–300 por dia e ajustar pela taxa de erro.
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

### 8.4 Fallback para outros provedores (faixa completa)

- Interface `ProvedorAudio`: `buscar(faixa) -> [candidatos]` e `baixar(candidato) -> arquivo`. A ordem dos provedores é configurável.
- **Casamento de faixa:**
  - artista e título normalizados (minúsculas, sem acentos, sem "(Official Video)", "feat." etc.);
  - **duração com tolerância de ±3 s**;
  - ISRC, quando disponível.

  Abaixo do limiar de confiança, a faixa fica marcada como **"sem áudio"**. **Analisar a música errada é pior que não analisar.**
- `analises.fonte_audio` registra de onde veio o áudio, para auditoria e reanálise.

Ordem proposta:

1. **YouTube Music, pelo `video_id`**: o mesmo áudio da curtida.
2. **Outro vídeo do YouTube com a mesma música**, achado por busca e casado pela duração. Útil quando o vídeo original está indisponível na região ou com restrição de idade.
3. **SoundCloud** e **Bandcamp**, que o yt-dlp já suporta: faixas completas e sem DRM quando o artista libera o stream. A cobertura é boa para independentes e ruim para catálogo de gravadora grande.
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
| `plano mostrar/aplicar/descartar` | Diff e aplicação **com confirmação** |
| `visualizar` | ADR-11 |

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

- **Não existe ferramenta MCP que aplique planos.** A aplicação no YouTube Music só acontece por `curator plano aplicar`, depois que você revisa o diff. Isso evita que uma conversa altere suas playlists por engano.
- **Escrita no YT Music:** `create_playlist`, `add_playlist_items` e `remove_playlist_items` do ytmusicapi, com log no plano. Depois de aplicar, a playlist é sincronizada de novo para confirmar.

## ADR-11: Visualizador gráfico

Uma ferramenta de curiosidade: ver como a biblioteca "se organiza" sozinha.

- `curator visualizar` gera **um HTML autocontido**, que abre offline no navegador. A primeira opção é Plotly; deck.gl se o número de pontos pesar.
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

## ADR-12: Segurança e higiene

- `.gitignore` para o banco, `browser.json`, `config.toml` com proxies, modelos, áudio, `.venv` e `__pycache__`. Os dois repos antigos versionavam o banco e os modelos; isso não se repete.
- As credenciais do ytmusicapi, as URLs de proxy com usuário e senha e as chaves opcionais (Spotify) ficam fora do repositório, em `~/.config/yt-music-curator/`.
- Toda escrita no YouTube Music passa por um plano revisável, com log e status.
- O áudio baixado nunca é persistido.

---

## Fluxos dos pedidos de exemplo

| Pedido | Fluxo |
|---|---|
| "Quero uma playlist para dirigir, músicas que me deem vontade de sair fazendo drift" | A IA consulta `descrever_vocabulario` e monta uma receita com `perfil_alvo` de energy, aggressive e arousal altos, BPM 120–160 e danceability média/alta, e `generos_preferidos` com os estilos eletrônicos e agressivos existentes (como industrial, phonk e drum & bass, se estiverem no vocabulário). Pode usar como `sementes` 3–5 faixas que você citar. → `executar_receita` → você ajusta → `salvar_receita` → plano. |
| "Quero uma playlist de rap" | Receita com filtro duro: gênero-pai **Hip Hop** com probabilidade ≥ limiar (Discogs400 ou Jamendo genre), sem perfil-alvo, ordenada pela probabilidade do gênero e com diversidade por artista. |
| "Quero uma playlist de rock geral, e uma playlist com meus subgêneros centrais de rock" | (1) Receita com o gênero-pai **Rock** ≥ limiar. (2) Dentro desse universo, o motor soma a massa dos estilos `Rock---*`, escolhe os estilos que concentram a maior parte da sua biblioteca, ou roda `sugerir_reorganizacao` restrito a esse universo, e gera uma playlist por subgênero central. Uma faixa pode entrar em várias. Se a árvore já tiver "Rock - All" e filhas, o plano propõe ajustes nela em vez de duplicar. |
| "Veja todas minhas músicas curtidas que ainda não estão em uma playlist, e me diga onde colocar" | `classificar_nao_alocadas` (ADR-06): multi-rótulo hierárquico. As faixas "sem destino" vêm agrupadas como possíveis playlists novas. → plano. |
| "Veja todas as minhas músicas. Sugira playlists para eu poder organizar melhor" | `sugerir_reorganizacao` (ADR-07): clusters sobrepostos, nomes dados pela IA a partir das estatísticas, sugestões de fusão e divisão comparando com a árvore atual. → plano. |
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

## Riscos e questões em aberto

- **Viés dos modelos:** os modelos de gênero do Essentia têm viés para o catálogo ocidental. J-Rock, kawaii metal e OSTs de jogos podem receber rótulos estranhos. Mitigações: o embedding e o kNN dependem menos dos rótulos; considerar enriquecer com tags do Last.fm (API gratuita) num ADR futuro.
- **Mudanças no YouTube:** o yt-dlp pode quebrar ou passar a exigir PO token, e a fila precisa aguentar parar por dias.
- **Tamanho da biblioteca:** medido em 3417 faixas (218 h). O brute-force em numpy sobra (ADR-02); a cota define os ~18 dias da carga inicial (ADR-08.5).
- **Custo do Essentia na CPU:** medido em ~5–6 s por faixa de 3 min e ~2,1 GB de RAM. Não é o gargalo (a cota é), então paralelismo e GPU ficam fora por enquanto.
- **Playlists com poucos membros:** o kNN fica frágil, e por isso há o fallback descritivo do ADR-06.
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
