-- O que cada eixo das projeções do mapa acompanha (#57): correlações de Spearman com as features, guardadas
-- com a projeção (mesmo hash_biblioteca). `versao` muda quando a orientação ou o cálculo mudam: aí a projeção
-- em cache é refeita.
CREATE TABLE projecao_eixos (
    espaco           TEXT NOT NULL,
    dims             INTEGER NOT NULL,
    hash_biblioteca  TEXT NOT NULL,
    versao           INTEGER NOT NULL,
    eixos            TEXT NOT NULL,  -- JSON: [{rotulo, correlacoes: [[nome, ρ], ...]}, ...]
    PRIMARY KEY (espaco, dims)
);
