"""Generate the interactive source-paper corpus HTML.

Produces a single self-contained HTML file from the corpus data:
  * Sky map of catalog sources (color = log paper count, click = focus)
  * Papers-per-source distribution (log-binned)
  * Top papers by source coverage (click = focus)
  * arXiv coverage donut
  * Year distribution
  * Source-paper bipartite network (subset)

Click a source in the sky map -> its papers fill the "Source focus" panel.
Click a paper in the top-papers chart -> its sources highlight on the sky map
and fill the "Paper focus" panel.

Output is offline-friendly: corpus data is embedded as JSON, and callers can
inline the package-vendored ECharts runtime to produce one standalone file.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

GAIA_ID_COL = "Gaia"
RA_COL = "RAICRS"
DEC_COL = "DEICRS"
NAME_FALLBACK = "main_id"


def _short_source_label(gaia_id: str, main_id: str = "", corpus_prefix: str = "Gaia") -> str:
    """Compact label for a Gaia source: prefer SIMBAD main_id, else last 8 digits
    prefixed by ``<corpus_prefix>-`` (e.g. ``Gaia-12345678``).
    """
    if main_id and str(main_id) != "nan":
        return str(main_id)
    return f"{corpus_prefix}-{str(gaia_id)[-8:]}"


def _paper_label(arxiv_id: str, bibcode: str) -> str:
    return arxiv_id or bibcode


def _paper_url(arxiv_id: str, doi: str, bibcode: str) -> str:
    if arxiv_id:
        return f"https://arxiv.org/abs/{arxiv_id}"
    if doi:
        return f"https://doi.org/{doi}"
    if bibcode:
        return f"https://ui.adsabs.harvard.edu/abs/{bibcode}"
    return ""


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        if pd.isna(x):
            return default
        return int(x)
    except (TypeError, ValueError):
        return default


def _safe_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        if pd.isna(x):
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _first_float(row: pd.Series, *columns: str) -> float | None:
    for column in columns:
        value = _safe_float(row.get(column))
        if value is not None:
            return value
    return None


def compute_overview(
    catalog_df: pd.DataFrame,
    edges_df: pd.DataFrame,
    *,
    name_col: str = NAME_FALLBACK,
    spectra_by_source: Optional[dict[str, dict]] = None,
) -> dict:
    """Compute the JSON-serializable overview payload from corpus data."""
    edges = edges_df.copy()
    # Ensure canonical columns exist even for empty/partial edge tables
    # (a corpus with zero SIMBAD papers otherwise raises KeyError on groupby).
    for col in ("arxiv_id", "bibcode", "title", "doi", "journal"):
        if col not in edges.columns:
            edges[col] = ""
        edges[col] = edges[col].fillna("").astype(str)
    edges["year"] = pd.to_numeric(edges.get("year"), errors="coerce")

    # Per-source stats + sky positions
    n_papers_per_src = edges.groupby("source_id").size()
    sources = []
    for _, row in catalog_df.iterrows():
        gid = str(row[GAIA_ID_COL])
        n = int(n_papers_per_src.get(gid, 0))
        main_id = str(row.get(name_col, "")) if name_col in catalog_df.columns else ""
        if (not main_id or main_id == "nan") and "Name" in catalog_df.columns:
            main_id = str(row.get("Name", ""))
        sources.append({
            "id": gid,
            "ra": _safe_float(row.get(RA_COL)),
            "dec": _safe_float(row.get(DEC_COL)),
            "n_papers": n,
            "label": _short_source_label(gid, main_id),
            "gmag": _first_float(row, "Gmag_gdr3", "Gmag"),
            "plx": _first_float(row, "Plx_gdr3", "plx"),
            "plx_err": _first_float(row, "e_Plx_gdr3", "e_plx"),
            "pm_ra": _first_float(row, "pmRA_gdr3", "pmRA"),
            "pm_dec": _first_float(row, "pmDE_gdr3", "pmDE"),
            "ruwe": _first_float(row, "RUWE_gdr3", "RUWE"),
            "has_xp": gid in (spectra_by_source or {}),
        })
    sources.sort(key=lambda s: -s["n_papers"])

    # Per-paper stats
    paper_groups = edges.groupby("bibcode", as_index=False).agg(
        n_src=("source_id", "nunique"),
        arxiv_id=("arxiv_id", "first"),
        title=("title", "first"),
        year=("year", "first"),
        journal=("journal", "first"),
        doi=("doi", "first"),
    )
    papers = []
    for _, r in paper_groups.iterrows():
        arxiv = str(r["arxiv_id"]) if r["arxiv_id"] != "" else ""
        bib = str(r["bibcode"])
        doi = str(r["doi"]) if r["doi"] != "" else ""
        year = int(r["year"]) if pd.notna(r["year"]) else None
        papers.append({
            "arxiv_id": arxiv,
            "bibcode": bib,
            "title": str(r["title"]),
            "year": year,
            "journal": str(r["journal"]),
            "n_sources": int(r["n_src"]),
            "url": _paper_url(arxiv, doi, bib),
        })
    papers.sort(key=lambda p: -p["n_sources"])

    n_with_arxiv = sum(1 for p in papers if p["arxiv_id"])

    # Year histogram (only papers with a year)
    years = [p["year"] for p in papers if p["year"]]
    year_hist = {}
    for y in years:
        year_hist[y] = year_hist.get(y, 0) + 1
    year_hist = [{"year": k, "count": v} for k, v in sorted(year_hist.items())]

    # Edges as minimal triples (for the bipartite subgraph render)
    edge_triples = [
        {"source_id": str(s), "arxiv_id": str(a), "bibcode": str(b)}
        for s, a, b in zip(
            edges["source_id"].astype(str),
            edges["arxiv_id"].astype(str),
            edges["bibcode"].astype(str),
        )
    ]

    return {
        "meta": {
            "n_sources": len(sources),
            "n_papers": len(papers),
            "n_edges": len(edge_triples),
            "n_with_arxiv": n_with_arxiv,
            "n_without_arxiv": len(papers) - n_with_arxiv,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "year_range": [min(years) if years else None, max(years) if years else None],
        },
        "sources": sources,
        "papers": papers,
        "edges": edge_triples,
        "year_hist": year_hist,
        "spectra": spectra_by_source or {},
    }


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>__CORPUS_NAME__ Source-Paper Corpus</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #0f172a; color: #e2e8f0; padding: 16px; }
  h1 { text-align:center; color: #f1f5f9; margin-bottom:2px; font-size:18px; }
  .subtitle { text-align:center; color: #94a3b8; margin-bottom:16px; font-size:13px; }
  .card { background: #1e293b; border-radius:10px; padding:14px; margin-bottom:14px; }
  .card h3 { color: #cbd5e1; font-size:13px; margin-bottom:8px; }
  .row { display:flex; gap:14px; }
  .row .card { flex:1; }
  .chart { width:100%; }
  .legend { display:flex; gap:18px; justify-content:center; margin-bottom:12px; flex-wrap:wrap; }
  .legend span { display:flex; align-items:center; gap:6px; font-size:12px; color:#94a3b8; }
  .legend .dot { width:8px; height:8px; border-radius:50%; display:inline-block; }
  .focus { display:none; }
  .focus h3 { color:#fbbf24; }
  .focus .meta { color:#94a3b8; font-size:12px; margin-bottom:8px; }
  .focus ul { list-style:none; max-height:300px; overflow-y:auto; }
  .focus li { padding:6px 0; border-bottom:1px solid #334155; font-size:12px; }
  .focus li a { color:#60a5fa; text-decoration:none; }
  .focus li a:hover { text-decoration:underline; }
  .focus li .year { color:#94a3b8; margin-left:8px; }
  .focus .title { font-size:13px; margin-bottom:4px; }
  .focus .title b { color:#f1f5f9; }
  .focus .title .gid { color:#94a3b8; font-size:11px; margin-left:8px; }
  .astro-grid { display:grid; grid-template-columns: max-content max-content; gap:2px 14px; font-size:11px; margin:4px 0 10px; }
  .astro-grid .k { color:#64748b; }
  .astro-grid .v { color:#e2e8f0; font-family: ui-monospace, SFMono-Regular, monospace; }
  .spectrum-wrap { margin:10px 0 12px; border-top:1px solid #334155; padding-top:10px; }
  .spectrum-title { color:#94a3b8; font-size:11px; margin-bottom:4px; }
  .spectrum-chart { width:100%; height:260px; }
  .empty { color:#64748b; font-style:italic; padding:20px; text-align:center; }
  .footer { text-align:center; color:#475569; font-size:11px; margin-top:8px; }
  @media (max-width: 820px) { .row { flex-direction:column; } }
</style>
</head>
<body>
<h1>__CORPUS_NAME__ Source &harr; Paper Corpus</h1>
<div class="subtitle" id="subtitle"></div>

<div class="legend">
  <span><span class="dot" style="background:#60a5fa"></span>Source (star)</span>
  <span><span class="dot" style="background:#fbbf24"></span>Selected source</span>
  <span><span class="dot" style="background:#a78bfa"></span>Selected paper's sources</span>
</div>

<div class="card" style="display:flex; gap:12px; align-items:center; flex-wrap:wrap;">
  <label style="font-size:12px; color:#94a3b8;">Focus:</label>
  <input id="q-src" placeholder="Gaia source_id  (e.g. 5412403269717562240)"
         style="flex:1; min-width:260px; padding:6px 10px; background:#0f172a; color:#e2e8f0; border:1px solid #334155; border-radius:6px; font-family:inherit; font-size:12px;">
  <button id="go-src" style="padding:6px 14px; background:#3b82f6; color:#fff; border:0; border-radius:6px; cursor:pointer; font-size:12px;">Source</button>
  <input id="q-paper" placeholder="arxiv_id / bibcode / title  (e.g. 2305.03557)"
         style="flex:1; min-width:260px; padding:6px 10px; background:#0f172a; color:#e2e8f0; border:1px solid #334155; border-radius:6px; font-family:inherit; font-size:12px;">
  <button id="go-paper" style="padding:6px 14px; background:#a78bfa; color:#fff; border:0; border-radius:6px; cursor:pointer; font-size:12px;">Paper</button>
  <span id="q-err" style="color:#ef4444; font-size:11px;"></span>
</div>

<div class="card"><h3>Sky Distribution (color = log paper count)</h3><div id="sky" style="height:380px;"></div></div>

<div class="row">
  <div class="card"><h3>Papers per Source (top 15)</h3><div id="pps" style="height:340px;"></div></div>
  <div class="card"><h3>Top Papers by Source Coverage</h3><div id="topp" style="height:340px;"></div></div>
</div>

<div class="row">
  <div class="card"><h3>arXiv Coverage</h3><div id="cov" style="height:260px;"></div></div>
  <div class="card"><h3>Paper Year Distribution</h3><div id="year" style="height:260px;"></div></div>
</div>

<div class="row">
  <div class="card focus" id="focus-source">
    <h3>Source Focus</h3>
    <div class="meta" id="focus-source-meta">Click a source in the sky map or bar chart.</div>
    <div class="spectrum-wrap" id="focus-spectrum-wrap" style="display:none;">
      <div class="spectrum-title" id="focus-spectrum-title"></div>
      <div class="spectrum-chart" id="focus-spectrum"></div>
    </div>
    <ul id="focus-source-list"></ul>
  </div>
  <div class="card focus" id="focus-paper">
    <h3>Paper Focus</h3>
    <div class="meta" id="focus-paper-meta">Click a paper bar to see its sources.</div>
    <ul id="focus-paper-list"></ul>
  </div>
</div>

<div class="card"><h3>Source-Paper Bipartite Network (top 8 sources &times; top 12 papers, by coverage)</h3><div id="bip" style="height:500px;"></div></div>

<div class="footer" id="footer"></div>

<script src="echarts.min.js"></script>
<script>
const DATA = __DATA_JSON__;

const SRC_BY_ID = {};
DATA.sources.forEach(s => SRC_BY_ID[s.id] = s);
const PAPERS_BY_BIB = {};
DATA.papers.forEach(p => PAPERS_BY_BIB[p.bibcode] = p);

document.getElementById('subtitle').textContent =
  DATA.meta.n_sources + ' sources × ' + DATA.meta.n_papers + ' papers · ' +
  DATA.meta.n_edges + ' edges (' + DATA.meta.n_with_arxiv + ' with arXiv id)';
document.getElementById('footer').textContent =
  'Generated ' + DATA.meta.generated_at + ' · corpus v0.2+';

function initChart(id, opt) {
  const dom = document.getElementById(id);
  if (!dom) return null;
  const c = echarts.init(dom);
  c.setOption(opt);
  window.addEventListener('resize', () => { try { c.resize(); } catch(e){} });
  return c;
}

const AXIS_LABEL = {color:'#94a3b8'};
const AXIS_LINE = {lineStyle:{color:'#334155'}};
const SPLIT = {lineStyle:{color:'#1e293b'}};

// ---- Sky map ----
const skyPoints = DATA.sources.map(s => ({
  name: s.label,
  value: [s.ra, s.dec, s.n_papers, s.id],
  symbolSize: Math.max(6, Math.min(36, Math.log10(s.n_papers + 1) * 12)),
  itemStyle: {color: '#60a5fa', opacity: 0.85},
}));
// Compute tight data-driven axis bounds (ECharts default "nice numbers" leave
// too much whitespace for compact regions like Orion).
const _raVals = DATA.sources.map(s => s.ra).filter(v => v != null && !isNaN(v));
const _decVals = DATA.sources.map(s => s.dec).filter(v => v != null && !isNaN(v));
const _raRange = _raVals.length ? [Math.min(..._raVals), Math.max(..._raVals)] : [0, 360];
const _decRange = _decVals.length ? [Math.min(..._decVals), Math.max(..._decVals)] : [-90, 90];
const _raPad = Math.max((_raRange[1] - _raRange[0]) * 0.05, 0.05);
const _decPad = Math.max((_decRange[1] - _decRange[0]) * 0.05, 0.05);

const skyChart = initChart('sky', {
  tooltip: {trigger:'item', formatter: p =>
    `${p.data.name}<br/>(${p.data.value[0].toFixed(2)}, ${p.data.value[1].toFixed(2)})<br/>${p.data.value[2]} papers`},
  xAxis: {name:'RA [deg]', nameTextStyle:{color:AXIS_LABEL.color}, type:'value',
    min:_raRange[0]-_raPad, max:_raRange[1]+_raPad,
    axisLabel:AXIS_LABEL, axisLine:AXIS_LINE, splitLine:SPLIT},
  yAxis: {name:'Dec [deg]', nameTextStyle:{color:AXIS_LABEL.color}, type:'value',
    min:_decRange[0]-_decPad, max:_decRange[1]+_decPad,
    axisLabel:AXIS_LABEL, axisLine:AXIS_LINE, splitLine:SPLIT},
  series: [{type:'scatter', data:skyPoints, emphasis:{itemStyle:{color:'#fbbf24', borderColor:'#fff', borderWidth:2}}}],
});

// ---- Papers per source (top 15) ----
const topSrc = DATA.sources.slice(0, 15);
const ppsChart = initChart('pps', {
  tooltip:{trigger:'axis', axisPointer:{type:'shadow'}},
  grid:{left:130, right:30, top:10, bottom:30},
  xAxis:{type:'value', axisLabel:AXIS_LABEL, splitLine:SPLIT},
  yAxis:{type:'category', data: topSrc.map(s=>s.label).reverse(),
    axisLabel:{color:AXIS_LABEL.color, fontSize:10}},
  series:[{type:'bar', data: topSrc.map(s=>s.n_papers).reverse(),
    itemStyle:{color:'#60a5fa', borderRadius:[0,4,4,0]},
    label:{show:true, position:'right', color:'#cbd5e1', fontSize:11}}],
});

// ---- Top papers ----
const topPapers = DATA.papers.slice(0, 15);
const toppChart = initChart('topp', {
  tooltip:{trigger:'axis', axisPointer:{type:'shadow'},
    formatter: p => {
      const paper = topPapers[p[0].dataIndex];
      return `${paper.arxiv_id || paper.bibcode} (${paper.year||'?'})<br/>${paper.title.slice(0,80)}<br/>${paper.n_sources} sources`;
    }},
  grid:{left:110, right:30, top:10, bottom:30},
  xAxis:{type:'value', axisLabel:AXIS_LABEL, splitLine:SPLIT},
  yAxis:{type:'category', data: topPapers.map(p=>p.arxiv_id||p.bibcode.slice(0,18)).reverse(),
    axisLabel:{color:AXIS_LABEL.color, fontSize:10}},
  series:[{type:'bar', data: topPapers.map(p=>p.n_sources).reverse(),
    itemStyle:{color:'#f472b6', borderRadius:[0,4,4,0]},
    label:{show:true, position:'right', color:'#cbd5e1', fontSize:11}}],
});

// ---- arXiv coverage donut ----
initChart('cov', {
  tooltip:{trigger:'item', formatter:'{b}: {c} ({d}%)'},
  series:[{type:'pie', radius:['50%','75%'], center:['50%','55%'],
    label:{color:'#cbd5e1', fontSize:12, formatter:'{b}\\n{c} ({d}%)'},
    data:[
      {value: DATA.meta.n_with_arxiv, name:'With arXiv', itemStyle:{color:'#60a5fa'}},
      {value: DATA.meta.n_without_arxiv, name:'Journal only', itemStyle:{color:'#475569'}},
    ]}],
});

// ---- Year distribution ----
initChart('year', {
  tooltip:{trigger:'axis'},
  grid:{left:40, right:20, top:10, bottom:30},
  xAxis:{type:'category', data: DATA.year_hist.map(y=>y.year),
    axisLabel:AXIS_LABEL, axisLine:AXIS_LINE},
  yAxis:{type:'value', axisLabel:AXIS_LABEL, splitLine:SPLIT},
  series:[{type:'line', data: DATA.year_hist.map(y=>y.count), smooth:true,
    areaStyle:{color:'rgba(96,165,250,0.25)'}, lineStyle:{color:'#60a5fa'},
    itemStyle:{color:'#60a5fa'}}],
});

// ---- Bipartite subgraph (top 8 sources × top 12 papers) ----
const bpSrc = DATA.sources.slice(0, 8);
const bpPapers = DATA.papers.slice(0, 12);
const bpSourceIds = new Set(bpSrc.map(s => s.id));
const bpPaperBibs = new Set(bpPapers.map(p => p.bibcode));
const bpLinks = [];
DATA.edges.forEach(e => {
  if (bpSourceIds.has(e.source_id) && bpPaperBibs.has(e.bibcode)) {
    bpLinks.push({source: 'source:' + e.source_id, target: 'paper:' + e.bibcode});
  }
});
const bpNodes = [
  ...bpSrc.map((s,i) => ({id:'source:' + s.id, name: s.label, x: 100, y: 60 + i*50, symbolSize: 18,
    itemStyle:{color:'#60a5fa'}, category: 0})),
  ...bpPapers.map((p,i) => ({id:'paper:' + p.bibcode, name: p.arxiv_id||p.bibcode.slice(0,12), x: 500, y: 30 + i*36, symbolSize: 14,
    itemStyle:{color:'#f472b6'}, category: 1})),
];
initChart('bip', {
  tooltip:{},
  legend:{data:['Source','Paper'], textStyle:{color:'#94a3b8'}, top:0},
  series:[{
    type:'graph', layout:'none', data: bpNodes, links: bpLinks,
    categories:[{name:'Source'},{name:'Paper'}],
    lineStyle:{color:'#334155', width:1, opacity:0.6, curveness:0.1},
    label:{show:true, color:'#cbd5e1', fontSize:10, position:'right'},
    emphasis:{lineStyle:{color:'#fbbf24', width:2, opacity:1}},
  }],
});

// ---- Focus handlers ----
function fmtFloat(v, d=3) { return (v===null||v===undefined||isNaN(v)) ? '—' : Number(v).toFixed(d); }

function raDecToHMS(ra, dec) {
  // Best-effort hms/dms rendering; returns ['11h 01m 52s', '-34° 42\' 18"']
  try {
    const h = Math.floor(ra/15), m = Math.floor((ra/15 - h)*60),
          s = ((ra/15 - h)*60 - m)*60;
    const ds = dec<0 ? '-' : '+', ad = Math.abs(dec),
          dd = Math.floor(ad), dm = Math.floor((ad-dd)*60),
          dss = ((ad-dd)*60 - dm)*60;
    return [`${h}h ${m}m ${s.toFixed(1)}s`,
            `${ds}${dd}° ${dm}' ${dss.toFixed(1)}"`];
  } catch(e) { return ['—','—']; }
}

function astrometryGrid(s) {
  const distPc = s.plx ? (1000.0/s.plx).toFixed(1) : null;
  const [rah, dech] = raDecToHMS(s.ra||0, s.dec||0);
  const row = (k, v) => `<span class="k">${k}</span><span class="v">${v}</span>`;
  let html = '<div class="astro-grid">';
  html += row('RA',  `${fmtFloat(s.ra,4)}° <span style="color:#64748b">(${rah})</span>`);
  html += row('Dec', `${fmtFloat(s.dec,4)}° <span style="color:#64748b">(${dech})</span>`);
  const plxStr = s.plx
    ? `${fmtFloat(s.plx,3)}${s.plx_err ? ' ± ' + fmtFloat(s.plx_err,3) : ''} mas` +
      (distPc ? ` <span style="color:#64748b">→ ${distPc} pc</span>` : '')
    : '—';
  html += row('Parallax', plxStr);
  html += row('μ_RA',  s.pm_ra  ? `${fmtFloat(s.pm_ra,2)} mas/yr` : '—');
  html += row('μ_Dec', s.pm_dec ? `${fmtFloat(s.pm_dec,2)} mas/yr` : '—');
  html += row('RUWE',  fmtFloat(s.ruwe,3));
  html += row('G mag', fmtFloat(s.gmag,3));
  html += row('Papers', `${s.n_papers}`);
  html += '</div>';
  return html;
}

let spectrumChart = null;
function renderSpectrum(s) {
  const wrap = document.getElementById('focus-spectrum-wrap');
  const title = document.getElementById('focus-spectrum-title');
  const chartDom = document.getElementById('focus-spectrum');
  const spectrum = DATA.spectra[s.id];
  if (spectrumChart) { spectrumChart.dispose(); spectrumChart = null; }
  wrap.style.display = 'block';
  if (!spectrum) {
    title.textContent = 'Gaia XP spectrum unavailable';
    chartDom.innerHTML = '<div class="empty">No cached XP Continuous or Sampled data for this source.</div>';
    return;
  }

  const spectrumFormat = (spectrum.format || 'sampled').toUpperCase();
  title.textContent = `Gaia XP ${spectrumFormat} · ${spectrum.wavelength.length} wavelength points`;
  chartDom.innerHTML = '';
  const displayFlux = spectrum.flux.map(v => v == null ? null : v * 1e17);
  const displayError = spectrum.flux_error.map(v => v == null ? null : v * 1e17);
  const lower = displayFlux.map((v, i) =>
    v == null ? null : v - (displayError[i] || 0));
  const band = displayError.map(v => v == null ? null : 2 * v);
  spectrumChart = echarts.init(chartDom);
  spectrumChart.setOption({
    animation:false,
    tooltip:{trigger:'axis', formatter: params => {
      const i = params[0].dataIndex;
      const flux = displayFlux[i];
      const err = displayError[i];
      return `${spectrum.wavelength[i].toFixed(1)} nm<br/>Flux: ${flux == null ? '—' : flux.toFixed(3)} × 10⁻¹⁷` +
             (err == null ? '' : `<br/>Error: ${err.toFixed(3)} × 10⁻¹⁷`);
    }},
    grid:{left:96,right:18,top:12,bottom:42},
    xAxis:{type:'category',data:spectrum.wavelength,boundaryGap:false,
      name:'Wavelength [nm]',nameLocation:'middle',nameGap:28,
      axisLabel:{color:'#94a3b8',formatter:(v,i)=>i%40===0?Number(v).toFixed(0):''},axisLine:AXIS_LINE},
    yAxis:{type:'value',scale:true,name:'Flux [10⁻¹⁷ W m⁻² nm⁻¹]',
      nameLocation:'middle',nameGap:72,nameRotate:90,nameTextStyle:{color:'#94a3b8'},
      axisLabel:{color:'#94a3b8'},splitLine:SPLIT},
    series:[
      {type:'line',data:lower,stack:'xp-error',symbol:'none',lineStyle:{opacity:0},areaStyle:{opacity:0}},
      {type:'line',data:band,stack:'xp-error',symbol:'none',lineStyle:{opacity:0},areaStyle:{color:'rgba(96,165,250,0.25)'}},
      {name:'Flux',type:'line',data:displayFlux,symbol:'none',lineStyle:{color:'#60a5fa',width:1.4}},
    ],
  });
}

function showSourceFocus(srcId) {
  const s = SRC_BY_ID[srcId];
  if (!s) return;
  const card = document.getElementById('focus-source');
  card.style.display = 'block';
  document.getElementById('focus-source-meta').innerHTML =
    `<div class="title"><b>${s.label}</b><span class="gid">Gaia DR3 ${s.id}</span></div>` +
    astrometryGrid(s);
  renderSpectrum(s);
  const papers = DATA.edges.filter(e => e.source_id === srcId).map(e => PAPERS_BY_BIB[e.bibcode]).filter(Boolean);
  papers.sort((a,b) => (b.year||0) - (a.year||0));
  const ul = document.getElementById('focus-source-list');
  if (!papers.length) ul.innerHTML = '<div class="empty">No papers linked.</div>';
  else ul.innerHTML = papers.slice(0, 50).map(p => {
    const label = p.arxiv_id || p.bibcode.slice(0, 22);
    const title = (p.title||'').slice(0, 90);
    const year = p.year ? `<span class="year">${p.year}</span>` : '';
    const link = p.url ? `<a href="${p.url}" target="_blank">${label}</a>` : label;
    return `<li>${link}${year}<br/><span style="color:#94a3b8">${title}</span></li>`;
  }).join('');
  // Highlight on sky map
  skyChart.setOption({series:[{data: skyPoints.map(p => {
    const sel = p.value[3] === srcId;
    return {...p, itemStyle: {color: sel ? '#fbbf24' : '#60a5fa', opacity: sel ? 1 : 0.4}};
  })}]});
}

function showPaperFocus(bibcode) {
  const p = PAPERS_BY_BIB[bibcode];
  if (!p) return;
  const card = document.getElementById('focus-paper');
  card.style.display = 'block';
  document.getElementById('focus-paper-meta').innerHTML =
    `<div class="title"><b>${p.arxiv_id || p.bibcode}</b>` +
    `<span class="gid">${p.year||'?'}` + (p.journal ? ` · ${p.journal}` : '') + `</span></div>` +
    `<div style="font-size:12px; color:#cbd5e1; margin-bottom:6px;">${(p.title||'').slice(0,160)}</div>` +
    `<div style="font-size:11px; color:#94a3b8;">${p.n_sources} sources linked</div>`;
  const srcs = DATA.edges.filter(e => e.bibcode === bibcode).map(e => SRC_BY_ID[e.source_id]).filter(Boolean);
  const ul = document.getElementById('focus-paper-list');
  if (!srcs.length) { ul.innerHTML = '<div class="empty">No sources linked.</div>'; return; }
  ul.innerHTML = srcs.slice(0, 50).map(s => {
    const plx = s.plx ? `${fmtFloat(s.plx,2)} mas` : '—';
    return `<li><b>${s.label}</b> <span class="year">G=${fmtFloat(s.gmag,2)} · Plx=${plx} · RUWE=${fmtFloat(s.ruwe,2)} · ${s.n_papers}p</span>` +
           `<br/><span style="color:#94a3b8">Gaia DR3 ${s.id} · RA=${fmtFloat(s.ra,2)}° Dec=${fmtFloat(s.dec,2)}°</span></li>`;
  }).join('');
  // Highlight matching points on sky map
  const matchIds = new Set(srcs.map(s => s.id));
  skyChart.setOption({series:[{data: skyPoints.map(pt => {
    const sel = matchIds.has(pt.value[3]);
    return {...pt, itemStyle: {color: sel ? '#a78bfa' : '#60a5fa', opacity: sel ? 1 : 0.25}};
  })}]});
}

skyChart.on('click', p => { if (p.data && p.data.value) showSourceFocus(p.data.value[3]); });
ppsChart.on('click', p => { const s = topSrc[topSrc.length - 1 - p.dataIndex]; if (s) showSourceFocus(s.id); });
toppChart.on('click', p => { const paper = topPapers[topPapers.length - 1 - p.dataIndex]; if (paper) showPaperFocus(paper.bibcode); });

// ---- Search-box handlers ----
function flashError(msg) {
  const el = document.getElementById('q-err');
  el.textContent = msg;
  if (msg) setTimeout(() => { if (el.textContent === msg) el.textContent = ''; }, 3000);
}

document.getElementById('go-src').addEventListener('click', () => {
  const q = document.getElementById('q-src').value.trim();
  if (!q) { flashError('Enter a Gaia source_id'); return; }
  const id = q.replace(/^Gaia\\s*DR3\\s*/i, '').trim();
  if (SRC_BY_ID[id]) { showSourceFocus(id); flashError(''); }
  else flashError('Source ' + q + ' not in catalog');
});
document.getElementById('q-src').addEventListener('keydown', e => { if (e.key === 'Enter') document.getElementById('go-src').click(); });

document.getElementById('go-paper').addEventListener('click', () => {
  const q = document.getElementById('q-paper').value.trim().toLowerCase();
  if (!q) { flashError('Enter arxiv_id / bibcode / title'); return; }
  // Try exact arxiv_id, then exact bibcode, then title substring
  let hit = DATA.papers.find(p => p.arxiv_id && p.arxiv_id.toLowerCase() === q);
  if (!hit) hit = DATA.papers.find(p => p.bibcode.toLowerCase() === q);
  if (!hit) {
    const matches = DATA.papers.filter(p => p.title && p.title.toLowerCase().includes(q));
    if (matches.length === 1) hit = matches[0];
    else if (matches.length > 1) { flashError(matches.length + ' title matches; refine query'); return; }
  }
  if (hit) { showPaperFocus(hit.bibcode); flashError(''); }
  else flashError('No paper matched: ' + q);
});
document.getElementById('q-paper').addEventListener('keydown', e => { if (e.key === 'Enter') document.getElementById('go-paper').click(); });
</script>
</body>
</html>
"""


def render_html(payload: dict, corpus_name: str = "TWA") -> str:
    """Render the overview HTML with embedded data.

    ``corpus_name`` is substituted into <title> and <h1>; defaults to "TWA"
    for backward compat with the original TWA demo.
    """
    # Compact but readable; the file is meant to be opened, not edited.
    json_str = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    html = HTML_TEMPLATE.replace("__DATA_JSON__", json_str)
    # Escape for HTML; also handle empty/None gracefully.
    safe_name = (corpus_name or "Source-Paper").replace("<", "").replace(">", "")
    return html.replace("__CORPUS_NAME__", safe_name)


def build_overview_html(
    output_path: Path | str,
    *,
    catalog_path: Path | str = "TWAxgdr3.csv",
    edges_path: Path | str = "data/twa/paper_edges.parquet",
    name_col: str = NAME_FALLBACK,
    inline_echarts: bool = False,
    echarts_path: Path | str | None = None,
    corpus_name: Optional[str] = None,
    spectra_dir: Path | str | None = None,
) -> Path:
    """Top-level entry: read corpus inputs, write the HTML, return its path.

    Parameters
    ----------
    output_path : where to write the HTML.
    catalog_path : member CSV with Gaia/RAICRS/DEICRS columns.
    edges_path : paper_edges.parquet from build_twa_paper_edges.py or
        build_corpus.py.
    name_col : column to use as a display name (default ``main_id`` from the edges
        table; not present in the bare catalog, in which case labels fall back
        to ``<corpus_name>-<last8>``).
    inline_echarts : if True, read ``echarts.min.js`` and embed it as an inline
        ``<script>`` block instead of a sibling-file reference. Produces a
        single self-contained HTML that can be emailed or chatted as-is, no
        accompanying files needed. Adds ~1 MB to the file size.
    echarts_path : location of the vendored ``echarts.min.js`` to inline.
        Defaults to the copy shipped inside :mod:`astro_pipeline.viz`.
    corpus_name : human-readable name for <title> and <h1>. Defaults to the
        parent directory name of ``output_path`` (so a corpus at
        ``data/orion_onc/source_paper_viz.html`` becomes "orion_onc ...").
    """
    out = Path(output_path)
    catalog = pd.read_csv(catalog_path, dtype={GAIA_ID_COL: str})
    edges = pd.read_parquet(edges_path)

    if corpus_name is None:
        corpus_name = out.parent.name

    # Bring a per-source display name from the edges' main_id, if available.
    if name_col == NAME_FALLBACK and NAME_FALLBACK in edges.columns:
        main_map = edges.drop_duplicates("source_id").set_index("source_id")["main_id"]
        main_map.index = main_map.index.astype(str)
        catalog = catalog.copy()
        catalog[NAME_FALLBACK] = catalog[GAIA_ID_COL].astype(str).map(main_map).fillna("")

    spectra_by_source: dict[str, dict] = {}
    if spectra_dir:
        from astro_pipeline.spectra import read_xp_continuous, read_xp_sampled

        spectrum_root = Path(spectra_dir)
        for gaia_id in catalog[GAIA_ID_COL].astype(str):
            candidates = [
                (
                    spectrum_root / "gaia_xp_continuous" / f"{gaia_id}.fits",
                    read_xp_continuous,
                ),
                (
                    spectrum_root / "gaia_xp_sampled" / f"{gaia_id}.fits",
                    read_xp_sampled,
                ),
                # Backward compatibility: older callers passed the sampled
                # product directory rather than the corpus root.
                (spectrum_root / f"{gaia_id}.fits", read_xp_sampled),
            ]
            for spectrum_path, reader in candidates:
                if not spectrum_path.exists():
                    continue
                try:
                    spectra_by_source[gaia_id] = reader(spectrum_path)
                    break
                except Exception:
                    continue

    payload = compute_overview(
        catalog,
        edges,
        name_col=name_col,
        spectra_by_source=spectra_by_source,
    )
    html = render_html(payload, corpus_name=corpus_name)

    if inline_echarts:
        js_file = (
            Path(echarts_path)
            if echarts_path
            else Path(__file__).parent / "vendor" / "echarts.min.js"
        )
        if not js_file.exists():
            raise FileNotFoundError(f"echarts.min.js not found at {js_file}; cannot inline")
        js = js_file.read_text(encoding="utf-8")
        html = html.replace(
            '<script src="echarts.min.js"></script>',
            f"<script>{js}</script>",
        )

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out
