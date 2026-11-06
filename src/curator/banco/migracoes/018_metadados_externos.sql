-- Metadados externos (#88): Deezer e MusicBrainz casados com as faixas do YT Music (coleta/externos.py).
CREATE TABLE externos_faixas (
    video_id   TEXT NOT NULL REFERENCES faixas (video_id) ON DELETE CASCADE,
    fonte      TEXT NOT NULL,                       -- deezer | musicbrainz
    ref        TEXT,                                -- id na fonte; NULL sem casamento ou com erro
    score      REAL,                                -- casamento (título, artista, duração); 1,0 pelo ISRC
    dados      TEXT,                                -- JSON: o que a fonte devolveu de útil
    status     TEXT NOT NULL CHECK (status IN ('ok', 'sem-casamento', 'erro')),
    erro       TEXT,
    tentativas INTEGER NOT NULL DEFAULT 0,          -- erros seguidos (o erro nunca vira cache)
    proxima_em TEXT,                                -- erro: nova tentativa a partir daqui
    buscado_em TEXT NOT NULL,
    PRIMARY KEY (video_id, fonte)
);

-- Artistas, álbuns e release groups, que várias faixas dividem: um pedido só por entidade.
CREATE TABLE externos_entidades (
    fonte      TEXT NOT NULL,
    tipo       TEXT NOT NULL,                       -- album | artista | release-group
    ref        TEXT NOT NULL,
    nome       TEXT,
    dados      TEXT,
    buscado_em TEXT NOT NULL,
    PRIMARY KEY (fonte, tipo, ref)
);

-- Gêneros e tags por faixa, com peso (votos no MusicBrainz, 1 no Deezer) e nível.
CREATE TABLE externos_generos (
    video_id TEXT NOT NULL REFERENCES faixas (video_id) ON DELETE CASCADE,
    fonte    TEXT NOT NULL,
    nivel    TEXT NOT NULL CHECK (nivel IN ('faixa', 'album', 'artista')),
    rotulo   TEXT NOT NULL,                         -- minúsculas
    peso     REAL NOT NULL,
    PRIMARY KEY (video_id, fonte, nivel, rotulo)
);
CREATE INDEX ix_externos_generos_rotulo ON externos_generos (rotulo);
