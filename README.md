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

## Desenvolvimento

```sh
uv run pytest
uv run ruff check . && uv run ruff format .
```
