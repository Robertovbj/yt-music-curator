-- Rótulos de gênero-pai dados pelo usuário (#93, motor/aprendido.py): "esta faixa é / não é Rock". São a base do
-- gênero-pai aprendido. Começam pelo gabarito pessoal e crescem pela conversa (MCP) ou pela CLI, nas faixas mais
-- duvidosas. NUNCA vêm das playlists existentes (elas seguem outra lógica; ver a memória do projeto e o ADR-17).
-- Pessoais: ficam fora do pacote de análises compartilhado (#96).
CREATE TABLE rotulos_genero (
    video_id   TEXT NOT NULL REFERENCES faixas (video_id) ON DELETE CASCADE,
    pai        TEXT NOT NULL,                       -- gênero-pai do Discogs (Rock, Pop, Hip Hop…)
    sim        INTEGER NOT NULL CHECK (sim IN (0, 1)),
    -- gabarito_artista: todas as faixas de um artista do gabarito (o rótulo vale para a faixa, mas veio do artista);
    -- gabarito_faixa, cli, mcp: a faixa em si. Um rótulo da faixa sempre vence um do artista.
    origem     TEXT NOT NULL CHECK (origem IN ('gabarito_artista', 'gabarito_faixa', 'cli', 'mcp')),
    criado_em  TEXT NOT NULL,
    PRIMARY KEY (video_id, pai)
);
CREATE INDEX idx_rotulos_genero_pai ON rotulos_genero (pai, sim);
