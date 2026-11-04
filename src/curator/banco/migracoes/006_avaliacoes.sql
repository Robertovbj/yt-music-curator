-- Avaliação da faixa no YouTube Music (#60). O dislike nunca apaga nada (#59): só tira a faixa de
-- playlists, classificações e recomendações (#61). `like` acompanha as curtidas; `neutra` é o padrão.
ALTER TABLE faixas ADD COLUMN avaliacao TEXT NOT NULL DEFAULT 'neutra'
    CHECK (avaliacao IN ('like', 'dislike', 'neutra'));
ALTER TABLE faixas ADD COLUMN avaliacao_em TEXT;   -- quando a avaliação mudou pela última vez
CREATE INDEX idx_faixas_avaliacao ON faixas (avaliacao);
UPDATE faixas SET avaliacao = 'like' WHERE curtida = 1;
