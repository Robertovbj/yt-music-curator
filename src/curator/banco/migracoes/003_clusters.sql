-- Agrupamentos (ADR-07): cada execução de UMAP + HDBSCAN fica guardada, com os parâmetros, para que
-- as faixas novas sejam atribuídas aos clusters existentes (âncoras) em vez de reagrupar tudo.
CREATE TABLE agrupamentos (
    agrupamento_id   INTEGER PRIMARY KEY,
    escopo           TEXT NOT NULL,            -- 'biblioteca' ou playlist_id da mãe
    parametros       TEXT NOT NULL,            -- JSON
    hash_biblioteca  TEXT NOT NULL,
    criado_em        TEXT NOT NULL
);

CREATE TABLE clusters (
    agrupamento_id  INTEGER NOT NULL REFERENCES agrupamentos (agrupamento_id) ON DELETE CASCADE,
    cluster_id      INTEGER NOT NULL,
    nome            TEXT NOT NULL,
    nome_origem     TEXT NOT NULL DEFAULT 'automatico',  -- 'automatico' (estatísticas) | 'ia' | 'usuario'
    hash_nucleo     TEXT NOT NULL,             -- identifica o cluster pelo núcleo; o nome fica em cache por ele
    centroide       BLOB NOT NULL,
    limiar          REAL NOT NULL,
    estatisticas    TEXT NOT NULL,             -- JSON
    PRIMARY KEY (agrupamento_id, cluster_id)
);
CREATE INDEX idx_clusters_hash ON clusters (hash_nucleo);

CREATE TABLE cluster_membros (
    agrupamento_id  INTEGER NOT NULL,
    cluster_id      INTEGER NOT NULL,
    video_id        TEXT NOT NULL REFERENCES faixas (video_id) ON DELETE CASCADE,
    nucleo          INTEGER NOT NULL DEFAULT 0,
    similaridade    REAL NOT NULL,
    PRIMARY KEY (agrupamento_id, cluster_id, video_id),
    FOREIGN KEY (agrupamento_id, cluster_id) REFERENCES clusters (agrupamento_id, cluster_id) ON DELETE CASCADE
);
CREATE INDEX idx_cluster_membros_video ON cluster_membros (video_id);
