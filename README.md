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

# 4. visão geral
uv run curator estatisticas
```

A sincronização é total: uma música removida de uma playlist no YouTube Music some do banco. Uma playlist com a mesma contagem, sincronizada há menos de 24 h, não é baixada de novo (`--forcar` ignora esses caches).

### Análise de áudio

Requer o extra `analise`, além de `ffmpeg`/`ffprobe` e `deno` no sistema (o yt-dlp usa o Deno para resolver o desafio JavaScript do YouTube).

```sh
uv run curator modelos baixar        # 22 modelos Essentia (~38 MB), verificados por SHA-256
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

**Várias saídas em paralelo.** Cada `[[proxy]]` do config vira uma linha de download própria, com a sua cota e o seu ritmo; a análise continua uma faixa por vez. `contrib/saidas/compose.yml` traz duas saídas prontas: uma VPN OpenVPN e um celular via Tailscale (exit node, nos dados móveis), cada uma como SOCKS5 em `127.0.0.1`:

```sh
cd contrib/saidas
docker compose --env-file ~/.config/yt-music-curator/saidas.env up -d --build                  # VPN
docker compose --env-file ~/.config/yt-music-curator/saidas.env --profile celular up -d        # + celular
```

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
uv run curator classificar --plano                      # onde colocar as curtidas não alocadas
uv run curator avaliar-classificacao --mae "Rock - All" # leave-one-out nas filhas
uv run curator plano mostrar                            # o diff, agrupado por playlist
```

Nomes de artistas, gêneros, features e playlists são conferidos com o banco; um nome errado é recusado com sugestão (`quis dizer: Banda Rock?`). Nada é aplicado no YouTube Music ainda: os planos ficam guardados para o M7.

## Desenvolvimento

```sh
uv run pytest
uv run ruff check . && uv run ruff format .
```

### Reorganização e mapa

Requer o extra `organizar` (UMAP, scikit-learn, Plotly): `uv sync --extra analise --extra organizar`.

```sh
uv run curator reorganizar                 # clusters sobrepostos → criar, completar, fundir, dividir
uv run curator reorganizar --reagrupar     # recalcula do zero (senão encaixa faixas novas nos clusters salvos)
uv run curator reorganizar --mae "Rock - All" --plano
uv run curator clusters mostrar            # estatísticas de cada cluster
uv run curator clusters renomear 0 "Rock de estrada"
uv run curator visualizar --abrir          # ~/.local/share/yt-music-curator/mapa.html, abre offline
```

O mapa tem UMAP 2D/3D em três espaços (EffNet, MusiCNN, interpretável) e a vista de valência × excitação, com cores por cluster, playlist, gênero, quadrante, BPM ou "não alocada". Clicar numa faixa mostra as 8 mais parecidas.

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

### Backup e outro computador

```sh
uv run curator backup criar                       # ./yt-music-curator-AAAA-MM-DD.zip (pode rodar com o worker gravando)
uv run curator backup criar --com-credenciais     # + browser.json e saidas.env (cookies e chave do Tailscale)
uv run curator backup verificar arquivo.zip       # checksums e manifesto
uv run curator backup restaurar arquivo.zip       # na máquina nova; com dados existentes, exige --forcar e os guarda renomeados
```

O zip leva o banco (análises, vetores, receitas, planos, clusters), o `config.toml` e o `hierarquia.toml`. Os modelos Essentia ficam de fora: na máquina nova, `uv run curator modelos baixar`.
