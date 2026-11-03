-- Índice da pasta local de música (provedor `pasta_local`, ADR-08.4).
CREATE TABLE arquivos_locais (
    caminho      TEXT PRIMARY KEY,
    mtime        REAL NOT NULL,
    tamanho      INTEGER NOT NULL,
    duracao_s    REAL,
    artista      TEXT,
    titulo       TEXT,
    album        TEXT,
    artista_norm TEXT,                -- normalizar(artista), para busca
    titulo_norm  TEXT,                -- normalizar_titulo(titulo)
    fingerprint  TEXT,                -- Chromaprint (fpcalc), quando disponível
    indexado_em  TEXT NOT NULL
);
CREATE INDEX idx_arquivos_locais_titulo ON arquivos_locais (titulo_norm);
