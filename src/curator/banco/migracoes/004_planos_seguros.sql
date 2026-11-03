-- Salvaguardas da escrita no YT (#44): retrato das playlists na criação do plano, backup antes de aplicar,
-- ids das playlists criadas e o status 'desfeito'. O SQLite não altera CHECK: a tabela é recriada.
CREATE TABLE planos_novo (
    plano_id    INTEGER PRIMARY KEY,
    origem      TEXT NOT NULL,
    diff        TEXT NOT NULL,                   -- JSON [Operacao]
    status      TEXT NOT NULL DEFAULT 'pendente'
                CHECK (status IN ('pendente', 'aplicado', 'parcial', 'descartado', 'desfeito')),
    log         TEXT,                            -- JSON [{op, tipo, playlist, ok, detalhe}]
    snapshots   TEXT,                            -- JSON {playlist_id: snapshot_hash} quando o plano foi criado
    backup      TEXT,                            -- JSON {playlist_id: {"titulo": ..., "itens": [[video_id, set_video_id]]}}
    criadas     TEXT,                            -- JSON {"nova:<nome>": playlist_id}
    criado_em   TEXT NOT NULL,
    aplicado_em TEXT,
    desfeito_em TEXT
);
INSERT INTO planos_novo (plano_id, origem, diff, status, log, criado_em, aplicado_em)
    SELECT plano_id, origem, diff, status, log, criado_em, aplicado_em FROM planos;
DROP TABLE planos;
ALTER TABLE planos_novo RENAME TO planos;
