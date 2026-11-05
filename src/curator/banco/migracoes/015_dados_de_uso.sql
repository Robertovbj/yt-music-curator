-- Dados de uso e popularidade (#85). A API do YouTube Music não tem contagem de reproduções por usuário, e o
-- histórico (`get_history`) depende de o histórico do YouTube estar ligado na conta. Fica aqui o que a API dá:
-- `explicita` e `tipo_video` vêm nas curtidas e playlists, sem chamada extra; `visualizacoes` (de todo o
-- YouTube, não suas) e `publicada_em` vêm do `get_song`, uma chamada por faixa, com validade e backoff.
ALTER TABLE faixas ADD COLUMN explicita INTEGER;                          -- 1/0; NULL = a resposta não disse
ALTER TABLE faixas ADD COLUMN tipo_video TEXT;                            -- ATV (faixa de áudio), OMV (clipe), UGC…
ALTER TABLE faixas ADD COLUMN visualizacoes INTEGER;
ALTER TABLE faixas ADD COLUMN publicada_em TEXT;                          -- AAAA-MM-DD
ALTER TABLE faixas ADD COLUMN estatisticas_em TEXT;                       -- última busca que respondeu
ALTER TABLE faixas ADD COLUMN estatisticas_tentativas INTEGER NOT NULL DEFAULT 0;
ALTER TABLE faixas ADD COLUMN estatisticas_proxima TEXT;                  -- depois de uma falha
