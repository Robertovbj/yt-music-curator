-- Playlists criadas a partir de `curator playlists sugerir` (#80): as gerais, montadas só pela matemática a partir
-- das curtidas. `curator classificar --nas-gerais` usa estas como destino das curtidas novas.
ALTER TABLE playlists ADD COLUMN geral INTEGER NOT NULL DEFAULT 0;
