-- Recomendações fora da biblioteca (#63, ADR-13). As candidatas ficam em `faixas` (para reaproveitar
-- metadados e análise), mas fora da biblioteca: não são curtidas nem estão em playlists próprias comuns.

-- O que cada fonte devolveu para cada semente, como veio (snapshot): o ranking pode ser refeito sobre a
-- mesma coleta, e o rádio, que muda com o tempo, é coletado de novo quando o cache vence.
CREATE TABLE coletas (
    coleta_id    INTEGER PRIMARY KEY,
    fonte        TEXT NOT NULL CHECK (fonte IN ('yt_radio', 'yt_related', 'deezer')),
    semente      TEXT NOT NULL,                  -- video_id da faixa-semente
    itens        TEXT NOT NULL,                  -- JSON [video_id, ...] na ordem da fonte
    coletada_em  TEXT NOT NULL
);
CREATE INDEX idx_coletas_semente ON coletas (fonte, semente, coletada_em);

CREATE TABLE recomendacoes (
    recomendacao_id  INTEGER PRIMARY KEY,
    pedido           TEXT,                       -- a frase original, quando houver
    modo             TEXT NOT NULL CHECK (modo IN ('rapido', 'lento')),
    escopo           TEXT NOT NULL CHECK (escopo IN ('novas', 'misturar')),
    status           TEXT NOT NULL DEFAULT 'pronta'
                     CHECK (status IN ('pronta', 'aguardando_analise', 'descartada')),
    sementes         TEXT NOT NULL,              -- JSON [video_id] usados de fato
    spec             TEXT NOT NULL,              -- JSON do pedido (como as sementes foram dadas, tamanho, filtros…)
    coletas          TEXT NOT NULL,              -- JSON [coleta_id]
    hash             TEXT NOT NULL,              -- hash da spec
    criada_em        TEXT NOT NULL,
    concluida_em     TEXT
);

-- Sem ON DELETE CASCADE em video_id: uma faixa recomendada nunca some do banco (#59).
CREATE TABLE recomendacao_itens (
    recomendacao_id  INTEGER NOT NULL REFERENCES recomendacoes (recomendacao_id) ON DELETE CASCADE,
    posicao          INTEGER NOT NULL,
    video_id         TEXT NOT NULL REFERENCES faixas (video_id),
    score            REAL NOT NULL,
    fontes           TEXT NOT NULL,              -- JSON ["yt_radio", ...]
    votos            INTEGER NOT NULL,           -- quantas sementes trouxeram a faixa
    vezes_antes      INTEGER NOT NULL DEFAULT 0, -- recomendações anteriores sem reação
    conhecida        INTEGER NOT NULL DEFAULT 0, -- 1 = já estava na biblioteca (escopo "misturar")
    PRIMARY KEY (recomendacao_id, posicao)
);
CREATE INDEX idx_recomendacao_itens_video ON recomendacao_itens (video_id);

-- Playlists criadas a partir de recomendações ("Descobertas · …"). As faixas que só estão nelas não contam
-- como biblioteca: ouvir é diferente de curtir, e receitas das curtidas não devem misturá-las (ADR-13).
ALTER TABLE playlists ADD COLUMN descobertas INTEGER NOT NULL DEFAULT 0;
