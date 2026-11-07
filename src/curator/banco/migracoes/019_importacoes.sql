-- Análises importadas de outra pessoa (#96, banco/compartilhar.py). Cada importação vira uma linha aqui, e o que
-- veio dela leva o `importacao_id`. NULL = feito aqui. Uma análise refeita aqui (INSERT OR REPLACE do worker)
-- volta a NULL sozinha. A importação nunca escreve curtida, avaliação nem playlist: uma faixa importada continua
-- NOVA para quem importa (conhecida = curtida ∪ playlist própria).
CREATE TABLE importacoes (
    importacao_id  INTEGER PRIMARY KEY,
    autor          TEXT,                    -- o nome que veio no manifesto (opcional)
    arquivo        TEXT NOT NULL,           -- nome do arquivo importado
    sha256         TEXT NOT NULL UNIQUE,    -- do analises.db do pacote: o mesmo pacote não entra duas vezes
    exportado_em   TEXT,
    importado_em   TEXT NOT NULL,
    contagens      TEXT NOT NULL            -- JSON: o que entrou e o que ficou de fora (e por quê)
);

ALTER TABLE faixas ADD COLUMN importacao_id INTEGER REFERENCES importacoes (importacao_id);  -- criada pela importação
ALTER TABLE analises ADD COLUMN importacao_id INTEGER REFERENCES importacoes (importacao_id);
ALTER TABLE analises_extras ADD COLUMN importacao_id INTEGER REFERENCES importacoes (importacao_id);
ALTER TABLE externos_faixas ADD COLUMN importacao_id INTEGER REFERENCES importacoes (importacao_id);
