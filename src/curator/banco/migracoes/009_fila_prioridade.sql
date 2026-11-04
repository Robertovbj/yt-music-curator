-- Modo lento das recomendações (#67): as candidatas entram na mesma fila, com prioridade menor que a
-- biblioteca (0 = biblioteca, 1 = candidata) e sem cota própria (#62).
ALTER TABLE fila_download ADD COLUMN prioridade INTEGER NOT NULL DEFAULT 0;
-- As candidatas escolhidas para análise, para a recomendação poder ser concluída e refeita depois.
ALTER TABLE recomendacoes ADD COLUMN analisar TEXT;   -- JSON [video_id]
