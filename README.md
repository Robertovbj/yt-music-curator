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

[pasta_local]
caminho = "~/Música"   # depois: uv run curator pasta-local
```

Para rodar todo dia em segundo plano, veja `contrib/systemd/`.

## Desenvolvimento

```sh
uv run pytest
uv run ruff check . && uv run ruff format .
```
