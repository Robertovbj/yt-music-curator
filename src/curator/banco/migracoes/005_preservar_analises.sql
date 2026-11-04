-- Uma faixa analisada nunca sai do banco (#59): sair das curtidas e de todas as playlists só muda o
-- pertencimento. Sem esta trava, o ON DELETE CASCADE de analises/features/vetores apagaria em silêncio
-- o download e a análise. A trava fica em `faixas` (e não trocando o CASCADE por RESTRICT) para não
-- reescrever as tabelas de vetores, que são as maiores do banco.
CREATE TRIGGER faixas_preservar_analise BEFORE DELETE ON faixas
WHEN EXISTS (SELECT 1 FROM analises WHERE video_id = OLD.video_id)
  OR EXISTS (SELECT 1 FROM features WHERE video_id = OLD.video_id)
  OR EXISTS (SELECT 1 FROM vetores WHERE video_id = OLD.video_id)
BEGIN
    SELECT RAISE(ABORT, 'faixa analisada não pode ser apagada: a análise fica guardada (#59)');
END;
