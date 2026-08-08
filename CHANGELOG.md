# Changelog

All notable changes to this project are documented here. Versions follow
[Semantic Versioning](https://semver.org/).

## [0.3.0] — 2026-08-05

### Summary
Adds reproducible Gaia DR3 XP Sampled and XP Continuous acquisition to the TWA
corpus, exposes the cached spectra in the standalone HTML visualization, and
exports the XP products and quality metadata in the JSONL corpus.

### Added
- `scripts/download_twa_xp.py` downloads all available TWA XP Sampled products
  and checkpoints each completed source.
- `scripts/download_twa_xp_continuous.py` downloads XP Continuous products in
  small, resumable batches (five sources by default).
- XP Sampled parsing includes wavelengths, fluxes, and flux uncertainties;
  XP Continuous parsing includes BP/RP coefficients, coefficient errors, and
  18 quality fields.
- The source focus view in the ECharts HTML renders a BP/RP spectrum with an
  uncertainty band when a cached XP Sampled product is available.
- Tests cover DataLink batching, Sampled/Continuous parsing, JSONL export, and
  XP visualization rendering.

### Changed
- Gaia DataLink downloads now support both `XP_SAMPLED` and `XP_CONTINUOUS`,
  save products as `<source_id>.fits` in retrieval-specific directories, and
  persist progress after every batch.
- The JSONL `multimodal.gaia_xp` record now reports availability, cache state,
  DataLink resolver URLs, local paths, Sampled point counts, and Continuous
  coefficients, errors, and quality metadata.
- TWA column aliases now populate parallax, RUWE, G magnitude, and proper
  motions from the source catalog.

### Fixed
- Continuous downloads no longer submit an oversized request before applying
  the configured batch limit.
- Checkpoints are written throughout a download instead of only after the full
  source list completes.

## [0.2.1] — 2026-06-28

### Summary
Two follow-ups to v0.2 narrowing the corpus to **Gaia + arXiv** (per the new
scope decided with the advisor): the previously-broken `corpus paper
<arxiv_id>` lookup now works end-to-end (76% arXiv-id coverage), and the
visualization is regenerated from real corpus data instead of a stale
hand-coded mock.

### Added
- **`astro_pipeline.viz`** — interactive HTML overview of the source↔paper
  corpus. Six ECharts panels: sky distribution (color = log paper count),
  top-15 sources by paper count, top-15 papers by source coverage, arXiv
  coverage donut, paper year distribution, and a bipartite network subset.
  Two focus modes: search boxes (`Gaia source_id`, `arxiv_id|bibcode|title`)
  and click-through on every chart. Output is offline-friendly (ECharts
  vendored locally, data embedded as JSON).
- **`scripts/build_viz.py`** — CLI to regenerate `data/twa/source_paper_viz.html`
  from the corpus parquets. Backs up the previous HTML once.
- **`scripts/resolve_arxiv_ids.py`** — batched arXiv DOI-API resolver that
  backfills the `arxiv_id` column in `paper_edges.parquet`. Uses arXiv's
  boolean-OR query (`doi:X OR doi:Y ...`) at batch-size 20 to resolve
  ~3000 papers in ~11 minutes vs. ~2.6 h serial. Resumable via per-batch
  checkpointing; idempotent.

### Fixed
- **`corpus paper <arxiv_id>` was dead.** v0.2 shipped `paper_edges.parquet`
  with `arxiv_id` 100% empty (the `--resolve-arxiv` step of the build script
  had never been run), so the arxiv-id lookup path silently returned "No
  paper matched". After running `resolve_arxiv_ids.py`, coverage is now
  **76.2%** (2543 / 3336 unique papers; 5185 / 6505 edges). The remaining
  24% are journal-only papers with no arXiv preprint — they still resolve
  via bibcode / DOI / title.
- **`source_paper_viz.html` was a hardcoded lie.** The old file was a
  hand-tuned mock titled "49 TW Hya members × 94 arXiv papers · 2,494
  edges" with hardcoded chart data — none of it reflected the real corpus
  (67 sources × 3336 papers · 6505 edges). Replaced with a generator
  (`astro_pipeline.viz.build_overview_html`) driven by `paper_edges.parquet`
  and the member catalog.

### Removed
- `data/twa/vis_stats.json` — stale snapshot from the v0.1 substring-matching
  era; no code referenced it.
- `data/twa/source_paper_viz.html.bak` — backup of the dead mock, kept only
  until the new generator proved stable.

### Changed
- `data/twa/paper_edges.parquet` — `arxiv_id` column now populated for 76%
  of bibcodes (was 0%). Backup at `paper_edges.parquet.bak`.
- `data/twa/simbad_refs.parquet` — `arxiv_id` column kept in sync by the
  same resolver. Backup at `simbad_refs.parquet.bak`.

## [0.2.0] — 2026-06-17

### Summary
Delivers the project's core deliverable: a **bidirectional source ↔ paper
corpus query API**. Input a TWA source → get its papers + multimodal data
(Gaia / LAMOST / SDSS); input a paper → get the sources it studies + their
multimodal data. Paper linking is now **authoritative** (SIMBAD-curated
bibliography) instead of substring-matched, and the multimodal availability
flags are now **truthful**.

### Added
- **`astro_pipeline.corpus`** — bidirectional query API + CLI.
  - `lookup_source(gaia_id)` → astrometry + per-survey multimodal availability
    (Gaia XP / LAMOST / SDSS) + the SIMBAD reference list, each with
    arxiv_id/bibcode/title/year/url.
  - `lookup_paper(arxiv_id|bibcode|title)` → paper metadata + the list of
    catalog sources it studies, each with its own multimodal data.
  - Live-first with cached fallback; a single survey failing degrades
    gracefully (`status: "unavailable"`/`"not_in_catalog"`) and never breaks
    the result. `--offline` uses cache only.
  - CLI: `python -m astro_pipeline.corpus {source|paper|summary} ...`.
- **`astro_pipeline.sources.simbad_refs`** — SIMBAD-grounded paper linking.
  - `build_paper_edges()` resolves each source via SIMBAD, reads its curated
    reference list, and converts bibcodes → arXiv ids (embedded parse first,
    arXiv DOI-API fallback with rate-limit backoff). Result cached to
    `data/twa/simbad_refs.parquet` (incremental, resumable).
  - `bibcode_to_arxiv()` — pure offline bibcode→arXiv-id parser.
  - `enrich_bibcode()` — fetches title/DOI/year/journal from SIMBAD.
- `models.paper.add_paper_columns()` — attaches `arxiv_ids`, `bibcodes`,
  `num_papers` to a catalog (journal-only papers kept in `bibcodes`).
- `scripts/build_twa_paper_edges.py` — regenerates the authoritative edge
  table. Two-phase & resumable; `--resolve-arxiv` for optional arXiv-id fill.
- Tests (offline, no live network):
  - `tests/test_exporter_idcol.py` — multimodal ID-column regression.
  - `tests/test_bibcode_to_arxiv.py` — bibcode parsing.
  - `tests/test_corpus_offline.py` — bidirectional query, cache fallback,
    HTML-corruption guard, URL precedence.
  - `tests/test_paper_edges.py` — structural + real-data over-match guard.

### Fixed
- **Multimodal flags all-False bug.** `io/exporter._find_id_col` guessed the
  id column from `[source_id, obsid, specobjid]`, picking LAMOST's `obsid`
  (8-digit) and comparing it to the catalog's 19-digit Gaia id — so every
  `has_*` flag was False. Replaced with `find_gaia_id_col()` + explicit
  per-survey Gaia columns (`source_id` / `gaiadr3`) on `build_knowledge_table`.
- **Paper over-matching (94 papers × all 49 sources).** The substring matcher
  matched a source to a paper when a Gaia id (never in abstracts) or a
  truncated coordinate appeared as a substring — producing garbage. Replaced
  entirely with SIMBAD bibliography linking.
- **SDSS `ParameterQuery._build_sql` literal `"{limit}"`** — `TOP {limit}`
  was never interpolated. Now `TOP <n>` is emitted correctly.
- **SDSS HTML-error-page corruption.** `query_catalog` now detects parsed
  HTML responses (single `<html>` column) and raises instead of silently
  poisoning the cache; `_validate_sdss_result` rejects empty-column tables.
  The corrupt `data/twa/catalogs/twa_sdss.parquet` was removed.
- `examples/twa_sample.py` now uses the SIMBAD linker (was hardcoded demo
  links) and passes the correct per-survey id columns, so the generated
  knowledge table has truthful flags and real paper links.

### Changed
- `paper_edges.parquet` schema now includes
  `source_id, arxiv_id, bibcode, title, year, journal, doi, main_id`.
  The old `paper_edges_refined.parquet` (v0.1 substring artifact) is retained
  for reference but no longer used.

## [0.1.0] — 2026-06-05
- Initial project setup: Gaia DR3 / LAMOST DR11 / SDSS DR17 catalog sources,
  cone & source-id queries, cross-match, knowledge-table exporter, TWA sample
  pipeline, and a live benchmark script.
