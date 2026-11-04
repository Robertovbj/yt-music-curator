-- Quando a avaliação da faixa foi lida de uma fonte confiável (#60): playlists, curtidas ou o botão de
-- like do player (endpoint "next"). As recomendadas são conferidas de novo depois de alguns dias.
ALTER TABLE faixas ADD COLUMN avaliacao_conferida_em TEXT;
