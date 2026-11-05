-- Versão do motor em cada execução de receita (#83). Mesma receita + mesmo banco dão a mesma lista (ADR-05)
-- enquanto a versão do motor não muda; a v2 trocou o gênero-pai pela afinidade com régua relativa (ADR-14).
-- Execuções de antes desta migração ficam com NULL (v1).
ALTER TABLE execucoes ADD COLUMN versao_motor TEXT;
