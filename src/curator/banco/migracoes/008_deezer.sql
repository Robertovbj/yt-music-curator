-- Casamento faixa do Deezer → video_id do YT Music (#65). Guarda também quando não casou (video_id NULL),
-- para não repetir a busca no YT a cada recomendação.
CREATE TABLE deezer_casamentos (
    deezer_id   INTEGER PRIMARY KEY,
    video_id    TEXT REFERENCES faixas (video_id),
    score       REAL,
    titulo      TEXT,
    artista     TEXT,
    casado_em   TEXT NOT NULL
);
