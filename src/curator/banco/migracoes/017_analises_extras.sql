-- Análises extras (#88): modelos fora do núcleo do analisador (tags do MSD-MusiCNN, YAMNet, MAEST), cada um com
-- a própria versão. Os vetores ficam em `vetores`, com tipos próprios (audio/extras.py).
CREATE TABLE analises_extras (
    video_id     TEXT NOT NULL REFERENCES faixas (video_id) ON DELETE CASCADE,
    extra        TEXT NOT NULL,                       -- msd_tags | yamnet | maest
    hash         TEXT NOT NULL,                       -- versão do cálculo + SHA-256 dos modelos
    status       TEXT NOT NULL CHECK (status IN ('ok', 'erro')),
    fonte_ref    TEXT,                                -- o áudio usado (o mesmo da análise do núcleo)
    tempo_s      REAL,
    erro         TEXT,
    analisada_em TEXT NOT NULL,
    PRIMARY KEY (video_id, extra)
);
