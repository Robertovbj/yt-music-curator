# yt-music-curator

Organiza a biblioteca do YouTube Music (curtidas e playlists) a partir das features de áudio extraídas pelo Essentia e de uma hierarquia de playlists mãe/filha. Gera playlists reprodutíveis a partir de pedidos em linguagem natural, feitos pelo Claude via MCP.

Junta as ideias de [`yt-music-helper`](../yt-music-helper), que cuida de cache, playlists e hierarquia, e [`yt-classifier`](../yt-classifier), que faz a análise de áudio com Essentia. As decisões de arquitetura estão em [`docs/decisoes.md`](docs/decisoes.md); o roteiro está nas [issues](https://github.com/Robertovbj/yt-music-curator/issues).

## Instalação

Requer Python 3.14 e [uv](https://docs.astral.sh/uv/) (`mise use -g uv`).

```sh
uv sync                    # coleta, banco e CLI
uv sync --extra analise    # + Essentia e yt-dlp, para o worker de análise (~850 MB)
```

Nada pessoal fica no repositório:

| O quê | Onde |
|---|---|
| Credenciais (`browser.json`), `config.toml`, `hierarquia.toml` | `~/.config/yt-music-curator/` |
| Banco SQLite, modelos Essentia | `~/.local/share/yt-music-curator/` |

## Uso

```sh
# 1. credenciais: copie os headers de uma requisição /browse do music.youtube.com (F12 → Rede)
uv run curator auth --clipboard
uv run curator auth testar

# 2. espelhar a biblioteca (curtidas, playlists próprias, álbuns)
uv run curator sincronizar

# 3. hierarquia mãe/filha
uv run curator hierarquia sugerir --salvar          # rascunho a partir de títulos "Rock - ..."
uv run curator importar-helper ../yt-music-helper/cache_yt_music.db   # ou traz do helper antigo
uv run curator hierarquia aplicar                   # valida e grava no banco
uv run curator hierarquia mostrar
uv run curator hierarquia verificar                 # agregadoras contêm todas as filhas?

# playlists que não são de música (vídeos do YouTube comum, aulas): fora de tudo
uv run curator playlists ignorar "Valorant Plays"   # aceita várias; `considerar` desfaz
uv run curator playlists ignoradas

# 4. visão geral
uv run curator estatisticas
```

Uma playlist ignorada some das listagens, do mapa, das receitas, da classificação, das recomendações e do MCP, e nenhum plano mexe nela. Os vídeos que só estão em playlists ignoradas também saem de tudo, mesmo curtidos, e não vão para a fila de análise; quem também está numa playlist normal continua valendo por ela. Análises já feitas ficam guardadas.

A sincronização é total: uma música removida de uma playlist no YouTube Music some do banco. Uma playlist com a mesma contagem, sincronizada há menos de 24 h, não é baixada de novo (`--forcar` ignora esses caches).

O `sincronizar` também guarda, de cada faixa, se é explícita e o tipo do vídeo (faixa de áudio, clipe ou vídeo de usuário), e busca as visualizações no YouTube e a data de publicação. Essas duas pedem uma chamada por faixa, então saem 300 por sincronização (`[metadados] estatisticas_por_rodada`; `--sem-estatisticas` pula). A API não informa quantas vezes você ouviu cada música.

**Metadados externos.** O `sincronizar` também casa até 100 faixas por rodada (`[externos] por_rodada`; `--sem-externos` pula) com o Deezer e o MusicBrainz, pelo título, artista e duração (o MusicBrainz pelo ISRC do Deezer quando há). Ficam guardados os gêneros por faixa, álbum e artista, mais ISRC, BPM, popularidade no Deezer e datas. Se essas fontes valem o que custam ainda está em aberto (#91). O MusicBrainz aceita um pedido por segundo e pede um contato no User-Agent (`[externos] contato`, uma URL ou um e-mail).

```sh
uv run curator externos coletar      # todas as pendentes (pode ser interrompido e retomado)
uv run curator externos status
```

### Análise de áudio

Requer o extra `analise`, além de `ffmpeg`/`ffprobe` e `deno` no sistema (o yt-dlp usa o Deno para resolver o desafio JavaScript do YouTube).

```sh
uv run curator modelos baixar        # 24 modelos Essentia (~400 MB, 350 deles do MAEST), verificados por SHA-256
uv run curator analisar --limite 5   # baixa, analisa, grava e apaga o áudio
uv run curator fila status           # pendentes, erros, saídas, estimativa de dias
uv run curator analisar --ate 07:00  # lote até um horário
```

O `sincronizar` já põe as faixas novas na fila. O worker respeita uma cota diária por saída de rede, pausa 20–60 s entre downloads, põe a saída em quarentena quando o YouTube pede "confirme que não é um robô" e tenta outros provedores (busca no YouTube, SoundCloud, Bandcamp, pasta local) quando a faixa não está disponível. Proxies e provedores ficam no `config.toml`:

```toml
[fila]
cota_diaria = 200

[[proxy]]
nome = "vps"
url = "socks5://usuario:senha@host:1080"
intervalo_min_s = 40   # opcional: pausa própria para uma saída mais visada (senão vale a do [fila])
intervalo_max_s = 60

[pasta_local]
caminho = "~/Música"   # depois: uv run curator pasta-local
```

Para rodar todo dia em segundo plano, veja `contrib/systemd/`.

**Guardar os áudios (experimental).** Por padrão o áudio é apagado depois da análise. Enquanto o projeto muda (um modelo novo reabre a fila inteira), dá para guardá-los e reanalisar sem baixar de novo: a faixa com áudio guardado é analisada sem rede, sem cota e sem pausa. Na média são 1,6 MB por faixa.

```toml
[audio]
guardar = true
guardar_dir = ""   # vazio = ~/.local/share/yt-music-curator/audios
```

```sh
uv run curator analisar --guardar-audio   # liga só nesta execução
uv run curator audios status              # quantos e quanto espaço
uv run curator audios limpar              # apaga todos (as análises ficam)
uv run curator audios baixar --aguardar   # só baixa e guarda o que falta, sem analisar
```

O `audios baixar` adianta o download para uma reanálise: baixa a mesma fonte da análise atual de cada faixa, com as saídas, cotas e pausas do worker, e não analisa nada. Ele usa a mesma trava do `analisar`, então o timer espera ele terminar. `--primeiro arquivo.txt` (um video_id por linha) põe algumas faixas na frente.

**Várias saídas em paralelo.** Cada `[[proxy]]` do config vira uma linha de download própria, com a sua cota e o seu ritmo; a análise continua uma faixa por vez. `contrib/saidas/compose.yml` traz duas saídas prontas: uma VPN OpenVPN e um celular via Tailscale (exit node, nos dados móveis), cada uma como SOCKS5 em `127.0.0.1`:

```sh
cd contrib/saidas
docker compose --env-file ~/.config/yt-music-curator/saidas.env up -d --build                  # VPN
docker compose --env-file ~/.config/yt-music-curator/saidas.env --profile celular up -d        # + celular
```

**Análises extras.** Além do núcleo (EffNet e cabeças, MusiCNN, tempo e tom), o analisador guarda três visões de outras redes e outros treinos, cada uma com versão própria (`[analise] extras`): as 50 tags do MSD-MusiCNN, as 521 classes do YAMNet (AudioSet, com "Rapping" e "Singing") e os 519 estilos do MAEST. Faixas novas recebem os extras na mesma passada. Nas já analisadas, eles rodam sobre os áudios guardados, sem rede e sem reabrir a fila: ~16 s por faixa, quase tudo do MAEST. Um extra novo ou trocado refaz só ele: o `curator analisar` completa os que faltam depois da fila, a partir do áudio guardado. Para não calcular extras, `[analise] extras = []` no config.

### Receitas, classificação e planos

Uma **receita** descreve uma playlist em JSON (o que o Claude vai escrever via MCP); o motor escolhe as músicas de forma determinística: mesma receita + mesmo banco = mesma lista.

```json
{
  "nome": "Dormir",
  "tamanho": 40,
  "filtros": {"excluir_artistas": ["HOYO-MiX"], "bpm": [50, 100], "max": {"aggressive": 0.2}},
  "perfil_alvo": {"relaxed": {"alvo": 0.9, "peso": 2}, "arousal": {"alvo": 0.15}, "energy": {"alvo": 0.1}},
  "diversidade": {"lambda": 0.75, "max_por_artista": 3}
}
```

```sh
uv run curator receita criar dormir.json --pedido "músicas para dormir, sem Hoyoverse"
uv run curator receita executar Dormir --plano-nova    # lista + plano criando a playlist
uv run curator classificar --em "Pop" --em "Rap" --plano  # não alocadas, só nas playlists que você nomear
uv run curator classificar --mae "Rock - All"            # ... ou entre as filhas de uma mãe
uv run curator classificar --nas-gerais --plano          # curtidas novas nas playlists gerais (abaixo)
uv run curator avaliar-classificacao --mae "Rock - All" # leave-one-out nas filhas
uv run curator plano mostrar                            # o diff, agrupado por playlist
```

Nomes de artistas, gêneros, features e playlists são conferidos com o banco; um nome errado é recusado com sugestão (`quis dizer: Banda Rock?`). Nada é aplicado no YouTube Music ainda: os planos ficam guardados para o M7.

O gênero-pai (`"genero_pai_min": {"Hip Hop": 0.8}`) usa uma régua relativa, igual para todos os gêneros. 1,0 é tão típico do gênero quanto a faixa mediana do núcleo dele na sua biblioteca, o conjunto de faixas que os dois modelos põem nesse gênero em primeiro lugar. O `examinar_genero` mostra também a mesma régua com o MAEST, que separa melhor rap e Electronic mas joga rock japonês no Pop. Por isso ela fica ao lado e não entra na receita (ADR-15, #93). Os níveis são 0,7 amplo, 0,8 normal e 1,0 estrito. Pelo MCP, `examinar_genero` mostra as faixas logo acima e logo abaixo do corte. Pop e Electronic quase não se separam pelo áudio. A decisão está no ADR-14, e a medição em `docs/avaliacao.md`.

Popularidade e tipo do vídeo (#87) vêm do `sincronizar`:
- **`popularidade`:** o percentil das visualizações de todo o YouTube na sua biblioteca, em escala log, de 0 (lado B) a 1 (as mais vistas). Vale em `perfil_alvo` e em `filtros.min`/`max`, como uma feature.
- **`filtros.tipos_video`:** `["faixa"]` deixa só as faixas de áudio, sem clipes (`clipe`) nem vídeos de usuário (`usuario`). A faixa sem essa informação fica.
- **Nas recomendações:** `--popularidade 0..1` e `--desde AAAA-MM-DD` (só lançamentos). Antes de ordenar, as melhores candidatas recebem as visualizações e a data.

## Desenvolvimento

```sh
uv run pytest
uv run ruff check . && uv run ruff format .
```

### Reorganização e mapa

Requer o extra `organizar` (UMAP, scikit-learn, Plotly): `uv sync --extra analise --extra organizar`.

```sh
uv run curator playlists sugerir --plano   # playlists gerais de TODAS as curtidas, ignorando as que existem
uv run curator clusters renomear 3 "Rock de estrada" --gerais   # dê nomes antes de aplicar o plano
uv run curator playlists sugerir --min-cluster 25 --reagrupar   # mais playlists, menores
uv run curator reorganizar --mae "Rock - All" --plano    # dentro de uma playlist sua: criar, completar, fundir, dividir
uv run curator reorganizar --mae "Rock - All" --recategorizar --plano   # refaz as filhas pelo som
uv run curator clusters mostrar --gerais   # estatísticas de cada cluster
uv run curator visualizar --abrir          # ~/.local/share/yt-music-curator/mapa.html, abre offline
```

As playlists que já existem nunca servem de base: o `playlists sugerir` olha só as curtidas, e o plano dele só cria playlists novas. Depois de aplicado, as criadas ficam marcadas como gerais, e a cada leva de curtidas novas o `classificar --nas-gerais` sugere em qual delas cada uma entra. `classificar` e `reorganizar` só mexem nas playlists que você nomear. O `--recategorizar` cria uma filha nova por cluster e esvazia as filhas antigas (o que sai delas continua na mãe); depois de aplicar o plano, o comando imprime as linhas para o `hierarquia.toml`.

O mapa tem UMAP 2D/3D em três espaços (EffNet, MusiCNN, interpretável), a PCA 2D/3D do espaço interpretável e a vista de valência × excitação, com cores por cluster, playlist, gênero, quadrante, BPM ou "não alocada". Clicar numa faixa mostra as 8 mais parecidas.

Cada eixo tem um rótulo com o que ele acompanha nos dados, como `eixo 1: ↑ energia, ↓ relaxado (ρ 0,86)`. No UMAP, o rótulo vem da correlação de Spearman com as features e os gêneros-pai, e a nuvem é girada e orientada para crescer com a energia, então regerar dá a mesma orientação. Na PCA, cada eixo é uma combinação exata das features, com os pesos e a variância explicada. O painel "Como ler este mapa" tem a tabela completa.

### Claude via MCP

O servidor MCP deixa o Claude montar receitas, classificar e sugerir reorganizações conversando com você. Ele nunca aplica nada: no máximo cria planos.

```sh
claude mcp add yt-music-curator -- uv --directory /caminho/para/yt-music-curator run --extra organizar curator mcp
```

No Claude Desktop, o equivalente em `claude_desktop_config.json`:

```json
{"mcpServers": {"yt-music-curator": {"command": "uv", "args": ["--directory", "/caminho/para/yt-music-curator", "run", "--extra", "organizar", "curator", "mcp"]}}}
```

### Aplicar planos no YouTube Music

```sh
uv run curator plano mostrar 3     # o diff
uv run curator plano aplicar 3     # pede confirmação; recusa se a playlist mudou no YT depois do plano
uv run curator plano desfazer 3    # volta ao estado de antes e apaga as playlists que o plano criou
uv run curator hierarquia corrigir # plano que completa as agregadoras com o que está nas filhas
```

Playlists criadas por plano são privadas. Planos acima de 200 faixas pedem `--grande`.

### Recomendações (músicas que você ainda não curtiu)

```sh
# só músicas NOVAS, sem baixar nada (segundos): rádio do YT + Deezer a partir das sementes
uv run curator recomendar rapido --playlist "Rock - Arena"
uv run curator recomendar rapido -f VIDEO_ID -f OUTRO_ID --criar-playlist   # + plano de playlist nova "Descobertas · …"

# só músicas NOVAS, baixando e analisando as candidatas antes (depois da biblioteca, na mesma fila)
uv run curator recomendar lento --artista "HOYO-MiX" --spec perfil.json --aguardar

# misturar novas e curtidas: só com a flag
uv run curator recomendar rapido --playlist Pop --misturar-curtidas --proporcao-novas 0.3

uv run curator recomendacoes listar        # o que já foi sugerido e o que virou curtida/dislike (após sincronizar)
uv run curator recomendacoes acerto        # taxa de acerto por fonte (yt_radio, yt_related, deezer) e modo
uv run curator recomendacoes curtidas --plano   # playlist "Descobertas curtidas": recomendadas que você curtiu
uv run curator plano dislikes              # plano que tira das suas playlists o que levou dislike
```

Toda recomendação mostra o resumo e pede confirmação. Ela nunca entra numa playlist sua existente sem `--adicionar-em X --permitir-novas`. Uma faixa sugerida 3 vezes sem reação fica 30 dias de fora. Os ajustes ficam em `[recomendacoes]` no `config.toml`. Antes de mostrar, cada música nova tem o like/dislike conferido no YT: o que levou dislike sai. Cada `curator sincronizar` confere de novo as sugeridas ainda sem reação. O que parece com o que você deu dislike (mesmo artista, som parecido) perde pontos.

### Backup e outro computador

```sh
uv run curator backup criar                       # ./yt-music-curator-AAAA-MM-DD.zip (pode rodar com o worker gravando)
uv run curator backup criar --com-credenciais     # + browser.json e saidas.env (cookies e chave do Tailscale)
uv run curator backup verificar arquivo.zip       # checksums e manifesto
uv run curator backup restaurar arquivo.zip       # na máquina nova; com dados existentes, exige --forcar e os guarda renomeados
```

O zip leva o banco (análises, vetores, receitas, planos, clusters), o `config.toml` e o `hierarquia.toml`. Os modelos Essentia ficam de fora: na máquina nova, `uv run curator modelos baixar`.

### Compartilhar análises com outras pessoas

Analisar uma biblioteca leva dias. Quem já analisou pode passar as análises adiante:

```sh
uv run curator analises exportar --autor "Seu nome"   # pacote em ~/.local/share/yt-music-curator/exportacoes/
uv run curator analises importar pacote.zip --seco     # confere e conta, sem gravar
uv run curator analises importar pacote.zip
uv run curator analises importadas                     # o que entrou de quem
uv run curator analises remover 1                      # desfaz uma importação
```

- **O que vai:** os metadados públicos das faixas, as análises, os extras e os metadados externos.
- **O que nunca vai:** curtidas, likes e dislikes, playlists, receitas, recomendações, proxies, o áudio e caminhos do seu disco.
- **Para quem importa:**
  - as faixas importadas continuam **novas**: não viram curtida nem entram na sua biblioteca;
  - a análise local sempre vence;
  - só entra o que foi feito com a mesma versão do analisador (os dois lados precisam de `uv run curator modelos baixar` com o mesmo código).
- **Tamanho:** o pacote da biblioteca toda tem ~86 MB. Não ponha no repositório.

Detalhes no ADR-16.
