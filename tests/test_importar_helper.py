import sqlite3

from curator.banco.conexao import abrir, agora
from curator.banco.importar_helper import importar


def test_importa_com_ids_truncados(tmp_path):
    helper = tmp_path / "cache_yt_music.db"
    h = sqlite3.connect(helper)
    h.executescript(
        """CREATE TABLE playlists (playlist_id TEXT PRIMARY KEY, titulo TEXT, mae_id TEXT);
           INSERT INTO playlists VALUES ('PLmae0000000000000000000000000000x', 'Rock - All', NULL);
           INSERT INTO playlists VALUES ('PLD8zSiH4Map8', 'Rock - Arena Rock', 'PLmae0000000000000000000000000000x');
           INSERT INTO playlists VALUES ('PLzzzzzzzzzzz', 'Rock - Épico & Folk', 'PLmae0000000000000000000000000000x');
           INSERT INTO playlists VALUES ('PLsumiu000000', 'Rock - Apagada', 'PLmae0000000000000000000000000000x');"""
    )
    h.commit()
    h.close()

    con = abrir(tmp_path / "c.db")
    for pid, titulo in [
        ("PLmae0000000000000000000000000000x", "Rock - All"),
        ("PLD8zSiH4Map8restoDoId000000000000", "Rock - Arena Rock"),
        ("PLoutroIdCompleto0000000000000000", "Rock - Epico & Folk"),
    ]:
        con.execute(
            "INSERT INTO playlists (playlist_id, titulo, propria, sincronizada_em) VALUES (?, ?, 1, ?)",
            (pid, titulo, agora()),
        )

    imp = importar(con, helper)
    como = {antigo: c for antigo, _, c in imp.casadas}
    assert como == {
        "PLmae0000000000000000000000000000x": "id exato",
        "PLD8zSiH4Map8": "prefixo do id",
        "PLzzzzzzzzzzz": "título",
    }
    assert imp.sem_par == [("PLsumiu000000", "Rock - Apagada")]
    assert 'id = "PLD8zSiH4Map8restoDoId000000000000"\ntitulo = "Rock - Arena Rock"\nmae = "PLmae' in imp.toml
    assert imp.toml.count("[[playlist]]") == 3
