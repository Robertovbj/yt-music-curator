-- Faixas ignoradas uma a uma (#100): curtidas que não são música para o curator (edição de meme, cover de piada).
-- Entram na lista negra (`consultas.IGNORADAS`) junto com os vídeos de playlists ignoradas (#77), mas valem mesmo que
-- a faixa esteja numa playlist válida. Nada muda na conta do YouTube: a faixa continua curtida lá, e `sincronizar` não
-- mexe nesta tabela. A análise já feita fica guardada (ADR-02). Pessoal: fica fora do pacote compartilhado (#96).
CREATE TABLE faixas_ignoradas (
    video_id   TEXT PRIMARY KEY REFERENCES faixas (video_id) ON DELETE CASCADE,
    motivo     TEXT,
    criado_em  TEXT NOT NULL
);
