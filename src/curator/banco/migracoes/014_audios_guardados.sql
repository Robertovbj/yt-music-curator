-- Áudios guardados (#84, experimental): com `[audio] guardar = true` (ou `analisar --guardar-audio`), o worker
-- não apaga o áudio depois da análise. Quando a fila reabre a faixa (modelo novo, analisador novo), o provedor
-- `guardado` usa o arquivo em vez de baixar de novo. Provedor, referência e score são os do download original.
CREATE TABLE audios_guardados (
    video_id     TEXT PRIMARY KEY REFERENCES faixas (video_id) ON DELETE CASCADE,
    caminho      TEXT NOT NULL,
    provedor     TEXT NOT NULL,
    ref          TEXT NOT NULL,
    score        REAL NOT NULL,
    exato        INTEGER NOT NULL,
    parcial      INTEGER NOT NULL,
    bytes        INTEGER NOT NULL,
    guardado_em  TEXT NOT NULL
);
