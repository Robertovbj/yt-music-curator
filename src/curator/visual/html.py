"""Visualizador da biblioteca (ADR-11, #40 e #41): um HTML autocontido que abre offline.

O plotly.min.js vai embutido (sai do pacote `plotly`). Os dados vão num <script type="application/json">:
faixas, playlists, clusters do último agrupamento, gênero dominante, coordenadas 2D/3D de cada espaço e os
vizinhos mais próximos de cada faixa (no embedding EffNet), para as linhas que aparecem ao clicar.
"""

import json
import sqlite3
from collections import Counter
from datetime import datetime
from importlib import resources
from pathlib import Path

import numpy as np

from curator.banco import consultas
from curator.motor import clusters, espacos
from curator.visual import projecoes

K_VIZINHOS = 8
MAX_GENEROS = 11


def dados(con: sqlite3.Connection) -> dict:
    ids = espacos.da_biblioteca(con)
    pos = {v: i for i, v in enumerate(ids)}
    meta = {
        r["video_id"]: r
        for r in con.execute(
            f"SELECT video_id, titulo, artistas FROM faixas WHERE video_id IN ({','.join('?' * len(ids))})", ids
        )
    } if ids else {}  # fmt: skip
    esc = espacos.escalares(con)
    col = {c: i for i, c in enumerate(esc.colunas)}

    def feature(v: str, nome: str):
        x = esc.X[esc.indice[v], col[nome]] if v in esc.indice else np.nan
        return None if np.isnan(x) else round(float(x), 3)

    # gênero dominante (argmax do Discogs400) e o gênero-pai, para colorir
    d400 = espacos.probabilidades(con, "discogs400")
    estilo = (
        {v: d400.colunas[int(np.argmax(d400.X[i]))] for i, v in enumerate(d400.ids)} if d400.colunas else {}
    )
    pais = Counter(e.split("---")[0] for e in estilo.values())
    generos = [g for g, _ in sorted(pais.items(), key=lambda x: (-x[1], x[0]))[:MAX_GENEROS]] + ["Outros"]

    playlists = con.execute(
        "SELECT playlist_id, titulo FROM playlists WHERE propria = 1 AND descobertas = 0 AND ignorada = 0 ORDER BY titulo"
    ).fetchall()
    idx_pl = {p["playlist_id"]: i for i, p in enumerate(playlists)}
    em_pl: dict[str, list[int]] = {v: [] for v in ids}
    for pid, vid in con.execute("SELECT DISTINCT playlist_id, video_id FROM playlist_faixas"):
        if vid in em_pl and pid in idx_pl:
            em_pl[vid].append(idx_pl[pid])

    aid = clusters.ultimo(con)
    cs = clusters.carregar(con, aid) if aid else []
    em_cl: dict[str, list[int]] = {v: [] for v in ids}
    for i, c in enumerate(cs):
        for v in c.membros:
            if v in em_cl:
                em_cl[v].append(i)

    nao_alocadas = set(consultas.nao_alocadas(con))
    faixas = []
    for v in ids:
        m = meta.get(v)
        e = estilo.get(v)
        pai = e.split("---")[0] if e else None
        faixas.append({
            "id": v,
            "t": m["titulo"] if m else v,
            "a": ", ".join(a["nome"] for a in json.loads(m["artistas"])[:3]) if m else "",
            "pl": sorted(em_pl[v]),
            "c": em_cl[v],
            "e": e.replace("---", " · ") if e else None,
            "g": generos.index(pai) if pai in generos else len(generos) - 1,
            "v": feature(v, "valence"),
            "ar": feature(v, "arousal"),
            "bpm": feature(v, "bpm"),
            "en": feature(v, "energy"),
            "na": v in nao_alocadas,
        })  # fmt: skip

    coords = {}
    for espaco in projecoes.ESPACOS:
        coords[espaco] = {}
        for dims in (2, 3):
            m = projecoes.projetar(con, espaco, dims)
            arr = [None] * len(ids)
            for i, v in enumerate(m.ids):
                if v in pos:
                    arr[pos[v]] = [round(float(x), 4) for x in m.X[i]]
            coords[espaco][str(dims)] = arr

    emb = espacos.embeddings(con)
    vizinhos = []
    for v in ids:
        if v in emb.indice:
            viz = espacos.vizinhos(emb, emb.linha(v), k=K_VIZINHOS, excluir={v})
            vizinhos.append([pos[x] for x, _ in viz if x in pos])
        else:
            vizinhos.append([])

    return {
        "gerado_em": datetime.now().astimezone().strftime("%d/%m/%Y %H:%M"),
        "biblioteca": len(consultas.biblioteca(con)),
        "faixas": faixas,
        "playlists": [p["titulo"] for p in playlists],
        "clusters": [c.nome for c in cs],
        "generos": generos,
        "coords": coords,
        "vizinhos": vizinhos,
    }


def gerar(con: sqlite3.Connection, destino: Path) -> Path:
    plotly_js = resources.files("plotly").joinpath("package_data/plotly.min.js").read_text(encoding="utf-8")
    payload = json.dumps(dados(con), ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = MODELO.replace("__PLOTLY__", plotly_js).replace("__DADOS__", payload)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(html, encoding="utf-8")
    return destino


MODELO = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mapa da biblioteca</title>
<style>
/* Catppuccin Mocha (catppuccin.com/palette), sempre escuro */
:root { color-scheme:dark; --bg:#1e1e2e; --painel:#181825; --tinta:#cdd6f4; --suave:#a6adc8;
        --linha:#313244; --linha-forte:#45475a; --neutro:#6c7086; --acento:#cba6f7; }
* { box-sizing:border-box; }
html, body { margin:0; height:100%; background:var(--bg); color:var(--tinta); font:14px/1.45 system-ui, "Segoe UI", sans-serif; }
.app { display:grid; grid-template-rows:auto 1fr; height:100%; }
header { display:flex; flex-wrap:wrap; gap:10px 18px; align-items:center; padding:10px 16px; background:var(--painel); border-bottom:1px solid var(--linha); }
header h1 { font-size:16px; margin:0 8px 0 0; font-weight:650; }
header .meta { color:var(--suave); font-size:12px; }
label { display:flex; gap:6px; align-items:center; color:var(--suave); font-size:12px; }
select, input[type=search] { font:inherit; font-size:13px; color:var(--tinta); background:var(--bg); border:1px solid var(--linha); border-radius:6px; padding:5px 8px; }
input[type=search] { width:220px; }
.corpo { display:grid; grid-template-columns:1fr 300px; min-height:0; }
#grafico { min-height:0; }
aside { border-left:1px solid var(--linha); background:var(--painel); padding:14px 16px; overflow:auto; }
aside h2 { font-size:13px; margin:0 0 8px; color:var(--suave); font-weight:600; text-transform:uppercase; letter-spacing:.05em; }
.ficha .titulo { font-size:15px; font-weight:650; }
.ficha .artista { color:var(--suave); margin-bottom:10px; }
.ficha dl { display:grid; grid-template-columns:auto 1fr; gap:3px 10px; margin:0 0 12px; font-size:13px; }
.ficha dt { color:var(--suave); } .ficha dd { margin:0; font-variant-numeric:tabular-nums; }
.ficha ul { margin:4px 0 12px; padding-left:18px; } .ficha li { margin:2px 0; }
.vazio { color:var(--suave); }
button.viz { font:inherit; color:var(--acento); background:none; border:0; padding:0; cursor:pointer; text-align:left; }
@media (max-width: 860px) { .corpo { grid-template-columns:1fr; grid-template-rows:1fr auto; } aside { border-left:0; border-top:1px solid var(--linha); max-height:40vh; } }
</style>
<script>__PLOTLY__</script>
</head>
<body>
<div class="app">
  <header>
    <h1>Mapa da biblioteca</h1>
    <label>Vista <select id="vista">
      <option value="2">UMAP 2D</option><option value="3">UMAP 3D</option><option value="va">Valência × excitação</option></select></label>
    <label>Espaço <select id="espaco">
      <option value="effnet">EffNet (timbre/estilo)</option><option value="musicnn">MusiCNN</option><option value="interpretavel">Interpretável (features + gêneros)</option></select></label>
    <label>Cor <select id="cor">
      <option value="cluster">Cluster sugerido</option><option value="playlist">Playlist</option><option value="genero">Gênero dominante</option>
      <option value="quadrante">Valência × excitação</option><option value="bpm">BPM</option><option value="energia">Energia</option><option value="na">Não alocada</option></select></label>
    <label id="rotulo-pl">Playlist <select id="playlist"><option value="multi">Todas (nº de playlists)</option></select></label>
    <label><input type="checkbox" id="centroides"> centroides das playlists</label>
    <label>Buscar <input type="search" id="busca" list="titulos" placeholder="título ou artista"></label>
    <datalist id="titulos"></datalist>
    <span class="meta" id="meta"></span>
  </header>
  <div class="corpo">
    <div id="grafico"></div>
    <aside><h2>Faixa</h2><div class="ficha" id="ficha"><p class="vazio">Clique num ponto (ou busque) para ver a faixa e as 8 mais parecidas.</p></div></aside>
  </div>
</div>
<script type="application/json" id="dados">__DADOS__</script>
<script>
const D = JSON.parse(document.getElementById("dados").textContent);
const F = D.faixas;
// Acentos do Catppuccin Mocha, em ordem que alterna os matizes vizinhos
const M = {blue: "#89b4fa", peach: "#fab387", green: "#a6e3a1", mauve: "#cba6f7", red: "#f38ba8", teal: "#94e2d5", yellow: "#f9e2af",
  pink: "#f5c2e7", sapphire: "#74c7ec", maroon: "#eba0ac", lavender: "#b4befe", sky: "#89dceb", flamingo: "#f2cdcd", rosewater: "#f5e0dc"};
const PALETA = Object.values(M);
const CINZA = "#6c7086";  // overlay0
// Mais categorias que cores: a cor se repete com outro símbolo (o 3D aceita menos símbolos)
const SIMBOLOS = {2: ["circle", "diamond", "square", "triangle-up"], 3: ["circle", "diamond", "square", "cross"]};
// Escala contínua só com acentos, com a claridade (OKLab) sempre crescente: blue → mauve → peach → yellow
const ESCALA = [[0, M.blue], [.35, M.mauve], [.7, M.peach], [1, M.yellow]];
const QUADRANTES = ["Animado / feliz", "Tenso / agitado", "Triste / melancólico", "Calmo / sereno"];
const $ = id => document.getElementById(id);
// Sem WebGL (alguns navegadores headless, GPUs bloqueadas) o 2D cai para SVG e o 3D fica indisponível.
const GL = (() => { try { return !!document.createElement("canvas").getContext("webgl"); } catch (e) { return false; } })();
if (!GL) $("vista").querySelector('option[value="3"]').disabled = true;
let selecionado = null;

$("meta").textContent = `${F.length} de ${D.biblioteca} faixas analisadas · gerado em ${D.gerado_em}`;
D.playlists.forEach((p, i) => $("playlist").insertAdjacentHTML("beforeend", `<option value="${i}">${esc(p)}</option>`));
$("titulos").innerHTML = F.map(f => `<option value="${esc(f.t)} — ${esc(f.a)}">`).join("");
if (!D.clusters.length) $("cor").value = "genero";

function esc(s) { return String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
function css(nome) { return getComputedStyle(document.documentElement).getPropertyValue(nome).trim(); }
function categoria(k) { return {cor: PALETA[k % PALETA.length], simbolo: Math.floor(k / PALETA.length)}; }
function quadrante(f) { if (f.v == null || f.ar == null) return null; return f.ar >= .5 ? (f.v >= .5 ? 0 : 1) : (f.v >= .5 ? 3 : 2); }

function posicao(i) {
  const vista = $("vista").value;
  if (vista === "va") { const f = F[i]; return f.v == null || f.ar == null ? null : [f.v, f.ar]; }
  return D.coords[$("espaco").value][vista][i];
}

function grupos() {
  // devolve [{nome, cor, idx[]}] (categórico) ou {continuo: campo}
  const cor = $("cor").value, todos = F.map((_, i) => i);
  if (cor === "bpm" || cor === "energia") return {continuo: cor === "bpm" ? "bpm" : "en", titulo: cor === "bpm" ? "BPM" : "Energia"};
  if (cor === "cluster") {
    const g = D.clusters.map((nome, c) => ({nome, ...categoria(c), idx: []}));
    const sem = {nome: "sem cluster", cor: CINZA, idx: []};
    todos.forEach(i => (F[i].c.length ? g[F[i].c[0]].idx : sem.idx).push(i));
    return [...g, sem];
  }
  if (cor === "genero") return D.generos.map((nome, k) => ({nome, ...(k === D.generos.length - 1 ? {cor: CINZA} : categoria(k)), idx: todos.filter(i => F[i].g === k)}));
  if (cor === "quadrante") {
    const g = QUADRANTES.map((nome, k) => ({nome, cor: [M.yellow, M.red, M.blue, M.teal][k], idx: []}));
    const sem = {nome: "sem valência", cor: CINZA, idx: []};
    todos.forEach(i => { const q = quadrante(F[i]); (q == null ? sem.idx : g[q].idx).push(i); });
    return [...g, sem];
  }
  if (cor === "na") return [{nome: "não alocada", cor: M.red, idx: todos.filter(i => F[i].na)}, {nome: "em alguma playlist", cor: CINZA, idx: todos.filter(i => !F[i].na)}];
  const p = $("playlist").value;
  if (p === "multi") {
    const nomes = ["nenhuma", "1 playlist", "2 playlists", "3 ou mais"], cores = [CINZA, M.blue, M.peach, M.red];
    return nomes.map((nome, k) => ({nome, cor: cores[k], idx: todos.filter(i => Math.min(F[i].pl.length, 3) === k)}));
  }
  const pi = +p;
  return [{nome: D.playlists[pi], cor: css("--acento"), idx: todos.filter(i => F[i].pl.includes(pi))}, {nome: "outras", cor: CINZA, idx: todos.filter(i => !F[i].pl.includes(pi))}];
}

function traco(idx, extra) {
  const tres = $("vista").value === "3";
  const pts = idx.map(i => [i, posicao(i)]).filter(x => x[1]);
  const t = {
    type: tres ? "scatter3d" : GL ? "scattergl" : "scatter", mode: "markers",
    x: pts.map(p => p[1][0]), y: pts.map(p => p[1][1]), customdata: pts.map(p => p[0]),
    text: pts.map(([i]) => `${esc(F[i].t)} — ${esc(F[i].a)}<br>${esc(F[i].e || "")}${F[i].pl.length ? "<br>" + F[i].pl.map(k => esc(D.playlists[k])).join(", ") : ""}`),
    hovertemplate: "%{text}<extra></extra>",
    marker: {size: tres ? 3 : 6, opacity: .85, line: {width: pts.map(([i]) => F[i].pl.length > 1 ? 1.2 : 0), color: css("--tinta")}},
  };
  if (tres) { t.z = pts.map(p => p[1][2]); t.marker.line = {width: 0}; }
  return Object.assign(t, extra);
}

function desenhar() {
  const vista = $("vista").value, tres = vista === "3";
  $("espaco").disabled = vista === "va";
  $("rotulo-pl").style.display = $("cor").value === "playlist" ? "" : "none";
  const g = grupos();
  let tracos;
  if (g.continuo) {
    const t = traco(F.map((_, i) => i).filter(i => F[i][g.continuo] != null), {name: g.titulo});
    t.marker.color = t.customdata.map(i => F[i][g.continuo]);
    t.marker.colorscale = ESCALA; t.marker.showscale = true;
    // ancorada embaixo, deixa o topo livre para a legenda (vizinhos, selecionada, centroides)
    t.marker.colorbar = {title: {text: g.titulo}, thickness: 12, len: .7, y: 0, yanchor: "bottom", outlinewidth: 0, tickcolor: css("--linha-forte")};
    tracos = [t];
  } else {
    const simbolos = SIMBOLOS[tres ? 3 : 2];
    tracos = g.filter(x => x.idx.length).map(x => {
      const t = traco(x.idx, {name: `${x.nome} (${x.idx.length})`});
      t.marker.color = x.cor;
      if (x.simbolo) t.marker.symbol = simbolos[x.simbolo % simbolos.length];
      return t;
    });
  }
  if ($("centroides").checked) {
    const cx = [], cy = [], cz = [], nomes = [];
    D.playlists.forEach((nome, k) => {
      const ps = F.map((_, i) => i).filter(i => F[i].pl.includes(k)).map(posicao).filter(Boolean);
      if (ps.length < 3) return;
      const m = d => ps.reduce((s, p) => s + p[d], 0) / ps.length;
      cx.push(m(0)); cy.push(m(1)); if (tres) cz.push(m(2)); nomes.push(nome);
    });
    const c = {type: tres ? "scatter3d" : "scatter", mode: "markers+text", x: cx, y: cy, text: nomes, textposition: "top center",
      name: "centroides", hoverinfo: "text", marker: {symbol: "diamond", size: tres ? 5 : 11, color: css("--tinta"), line: {width: 1, color: css("--bg")}},
      textfont: {size: 10, color: css("--tinta")}};
    if (tres) c.z = cz;
    tracos.push(c);
  }
  if (selecionado != null && posicao(selecionado)) {
    const p0 = posicao(selecionado), lx = [], ly = [], lz = [];
    D.vizinhos[selecionado].forEach(j => { const p = posicao(j); if (!p) return; lx.push(p0[0], p[0], null); ly.push(p0[1], p[1], null); if (tres) lz.push(p0[2], p[2], null); });
    const linhas = {type: tres ? "scatter3d" : "scatter", mode: "lines", x: lx, y: ly, name: "vizinhos", hoverinfo: "skip", line: {color: css("--acento"), width: 2}};
    const alvo = {type: tres ? "scatter3d" : "scatter", mode: "markers", x: [p0[0]], y: [p0[1]], name: "selecionada", hoverinfo: "skip",
      marker: {size: tres ? 8 : 16, color: "rgba(0,0,0,0)", line: {width: 3, color: css("--acento")}}};
    if (tres) { linhas.z = lz; alvo.z = [p0[2]]; }
    tracos.push(linhas, alvo);
  }
  const eixo = {showgrid: vista === "va", zeroline: false, showticklabels: vista === "va", gridcolor: css("--linha"),
    linecolor: css("--linha"), color: css("--suave")};
  const layout = {
    paper_bgcolor: css("--bg"), plot_bgcolor: css("--bg"), font: {color: css("--tinta")}, margin: {l: 40, r: 10, t: 40, b: 40},  // t: espaço para a barra de ferramentas não cobrir a legenda
    legend: {itemsizing: "constant", bgcolor: "rgba(0,0,0,0)"}, uirevision: vista + $("espaco").value, hovermode: "closest",
    hoverlabel: {bgcolor: css("--painel"), bordercolor: css("--linha-forte"), font: {color: css("--tinta")}},
    modebar: {bgcolor: "rgba(0,0,0,0)", color: css("--neutro"), activecolor: css("--acento")},
    xaxis: {...eixo, title: vista === "va" ? {text: "valência →"} : undefined, range: vista === "va" ? [0, 1] : undefined},
    yaxis: {...eixo, title: vista === "va" ? {text: "excitação →"} : undefined, range: vista === "va" ? [0, 1] : undefined},
  };
  const eixo3 = {...eixo, title: {text: ""}, showbackground: false, zerolinecolor: css("--linha"), spikecolor: css("--acento")};
  if (tres) layout.scene = {xaxis: eixo3, yaxis: eixo3, zaxis: eixo3, bgcolor: css("--bg")};
  if (vista === "va") {
    layout.shapes = [{type: "line", x0: .5, x1: .5, y0: 0, y1: 1, line: {color: css("--linha"), width: 1}}, {type: "line", x0: 0, x1: 1, y0: .5, y1: .5, line: {color: css("--linha"), width: 1}}];
    layout.annotations = [[.98, .98, "Animado / feliz", "right"], [.02, .98, "Tenso / agitado", "left"], [.02, .02, "Triste / melancólico", "left"], [.98, .02, "Calmo / sereno", "right"]]
      .map(([x, y, text, xanchor]) => ({x, y, text, xanchor, yanchor: y > .5 ? "top" : "bottom", showarrow: false, font: {color: css("--suave"), size: 12}}));
  }
  Plotly.react("grafico", tracos, layout, {responsive: true, displaylogo: false});
}

function ficha(i) {
  const f = F[i], fmt = x => x == null ? "—" : x;
  $("ficha").innerHTML = `<div class="titulo">${esc(f.t)}</div><div class="artista">${esc(f.a)}</div>
    <dl><dt>Gênero</dt><dd>${esc(f.e || "—")}</dd><dt>BPM</dt><dd>${fmt(f.bpm && Math.round(f.bpm))}</dd>
    <dt>Energia</dt><dd>${fmt(f.en)}</dd><dt>Valência</dt><dd>${fmt(f.v)}</dd><dt>Excitação</dt><dd>${fmt(f.ar)}</dd>
    <dt>Cluster</dt><dd>${f.c.length ? f.c.map(c => esc(D.clusters[c])).join(", ") : "—"}</dd></dl>
    <h2>Playlists</h2>${f.pl.length ? "<ul>" + f.pl.map(k => `<li>${esc(D.playlists[k])}</li>`).join("") + "</ul>" : '<p class="vazio">Nenhuma (não alocada).</p>'}
    <h2>Mais parecidas</h2><ul>${D.vizinhos[i].map(j => `<li><button class="viz" data-i="${j}">${esc(F[j].t)} — ${esc(F[j].a)}</button></li>`).join("")}</ul>`;
}

function selecionar(i) { selecionado = i; ficha(i); desenhar(); }
["vista", "espaco", "cor", "playlist", "centroides"].forEach(id => $(id).addEventListener("change", desenhar));
$("busca").addEventListener("change", e => {
  const q = e.target.value.toLowerCase();
  const i = F.findIndex(f => `${f.t} — ${f.a}`.toLowerCase() === q) ?? -1;
  const j = i >= 0 ? i : F.findIndex(f => `${f.t} ${f.a}`.toLowerCase().includes(q));
  if (j >= 0) selecionar(j);
});
$("ficha").addEventListener("click", e => { const b = e.target.closest("button.viz"); if (b) selecionar(+b.dataset.i); });
desenhar();
$("grafico").on("plotly_click", ev => { const p = ev.points.find(p => p.customdata != null); if (p) selecionar(p.customdata); });
</script>
</body>
</html>
"""
