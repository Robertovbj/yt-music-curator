"""Afinidade de gênero-pai (#83, ADR-14): um sinal e uma régua que valem igual para todos os gêneros.

Antes, o gênero-pai era o MAIOR estilo do Discogs400 ("Hip Hop" = máximo dos 26 estilos Hip Hop---*). O modelo é
multirrótulo (saída sigmoide) e espalha a probabilidade entre estilos irmãos: "Without Me" (Eminem) tinha 0,18 no
maior estilo e 1,31 somando os de Hip Hop. Com isso, o mesmo limite valia coisas diferentes em cada gênero.

Agora, por faixa e por pai:
1. **fração do Discogs400**: soma dos estilos do pai ÷ soma dos 400 estilos, ou seja, quanto da "atenção" do modelo
   cai naquele pai. Ao contrário do noisy-OR, não cresce com o número de estilos do pai (Electronic tem 106, Hip
   Hop 26).
2. **fração do MTG-Jamendo**: a mesma conta na outra cabeça, pelos rótulos equivalentes (`MAPA_JAMENDO`; décadas
   fora). As duas cabeças usam o mesmo embedding EffNet, então concordam muito; a média ainda ganha um pouco.
3. **afinidade** = média das frações disponíveis (só o Discogs, se o pai não tem equivalente no Jamendo).
   Uma régua com o MAEST (#88, ADR-15) é calculada ao lado (`relativa_com_maest`) e mostrada à IA na fronteira,
   mas não é a padrão: ela joga o rock japonês no Pop.
4. **régua relativa**: afinidade ÷ mediana da afinidade no NÚCLEO do pai, que são as faixas da biblioteca que
   todas as cabeças põem nesse pai em 1º lugar. 1,0 = tão típica quanto a faixa mediana do núcleo. O mesmo
   limite (0,8) separou Hip Hop e Rock na avaliação (docs/avaliacao.md). Núcleo com menos de `MIN_NUCLEO` faixas:
   núcleo amplo (1º em qualquer cabeça); sem isso, a referência global (mediana das medianas dos núcleos).

Testados e deixados de fora: suavizar pelos vizinhos no embedding (ganho dentro do ruído, e parte dele vem de
vizinhos do mesmo artista) e puxar para a média do artista (o gabarito é por artista, e compositores de trilha
sonora tocam vários gêneros).

Pop e Electronic não se separam bem só pelo áudio: esses modelos chamam a produção pop moderna de Electronic.

**A régua aprendida com os seus rótulos (#93, #101, ADR-17; `relativa_aprendida`)** aparece ao lado, no
`examinar_genero`. Ela só julga a faixa que passa pelo portão e pela guarda (parecida com faixas rotuladas); nas
outras vale a v2. Ela entra nas receitas (`relativa_receita`) gênero a gênero, nos pais de `REGUA_APRENDIDA`, que é
uma decisão do usuário. Mudar esse conjunto pede um `VERSAO_MOTOR` novo.
"""

import re
import sqlite3
from dataclasses import dataclass

import numpy as np

from curator.motor import espacos

# os gêneros-pai em que o `genero_pai_min` das receitas usa a régua aprendida (#93, #101, ADR-17), decididos pelo
# usuário um a um. Os outros, e os sem modelo, ficam na v2 (ADR-14). Mudar o conjunto pede um `VERSAO_MOTOR` novo.
REGUA_APRENDIDA: frozenset[str] = frozenset({"Hip Hop", "Pop"})  # decisão de 09/11 (#101), VERSAO_MOTOR "3"

# gênero-pai do Discogs400 → rótulos equivalentes do MTG-Jamendo (só os inequívocos)
MAPA_JAMENDO = {
    "Blues": ["blues", "bluesrock"],
    "Classical": ["classical", "orchestral", "symphonic", "choir"],
    "Electronic": ["electronic", "electronica", "edm", "techno", "house", "deephouse", "trance", "drumnbass",
                   "dubstep", "breakbeat", "idm", "minimal", "club", "eurodance", "electropop", "synthpop"],
    "Folk, World, & Country": ["folk", "country", "world", "ethno", "celtic", "worldfusion"],
    "Funk / Soul": ["funk", "soul", "rnb", "disco"],
    "Hip Hop": ["hiphop", "rap"],
    "Jazz": ["jazz", "acidjazz", "jazzfusion", "swing"],
    "Latin": ["latin", "bossanova"],
    "Pop": ["pop", "poprock", "electropop", "synthpop", "popfolk", "dance"],
    "Reggae": ["reggae", "dub"],
    "Rock": ["rock", "metal", "hardrock", "punkrock", "alternativerock", "classicrock", "grunge", "poprock",
             "progressive", "psychedelic", "rocknroll", "postrock"],
    "Stage & Screen": ["soundtrack"],
}  # fmt: skip
DECADAS = {"60s", "70s", "80s", "90s"}  # rótulos de época do Jamendo: não são gênero

# Visões de outras redes e outros treinos (#88). O MAEST usa a taxonomia do Discogs ("Pai---Estilo") e não precisa
# de mapa. "Rapping"/"Singing" do YAMNet são técnica vocal, não gênero: ficam fora deste mapa.
MAPA_YAMNET = {
    "Blues": ["Blues"],
    "Classical": ["Classical music", "Opera"],
    "Electronic": ["Electronic music", "House music", "Techno", "Dubstep", "Drum and bass", "Electronica",
                   "Electronic dance music", "Ambient music", "Trance music"],
    "Folk, World, & Country": ["Country", "Bluegrass", "Folk music", "Middle Eastern music", "Music of Africa",
                               "Afrobeat", "Music of Asia", "Carnatic music", "Music of Bollywood", "Traditional music"],
    "Funk / Soul": ["Rhythm and blues", "Soul music", "Funk", "Disco", "Gospel music"],
    "Hip Hop": ["Hip hop music"],
    "Jazz": ["Jazz", "Swing music"],
    "Latin": ["Music of Latin America", "Salsa music", "Flamenco"],
    "Pop": ["Pop music"],
    "Reggae": ["Reggae", "Ska"],
    "Rock": ["Rock music", "Heavy metal", "Punk rock", "Grunge", "Progressive rock", "Rock and roll", "Psychedelic rock"],
    "Stage & Screen": ["Soundtrack music", "Theme music", "Video game music"],
}  # fmt: skip
# tags do MSD-MusiCNN (Last.fm) que são gênero; as outras (décadas, humor, "female vocalists") ficam de fora
MAPA_MSD = {
    "Blues": ["blues"],
    "Electronic": ["electronic", "electronica", "electro", "House", "ambient", "dance", "chillout"],
    "Folk, World, & Country": ["folk", "country"],
    "Funk / Soul": ["soul", "funk", "rnb"],
    "Hip Hop": ["Hip-Hop"],
    "Jazz": ["jazz"],
    "Pop": ["pop", "indie pop"],
    "Rock": ["rock", "alternative", "indie", "alternative rock", "metal", "classic rock", "indie rock", "punk",
             "hard rock", "heavy metal", "Progressive rock"],
}  # fmt: skip

# Rótulos livres dos metadados externos (gêneros do MusicBrainz e do Deezer) → pais do Discogs.
# Primeiro os compostos que a palavra solta classificaria errado; depois palavras inteiras (um rótulo pode ter mais
# de um pai, como "blues rock"). HEURÍSTICA conferida na avaliação da #88.
_COMPOSTOS = {
    "pop rap": ["Hip Hop"], "emo rap": ["Hip Hop"], "rap rock": ["Hip Hop", "Rock"], "rap metal": ["Hip Hop", "Rock"],
    "trip hop": ["Electronic"], "rhythm and blues": ["Funk / Soul"], "r and b": ["Funk / Soul"],
    "contemporary r and b": ["Funk / Soul"], "drum and bass": ["Electronic"], "synth pop": ["Electronic", "Pop"],
    "synthpop": ["Electronic", "Pop"], "electropop": ["Electronic", "Pop"], "dance pop": ["Pop", "Electronic"],
    "pop punk": ["Rock"], "pop rock": ["Rock", "Pop"], "nu metal": ["Rock"], "funk metal": ["Rock"],
    "funk carioca": ["Latin"], "brazilian funk": ["Latin"], "baile funk": ["Latin"], "films games": ["Stage & Screen"],
    "soul and funk": ["Funk / Soul"], "video game music": ["Stage & Screen"], "latin music": ["Latin"],
    "brazilian music": ["Latin"], "bossa nova": ["Latin", "Jazz"],
}  # fmt: skip
_PALAVRAS = {
    "Hip Hop": r"hip hop|hiphop|rap|trap|drill|grime|boom bap|phonk|horrorcore|crunk|g funk|gangsta",
    "Rock": r"rock|metal|punk|grunge|hardcore|emo|screamo|shoegaze|metalcore|deathcore|djent|alternative|post hardcore",
    "Electronic": r"electronic|electronica|edm|house|techno|trance|dubstep|electro|synthwave|idm|ambient|breakbeat|"
    r"future bass|eurodance|hardstyle|chiptune|vaporwave|downtempo|dance|dubstep|jungle",
    "Pop": r"pop|j pop|k pop|c pop|teen pop|indie pop|power pop",
    "Funk / Soul": r"soul|funk|disco|motown|r b|rnb|new jack swing|gospel",
    "Jazz": r"jazz|swing|bebop|big band",
    "Latin": r"latin|reggaeton|salsa|bachata|cumbia|merengue|tango|mpb|samba|sertanejo|forro|axe|pagode",
    "Reggae": r"reggae|dancehall|dub|ska|rocksteady",
    "Folk, World, & Country": r"folk|country|bluegrass|celtic|americana|world",
    "Blues": r"blues",
    "Classical": r"classical|orchestral|baroque|opera|choral|symphonic|chamber music|contemporary classical",
    "Stage & Screen": r"soundtrack|score|video game|anime|musical|show tunes|film|theatre",
}


def pais_de_rotulo(rotulo: str) -> list[str]:
    """Pais do Discogs para um rótulo externo ("hip-hop" → Hip Hop, "blues rock" → Blues e Rock; "seen live" → [])."""
    limpo = " ".join(re.sub(r"[^\w]+", " ", rotulo.casefold().replace("&", " and ")).split())
    if limpo in _COMPOSTOS:
        return list(_COMPOSTOS[limpo])
    return [p for p, rx in _REGEX_PALAVRAS.items() if rx.search(limpo)]


_REGEX_PALAVRAS = {p: re.compile(rf"\b(?:{rx})\b") for p, rx in _PALAVRAS.items()}
MIN_NUCLEO = 10
NIVEIS = {"amplo": 0.7, "normal": 0.8, "estrito": 1.0}


@dataclass(frozen=True)
class Regua:
    pai: str
    referencia: float  # a afinidade que vale 1,0
    tipo: str  # "nucleo" | "nucleo_amplo" | "global"
    nucleo: int  # faixas do núcleo usado (0 na global)
    referencia_maest: float | None = None  # a fração do MAEST que vale 1,0 (#88); None = sem a visão do MAEST


class Afinidades:
    """Afinidade e régua de todos os gêneros-pai, medidas sobre a biblioteca analisada deste banco."""

    def __init__(self, con: sqlite3.Connection):
        self._con = con
        self._aprendido = None
        self.d = espacos.probabilidades(con, "discogs400")
        self.j = espacos.probabilidades(con, "jamendo_genre")
        self.pais = sorted({c.split("---")[0] for c in self.d.colunas if "---" in c})
        # matrizes de pertencimento: estilo → pai (Discogs) e rótulo → pai (Jamendo)
        self._md = np.zeros((len(self.d.colunas), len(self.pais)), np.float64)
        for i, c in enumerate(self.d.colunas):
            if "---" in c:
                self._md[i, self.pais.index(c.split("---")[0])] = 1
        col_j = {c: i for i, c in enumerate(self.j.colunas)}
        self._mj = np.zeros((len(self.j.colunas), len(self.pais)), np.float64)
        for p, rotulos in MAPA_JAMENDO.items():
            if p in self.pais:
                for r in rotulos:
                    if r in col_j:
                        self._mj[col_j[r], self.pais.index(p)] = 1
        self._com_jamendo = self._mj.any(axis=0)
        self._genero_j = np.array([c not in DECADAS for c in self.j.colunas], bool)
        # MAEST (#88, ADR-15): mesma taxonomia do Discogs ("Pai---Estilo"), outra rede e outro treino
        self.m = espacos.probabilidades(con, "maest519")
        self._mm = np.zeros((len(self.m.colunas), len(self.pais)), np.float64)
        for i, c in enumerate(self.m.colunas):
            if "---" in c and (pai := c.split("---")[0]) in self.pais:
                self._mm[i, self.pais.index(pai)] = 1
        base = [v for v in espacos.da_biblioteca(con) if v in self.d.indice]
        fd, fj, tem_j = self._fracoes(base)
        afin = self._fundir(fd, fj, tem_j)
        self._mascaras: dict[str, np.ndarray] = {}
        self.reguas = self._medir_reguas(afin, fd, fj, tem_j)
        self._medir_reguas_maest(base)

    # ------------------------------------------------------------------ cálculo

    def _fracoes(self, ids: list[str]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(fração Discogs, fração Jamendo, tem Jamendo), linhas alinhadas a `ids`; faixa sem Discogs = 0."""
        n, p = len(ids), len(self.pais)
        fd, fj, tem_j = np.zeros((n, p)), np.zeros((n, p)), np.zeros(n, bool)
        li = [k for k, v in enumerate(ids) if v in self.d.indice]
        if li:
            X = self.d.X[[self.d.indice[ids[k]] for k in li]].astype(np.float64)
            fd[li] = (X @ self._md) / np.maximum(X.sum(axis=1, keepdims=True), 1e-12)
        lj = [k for k, v in enumerate(ids) if v in self.j.indice]
        if lj and self._mj.any():
            Y = self.j.X[[self.j.indice[ids[k]] for k in lj]].astype(np.float64)
            total = Y[:, self._genero_j].sum(axis=1, keepdims=True)
            fj[lj] = (Y @ self._mj) / np.maximum(total, 1e-12)
            tem_j[lj] = True
        return fd, fj, tem_j

    def _fundir(self, fd: np.ndarray, fj: np.ndarray, tem_j: np.ndarray) -> np.ndarray:
        usa_j = tem_j[:, None] & self._com_jamendo[None, :]
        return np.where(usa_j, (fd + fj) / 2, fd)

    def _medir_reguas(self, afin, fd, fj, tem_j) -> dict[str, Regua]:
        if not len(afin):
            return {p: Regua(p, 1.0, "global", 0) for p in self.pais}
        primeiro_d = fd.argmax(axis=1)
        # no Jamendo, só os pais que ele tem disputam o 1º lugar
        fj_mascarado = np.where(self._com_jamendo[None, :], fj, -1.0)
        primeiro_j = fj_mascarado.argmax(axis=1)
        medidas: dict[str, tuple[float, str, int]] = {}
        for k, p in enumerate(self.pais):
            em_d = primeiro_d == k
            em_j = (primeiro_j == k) & tem_j if self._com_jamendo[k] else np.zeros(len(afin), bool)
            todas = em_d & (em_j | ~tem_j) if self._com_jamendo[k] else em_d
            qualquer = em_d | em_j
            for nome, mascara in (("nucleo", todas), ("nucleo_amplo", qualquer)):
                if mascara.sum() >= MIN_NUCLEO:
                    medidas[p] = (float(np.median(afin[mascara, k])), nome, int(mascara.sum()))
                    self._mascaras[p] = mascara
                    break
        nucleos = [m for m, tipo, _ in medidas.values() if tipo == "nucleo"]
        # sem núcleo nenhum (biblioteca pequena demais): a afinidade típica do gênero dominante de cada faixa
        global_ = float(np.median(nucleos)) if nucleos else float(np.median(afin.max(axis=1))) or 1.0
        return {
            p: Regua(p, *medidas[p]) if p in medidas else Regua(p, global_, "global", 0) for p in self.pais
        }

    def fracao_maest(self, ids: list[str]) -> tuple[np.ndarray, np.ndarray]:
        """(fração do MAEST por pai, tem MAEST), linhas alinhadas a `ids`."""
        fm, tem = np.zeros((len(ids), len(self.pais))), np.zeros(len(ids), bool)
        li = [k for k, v in enumerate(ids) if v in self.m.indice]
        if li and self._mm.any():
            X = self.m.X[[self.m.indice[ids[k]] for k in li]].astype(np.float64)
            fm[li] = (X @ self._mm) / np.maximum(X.sum(axis=1, keepdims=True), 1e-12)
            tem[li] = True
        return fm, tem

    def _medir_reguas_maest(self, base: list[str]) -> None:
        """A régua do MAEST de cada pai: a mediana da fração dele no MESMO núcleo da régua principal.

        Só quando há pelo menos `MIN_NUCLEO` faixas do núcleo com MAEST; senão o pai fica só com a régua principal
        (biblioteca ainda sem os extras, ou pai na referência global)."""
        fm, tem = self.fracao_maest(base)
        for k, p in enumerate(self.pais):
            mascara = self._mascaras.get(p)
            if mascara is None or (mascara & tem).sum() < MIN_NUCLEO:
                continue
            ref = float(np.median(fm[mascara & tem, k]))
            if ref > 0:
                r = self.reguas[p]
                self.reguas[p] = Regua(r.pai, r.referencia, r.tipo, r.nucleo, ref)

    # ------------------------------------------------------------------ consulta

    def afinidade(self, ids: list[str]) -> np.ndarray:
        """Matriz (faixas × pais) da afinidade bruta do Discogs + Jamendo, na ordem de `self.pais`."""
        return self._fundir(*self._fracoes(ids))

    def relativa(self, pai: str, ids: list[str]) -> np.ndarray:
        """Afinidade de `pai` na régua do gênero (1,0 = típica do núcleo). Pai que não existe: zeros.

        É a régua das receitas (`genero_pai_min`). A com o MAEST fica em `relativa_com_maest`."""
        if pai not in self.pais:
            return np.zeros(len(ids), np.float64)
        return self.afinidade(ids)[:, self.pais.index(pai)] / self.reguas[pai].referencia

    @property
    def aprendido(self):
        """Os modelos aprendidos com os rótulos do usuário (#93), calculados só quando alguém pede."""
        if self._aprendido is None:
            from curator.motor import aprendido

            self._aprendido = aprendido.obter(self._con)
        return self._aprendido

    def usa_aprendida(self, pai: str) -> bool:
        return pai in REGUA_APRENDIDA and pai in self.aprendido.modelos

    def fora_do_aprendido(self, ids: list[str]) -> np.ndarray:
        """Faixas que a régua v2 põe em 1º lugar num pai SEM modelo aprendido (trilha sonora, clássica…): o portão
        da #93. O modelo só conhece os pais rotulados, então não julga essas."""
        if not ids:
            return np.zeros(0, bool)
        refs = np.array([self.reguas[p].referencia for p in self.pais])
        dominante = (self.afinidade(ids) / refs).argmax(axis=1)
        com_modelo = np.array([p in self.aprendido.modelos for p in self.pais])
        return ~com_modelo[dominante]

    def nao_julgadas(self, ids: list[str]) -> np.ndarray:
        """Faixas em que o modelo aprendido não opina: barradas pelo portão (`fora_do_aprendido`) ou pela guarda
        (#101: longe de tudo o que foi rotulado). Nelas vale a v2."""
        if not ids:
            return np.zeros(0, bool)
        return self.fora_do_aprendido(ids) | ~self.aprendido.dentro_da_guarda(ids)

    def relativa_aprendida(self, pai: str, ids: list[str]) -> np.ndarray:
        """A régua aprendida (#93, #101, ADR-17): o modelo nas faixas que ele julga (`nao_julgadas`), a relativa
        (v2) nas outras, e os rótulos do usuário por cima. NaN em tudo se o pai não tem modelo."""
        if pai not in self.aprendido.modelos:
            return np.full(len(ids), np.nan)
        ap = self.aprendido.relativa(pai, ids)
        rel = np.where(np.isnan(ap) | self.nao_julgadas(ids), self.relativa(pai, ids), ap)
        return self.aprendido.sobrepor_rotulos(pai, ids, rel)

    def relativa_receita(self, pai: str, ids: list[str]) -> np.ndarray:
        """A régua do `genero_pai_min`: a aprendida nos pais de `REGUA_APRENDIDA` que têm modelo, a relativa (v2)
        nos outros."""
        if self.usa_aprendida(pai):
            return self.relativa_aprendida(pai, ids)
        return self.relativa(pai, ids)

    def relativa_com_maest(self, pai: str, ids: list[str]) -> np.ndarray:
        """A mesma régua com o MAEST (#88, ADR-15): média da régua do Discogs + Jamendo com a da fração do MAEST,
        cada uma relativa ao próprio núcleo. Faixa sem MAEST (ou pai sem régua do MAEST) fica com `relativa`.

        Só aparece para a IA na fronteira, não entra na receita: separa melhor o rap e o Electronic, mas o MAEST
        chama boa parte do rock japonês de "J-pop" (no 0,8, o Rock do gabarito cai de 84% para 72% de revocação).
        A decisão fica para o gênero aprendido com os rótulos do usuário (#93)."""
        base = self.relativa(pai, ids)
        reg = self.reguas.get(pai)
        if reg is None or reg.referencia_maest is None:
            return base
        fm, tem = self.fracao_maest(ids)
        return np.where(tem, (base + fm[:, self.pais.index(pai)] / reg.referencia_maest) / 2, base)


class OutrasVisoes:
    """O que os sinais de fora da afinidade dizem de uma faixa (#88), para a IA conferir a fronteira de um gênero.

    - `maest`: fração dos estilos do pai no MAEST (mesma taxonomia do Discogs, outra rede);
    - `rapping`/`singing`: classes do YAMNet no percentil 90 dos quadros (só no Hip Hop): técnica vocal, não gênero;
    - `generos_externos`: os rótulos mais votados do artista no MusicBrainz e os do álbum no Deezer.
    Faixa sem o dado: a chave não aparece."""

    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self.maest = espacos.probabilidades(con, "maest519")
        self.yam = espacos.probabilidades(con, "yamnet_p90")

    def da_faixa(self, vid: str, pai: str) -> dict:
        out: dict = {}
        if vid in self.maest.indice:
            x = self.maest.linha(vid).astype(np.float64)
            cols = [i for i, c in enumerate(self.maest.colunas) if c.startswith(pai + "---")]
            out["maest"] = round(float(x[cols].sum() / max(x.sum(), 1e-12)), 3)
        if pai == "Hip Hop" and vid in self.yam.indice:
            y = self.yam.linha(vid)
            for nome, col in (("rapping", "Rapping"), ("singing", "Singing")):
                if col in self.yam.colunas:
                    out[nome] = round(float(y[self.yam.colunas.index(col)]), 3)
        ext = [
            r[0]
            for r in self.con.execute(
                """SELECT rotulo FROM externos_generos WHERE video_id = ?
                     AND ((fonte = 'musicbrainz' AND nivel = 'artista') OR (fonte = 'deezer' AND nivel = 'album'))
                   ORDER BY fonte = 'deezer', peso DESC, rotulo LIMIT 4""",
                (vid,),
            )
        ]
        if ext:
            out["generos_externos"] = ext
        return out
