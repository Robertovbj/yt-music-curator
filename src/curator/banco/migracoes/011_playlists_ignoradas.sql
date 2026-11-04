-- Playlists que não são de música (vídeos do YouTube comum numa conta conjunta, aulas...) (#77). Os vídeos
-- delas viram lista negra em todo o projeto; as análises já feitas ficam guardadas (#59).
ALTER TABLE playlists ADD COLUMN ignorada INTEGER NOT NULL DEFAULT 0;
