"""YTMusic falso, com o formato de resposta do ytmusicapi 1.12, para testar a coleta sem rede."""

from collections import Counter


def faixa(
    vid,
    titulo=None,
    artistas=("Banda",),
    album=None,
    dur=200,
    set_id=None,
    like=None,
    explicita=None,
    tipo=None,
):
    extra = {"isExplicit": explicita} if explicita is not None else {}
    if tipo:
        extra["videoType"] = f"MUSIC_VIDEO_TYPE_{tipo}"
    return extra | {
        "likeStatus": like,
        "videoId": vid,
        "title": titulo or f"Música {vid}",
        "artists": [{"name": a, "id": f"UC_{a.lower()}"} for a in artistas],
        "album": {"name": album, "id": f"MPRE_{album}"} if album else None,
        "duration_seconds": dur,
        "setVideoId": set_id or f"set-{vid}",
    }


class FalsoYT:
    def __init__(self):
        self.conta = "Roberto"
        self.curtidas: list[dict] = []
        self.playlists: dict[str, dict] = {}  # id -> {"title", "tracks", "owned"}
        self.albuns: dict[str, dict] = {}
        self.falhar_album: set[str] = set()
        self.musicas: dict[str, dict] = {}  # video_id → resposta do get_song (padrão: visualizações e data)
        self.falhar_musica: set[str] = set()
        self.falhar_escrita: set[str] = set()
        self.radios: dict[str, list[dict]] = {}  # semente → faixas do rádio (sem a própria semente)
        self.relacionadas: dict[str, list[dict]] = {}
        self.buscas: dict[str, list[dict]] = {}  # consulta → resultados de search(filter="songs")
        self.avaliacoes: dict[
            str, str
        ] = {}  # video_id → likeStatus real (botão do player); padrão INDIFFERENT
        self.falhar_next = False  # o "next" do player falha (faixa indisponível, erro de rede)
        self._seq = 0
        self.chamadas = Counter()

    def get_account_info(self):
        self.chamadas["get_account_info"] += 1
        return {"accountName": self.conta}

    def get_liked_songs(self, limit=100):
        self.chamadas["get_liked_songs"] += 1
        return {
            "trackCount": len(self.curtidas),
            "tracks": [{**t, "likeStatus": "LIKE"} for t in self.curtidas],
        }

    def get_library_playlists(self, limit=25):
        self.chamadas["get_library_playlists"] += 1
        itens = [{"playlistId": "LM", "title": "Liked Music", "count": str(len(self.curtidas))}]
        for pid, p in self.playlists.items():
            itens.append({"playlistId": pid, "title": p["title"], "count": f"{len(p['tracks'])} songs"})
        return itens

    def get_playlist(self, playlistId, limit=100):
        self.chamadas["get_playlist"] += 1
        p = self.playlists[playlistId]
        return {
            "id": playlistId,
            "title": p["title"],
            "owned": p.get("owned", True),
            "trackCount": len(p["tracks"]),
            "tracks": list(p["tracks"]),
        }

    # ------------------------------------------------------------ recomendações (ADR-13)

    def get_watch_playlist(self, videoId=None, playlistId=None, limit=25, radio=False, shuffle=False):
        self.chamadas["get_watch_playlist"] += 1
        if videoId not in self.radios:
            raise Exception("HTTP 404: vídeo indisponível")
        semente = {"videoId": videoId, "title": f"Semente {videoId}", "length": "3:00", "artists": []}
        tracks = [semente] + [
            {k: v for k, v in t.items() if k not in ("duration_seconds", "setVideoId")} | {"length": "3:20"}
            for t in self.radios[videoId]
        ]
        return {"tracks": tracks, "related": f"MPTRt_{videoId}" if videoId in self.relacionadas else None}

    def _send_request(self, endpoint, body, *a, **k):
        """Só o "next" cru: o botão de like do player mostra o dislike que o rádio esconde."""
        assert endpoint == "next", endpoint
        self.chamadas["next"] += 1
        if self.falhar_next:
            raise RuntimeError("next indisponível")
        vid = body["videoId"]
        botao = {
            "likeButtonRenderer": {
                "target": {"videoId": vid},
                "likeStatus": self.avaliacoes.get(vid, "INDIFFERENT"),
            }
        }
        return {"playerOverlays": {"playerOverlayRenderer": {"actions": [botao]}}}

    def get_song_related(self, browseId):
        self.chamadas["get_song_related"] += 1
        vid = browseId.removeprefix("MPTRt_")
        return [
            {"title": "Você também pode gostar", "contents": list(self.relacionadas[vid])},
            {"title": "Playlists recomendadas", "contents": [{"playlistId": "RDx", "title": "Mix"}]},
        ]

    def search(self, query, filter=None, limit=20, ignore_spelling=False):
        self.chamadas["search"] += 1
        return list(self.buscas.get(query, []))

    # ------------------------------------------------------------ escrita (M7)

    def create_playlist(
        self, title, description, privacy_status="PRIVATE", video_ids=None, source_playlist=None
    ):
        self.chamadas["create_playlist"] += 1
        pid = f"PLnova{len(self.playlists)}"
        self.playlists[pid] = {"title": title, "tracks": [], "privacy": privacy_status}
        return pid

    def add_playlist_items(self, playlistId, videoIds=None, source_playlist=None, duplicates=False):
        self.chamadas["add_playlist_items"] += 1
        if playlistId in self.falhar_escrita:
            return {"status": "STATUS_FAILED"}
        p = self.playlists[playlistId]
        for v in videoIds:
            if duplicates or v not in [t["videoId"] for t in p["tracks"]]:
                self._seq += 1
                p["tracks"].append(faixa(v, set_id=f"set-{self._seq}"))
        return {"status": "STATUS_SUCCEEDED"}

    def remove_playlist_items(self, playlistId, videos):
        self.chamadas["remove_playlist_items"] += 1
        sets = {v["setVideoId"] for v in videos}
        p = self.playlists[playlistId]
        p["tracks"] = [t for t in p["tracks"] if t["setVideoId"] not in sets]
        return "STATUS_SUCCEEDED"

    def edit_playlist(self, playlistId, title=None, **kw):
        self.chamadas["edit_playlist"] += 1
        if title:
            self.playlists[playlistId]["title"] = title
        return "STATUS_SUCCEEDED"

    def delete_playlist(self, playlistId):
        self.chamadas["delete_playlist"] += 1
        del self.playlists[playlistId]
        return "STATUS_SUCCEEDED"

    def get_song(self, videoId, signatureTimestamp=None):
        self.chamadas["get_song"] += 1
        if videoId in self.falhar_musica:
            raise Exception("HTTP 500")
        return self.musicas.get(videoId) or {
            "videoDetails": {"videoId": videoId, "viewCount": "1234567"},
            "microformat": {"microformatDataRenderer": {"publishDate": "2020-08-06T03:01:47-07:00"}},
        }

    def get_album(self, browseId):
        self.chamadas["get_album"] += 1
        if browseId in self.falhar_album:
            raise Exception("HTTP 404: álbum indisponível")
        return self.albuns.get(browseId, {"title": browseId, "year": "2020", "description": "desc"})


def biblioteca_exemplo(caminho_banco):
    """Banco sincronizado com 6 curtidas e a árvore 'Rock - All' (agregadora) + 2 filhas + 'Dormir'."""
    from curator.banco.conexao import abrir
    from curator.coleta.cliente import ClienteYT
    from curator.coleta.sincronizar import sincronizar_curtidas, sincronizar_playlists

    yt = FalsoYT()
    yt.curtidas = [faixa(v) for v in "abcdef"]
    yt.playlists = {
        "PLall": {"title": "Rock - All", "tracks": [faixa("a"), faixa("b"), faixa("c")]},
        "PLarena": {"title": "Rock - Arena", "tracks": [faixa("a"), faixa("x")]},
        "PLfolk": {"title": "Rock - Folk", "tracks": [faixa("b")]},
        "PLsono": {"title": "Dormir", "tracks": [faixa("d")]},
    }
    con = abrir(caminho_banco)
    cli = ClienteYT(yt, intervalo_s=0, dormir=lambda s: None)
    sincronizar_curtidas(con, cli)
    sincronizar_playlists(con, cli)
    return con


HIERARQUIA_ROCK = """
[[playlist]]
id = "PLall"
tipo = "agregadora"
[[playlist]]
id = "PLarena"
mae = "PLall"
[[playlist]]
id = "PLfolk"
mae = "PLall"
"""
