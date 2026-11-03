-- Schema inicial (ADR-02). Datas em ISO-8601 UTC ("2026-11-03T15:00:00Z").

-- ------------------------------------------------------------------ biblioteca
CREATE TABLE artistas (
    artista_id  TEXT PRIMARY KEY,           -- channelId do YT, ou "nome:<nome normalizado>" quando não há id
    nome        TEXT NOT NULL
);
CREATE INDEX idx_artistas_nome ON artistas (nome);

CREATE TABLE albuns (
    album_id              TEXT PRIMARY KEY,
    titulo                TEXT,
    ano                   INTEGER,
    descricao             TEXT,
    metadados_em          TEXT,             -- última busca bem-sucedida (TTL conta daqui)
    tentativas            INTEGER NOT NULL DEFAULT 0,
    proxima_tentativa_em  TEXT,             -- só preenchido depois de uma falha
    erro                  TEXT
);

CREATE TABLE faixas (
    video_id         TEXT PRIMARY KEY,
    titulo           TEXT NOT NULL,
    artistas         TEXT NOT NULL DEFAULT '[]',  -- JSON [{"id": ..., "nome": ...}], na ordem do YT
    album_id         TEXT REFERENCES albuns (album_id),
    album_nome       TEXT,
    duracao_s        INTEGER,
    isrc             TEXT,
    curtida          INTEGER NOT NULL DEFAULT 0,
    posicao_curtida  INTEGER,                     -- 1 = curtida mais recente
    criada_em        TEXT NOT NULL,
    atualizada_em    TEXT NOT NULL
);
CREATE INDEX idx_faixas_curtida ON faixas (curtida);
CREATE INDEX idx_faixas_album ON faixas (album_id);

CREATE TABLE faixa_artistas (
    video_id    TEXT NOT NULL REFERENCES faixas (video_id) ON DELETE CASCADE,
    artista_id  TEXT NOT NULL REFERENCES artistas (artista_id),
    ordem       INTEGER NOT NULL,
    PRIMARY KEY (video_id, artista_id)
);
CREATE INDEX idx_faixa_artistas_artista ON faixa_artistas (artista_id);

CREATE TABLE playlists (
    playlist_id      TEXT PRIMARY KEY,
    titulo           TEXT NOT NULL,
    propria          INTEGER NOT NULL DEFAULT 1,
    mae_id           TEXT REFERENCES playlists (playlist_id) ON DELETE SET NULL,
    tipo             TEXT NOT NULL DEFAULT 'folha' CHECK (tipo IN ('agregadora', 'folha')),
    total            INTEGER,                    -- contagem informada pelo YT na última listagem
    snapshot_hash    TEXT,                       -- hash dos video_ids em ordem
    sincronizada_em  TEXT
);
CREATE INDEX idx_playlists_mae ON playlists (mae_id);

-- Uma playlist pode repetir a mesma faixa, então a chave é a posição.
CREATE TABLE playlist_faixas (
    playlist_id   TEXT NOT NULL REFERENCES playlists (playlist_id) ON DELETE CASCADE,
    posicao       INTEGER NOT NULL,
    video_id      TEXT NOT NULL REFERENCES faixas (video_id),
    set_video_id  TEXT,                          -- necessário para remover itens (ADR-10)
    PRIMARY KEY (playlist_id, posicao)
);
CREATE INDEX idx_playlist_faixas_video ON playlist_faixas (video_id);

-- ------------------------------------------------------------------ análise de áudio
CREATE TABLE analises (
    video_id           TEXT PRIMARY KEY REFERENCES faixas (video_id) ON DELETE CASCADE,
    status             TEXT NOT NULL CHECK (status IN ('ok', 'erro', 'sem-audio')),
    versao_analisador  TEXT,
    hash_modelos       TEXT,
    fonte_audio        TEXT,                     -- provedor que entregou o áudio
    fonte_ref          TEXT,                     -- id/URL no provedor
    score_casamento    REAL,                     -- 1.0 quando é o próprio video_id
    duracao_analisada  REAL,
    bytes_baixados     INTEGER,
    tempo_s            REAL,
    tempos             TEXT,                     -- JSON {etapa: segundos}
    erro               TEXT,
    analisada_em       TEXT NOT NULL
);

CREATE TABLE features (
    video_id         TEXT PRIMARY KEY REFERENCES faixas (video_id) ON DELETE CASCADE,
    bpm              REAL,
    bpm_rhythm       REAL,
    bpm_tempocnn     REAL,
    beat_confidence  REAL,
    tom              TEXT,
    escala           TEXT,
    key_strength     REAL,
    danceability     REAL,
    acousticness     REAL,
    aggressive       REAL,
    electronic       REAL,
    happy            REAL,
    sad              REAL,
    relaxed          REAL,
    party            REAL,
    instrumental     REAL,
    voz_feminina     REAL,
    engagement       REAL,
    approachability  REAL,
    valence          REAL,                       -- 0..1 (rescalado de 1..9)
    arousal          REAL,
    valence_spread   REAL,
    arousal_spread   REAL,
    energy           REAL                        -- heurística (ADR-09)
);
CREATE INDEX idx_features_bpm ON features (bpm);
CREATE INDEX idx_features_energy ON features (energy);
CREATE INDEX idx_features_relaxed ON features (relaxed);

-- Vetores completos em float32 little-endian (embeddings e probabilidades).
CREATE TABLE vetores (
    video_id  TEXT NOT NULL REFERENCES faixas (video_id) ON DELETE CASCADE,
    tipo      TEXT NOT NULL,
    dim       INTEGER NOT NULL,
    dados     BLOB NOT NULL,
    PRIMARY KEY (video_id, tipo)
);
CREATE INDEX idx_vetores_tipo ON vetores (tipo);

CREATE TABLE rotulos_modelo (
    tipo    TEXT NOT NULL,
    indice  INTEGER NOT NULL,
    rotulo  TEXT NOT NULL,
    PRIMARY KEY (tipo, indice)
);

-- ------------------------------------------------------------------ download
CREATE TABLE fila_download (
    video_id              TEXT PRIMARY KEY REFERENCES faixas (video_id) ON DELETE CASCADE,
    status                TEXT NOT NULL DEFAULT 'pendente'
                          CHECK (status IN ('pendente', 'baixando', 'analisando', 'ok', 'erro', 'sem-audio')),
    tentativas            INTEGER NOT NULL DEFAULT 0,
    proxima_tentativa_em  TEXT,
    provedor              TEXT,
    saida                 TEXT,
    tipo_erro             TEXT,
    ultimo_erro           TEXT,
    provedores_tentados   TEXT NOT NULL DEFAULT '[]',
    criada_em             TEXT NOT NULL,
    atualizada_em         TEXT NOT NULL
);
CREATE INDEX idx_fila_status ON fila_download (status, proxima_tentativa_em);

-- Saídas de rede para o yt-dlp. "direta" sempre existe.
CREATE TABLE proxies (
    nome             TEXT PRIMARY KEY,
    url              TEXT,                       -- NULL = conexão direta
    cota_diaria      INTEGER NOT NULL,
    ativo            INTEGER NOT NULL DEFAULT 1,
    dia_uso          TEXT,                       -- data (UTC) a que uso_hoje se refere
    uso_hoje         INTEGER NOT NULL DEFAULT 0,
    sucessos         INTEGER NOT NULL DEFAULT 0,
    falhas           INTEGER NOT NULL DEFAULT 0,
    falhas_seguidas  INTEGER NOT NULL DEFAULT 0,
    quarentenas      INTEGER NOT NULL DEFAULT 0,
    quarentena_ate   TEXT,
    ultimo_uso_em    TEXT
);

-- ------------------------------------------------------------------ motor (fases seguintes)
CREATE TABLE receitas (
    receita_id   INTEGER PRIMARY KEY,
    nome         TEXT NOT NULL UNIQUE,
    pedido       TEXT,
    spec         TEXT NOT NULL,                  -- JSON
    hash_spec    TEXT NOT NULL,
    hash_pedido  TEXT,
    criada_em    TEXT NOT NULL,
    atualizada_em TEXT NOT NULL
);

CREATE TABLE execucoes (
    execucao_id    INTEGER PRIMARY KEY,
    receita_id     INTEGER NOT NULL REFERENCES receitas (receita_id) ON DELETE CASCADE,
    versao_banco   TEXT NOT NULL,
    resultado      TEXT NOT NULL,                -- JSON [video_id, ...]
    executada_em   TEXT NOT NULL
);

CREATE TABLE planos (
    plano_id    INTEGER PRIMARY KEY,
    origem      TEXT NOT NULL,
    diff        TEXT NOT NULL,                   -- JSON
    status      TEXT NOT NULL DEFAULT 'pendente' CHECK (status IN ('pendente', 'aplicado', 'parcial', 'descartado')),
    log         TEXT,
    criado_em   TEXT NOT NULL,
    aplicado_em TEXT
);

CREATE TABLE projecoes (
    espaco           TEXT NOT NULL,
    dims             INTEGER NOT NULL,
    hash_biblioteca  TEXT NOT NULL,
    video_id         TEXT NOT NULL REFERENCES faixas (video_id) ON DELETE CASCADE,
    coords           BLOB NOT NULL,
    PRIMARY KEY (espaco, dims, video_id)
);
