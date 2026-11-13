-- Marcações (#107): etiquetas que o usuário dá a artistas ("k-pop", "vtuber", "pop-ocidental") e exceções por faixa.
-- São dados, não regra: a IA usa como filtro ou fator nas receitas, e as regras de destino (etiqueta → master) ficam
-- no config.toml de cada pessoa (`[[marcacoes.regras]]`). Não entram no classificador de áudio.
-- A etiqueta de uma faixa = as dos artistas dela, mais as exceções da faixa (sim = 1 acrescenta, sim = 0 tira).
-- Uma exceção da faixa sempre vence o artista. Pessoais: ficam fora do pacote compartilhado (#96).
CREATE TABLE marcacoes_artista (
    artista_id  TEXT NOT NULL REFERENCES artistas (artista_id) ON DELETE CASCADE,
    etiqueta    TEXT NOT NULL,
    origem      TEXT NOT NULL CHECK (origem IN ('cli', 'mcp')),
    criado_em   TEXT NOT NULL,
    PRIMARY KEY (artista_id, etiqueta)
);
CREATE INDEX idx_marcacoes_artista_etiqueta ON marcacoes_artista (etiqueta);
CREATE TABLE marcacoes_faixa (
    video_id    TEXT NOT NULL REFERENCES faixas (video_id) ON DELETE CASCADE,
    etiqueta    TEXT NOT NULL,
    sim         INTEGER NOT NULL CHECK (sim IN (0, 1)),
    origem      TEXT NOT NULL CHECK (origem IN ('cli', 'mcp')),
    criado_em   TEXT NOT NULL,
    PRIMARY KEY (video_id, etiqueta)
);
CREATE INDEX idx_marcacoes_faixa_etiqueta ON marcacoes_faixa (etiqueta);
