# Changelog

All notable changes to this project are documented here. Versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Paper evidence prototype

- Extract overlapping source-alias mentions from supplied article HTML: title,
  abstract, paragraphs, figure captions, table rows, and provenance-labelled
  VizieR input rows. Preserve original text and explicit material coverage.
- Save content-keyed analyses in the existing fenced SQLite cache; add
  `analyze_paper_evidence` and `get_paper_evidence` MCP tools (27 tools total).
- This first version requires caller-supplied HTML and verified aliases; it does
  not download full text, parse PDF, OCR figures, or infer scientific identity.

### Added

- Explicit, bounded arXiv full-text collections with version-pinned originals,
  structured JSON, deterministic Markdown, per-input provenance, resumable
  downloads and conservative HTML/LaTeX/PDF quality reporting. New MCP tools
  `download_paper_corpus` and `paper_corpus_status`, library CLI build/status
  commands, and a reproducible 20–50-paper pilot selector.
- Additive shared-library schema v2 for paper downloads, text versions and
  artifact pins, retaining read-only v1 compatibility and existing Gaia data.

- A loopback-only, read-only whole-library HTML viewer with revision-aware
  pagination, complete Hammer–Aitoff sky positions, source provenance,
  bibliography, live refresh, and on-demand verified Gaia spectra.
- Exact release/product spectrum adapters and separate global-current versus
  corpus-pinned views; provisional DR4 spectra remain explicitly unsupported.
- A bounded CrA/Upper Sco published-member-table collection runner with
  per-attempt reports, matched/unmatched row accounting, XP verification, and
  run-bound literature-enrichment completion records. Both fixed full tables
  passed local delivery checks on 2026-09-29, including all availability-positive
  XP products and literature stages; counts and scientific matching caveats are
  recorded in the operator runbook.
- Explicit membership-selection resume from a verified published run, exposed
  through Python, MCP, and the fixed collection CLI; preserves frozen input and
  match provenance without repeating VizieR/Gaia positional queries.
- Bounded DOI batches through the official arXiv API with exact DOI mapping,
  complete-feed checks, and auditable exact-title disambiguation; unresolved
  ambiguities remain retryable rather than becoming negative cache entries.
- Browser regression checks for full pagination, cross-release identity,
  automatic refresh, spectrum scaling, mobile layouts, and untrusted text.
- A default-on, release-qualified local library with a SQLite global index,
  immutable content-addressed objects, per-build run snapshots, and pinned Gaia
  product-reference documents.
- Shared source-reference, paper-metadata, DOI-to-arXiv, exact Gaia-ID, and XP
  availability caches with configurable positive/negative TTLs and explicit
  retryable failure history.
- Global MCP tools `lookup_library_source`, `lookup_library_paper`, and
  `library_status`; together with paper collection tools the server exposes 27 tools.
- `scripts/library.py` status, dry-run/non-destructive legacy import,
  verification/reconciliation, reference export, and standalone export
  workflows.
- Input-row, match, duplicate-member, original-CSV, run, artifact, and product
  provenance retained independently from globally deduplicated source/product
  identities.

### Changed

- README is now a concise project entry point; TWA bootstrap, direct queries and
  export details live in topic guides. Architecture diagrams have editable Mermaid
  sources, locally rendered SVGs, and an offline synchronization check.
- README opens with an illustrated system overview showing data sources, two
  independent corpus builds, shared local storage, snapshots and query/view
  access; its generation prompts are included for future design revisions.
  The three architecture diagrams share a light palette, simpler connections
  and explicit workflow groups.
- Static corpus HTML can embed spectra, show cached-product status only, or be
  disabled; full region collection uses the lightweight status-only mode.
- Bibliography enrichment checkpoints basic metadata before optional arXiv
  lookups. Requests are spaced and repeated HTTP 429 defers uncached DOI work
  without recording false negatives or declaring the build complete.
- High-level corpus and membership builds use the shared library by default,
  while preserving label-root Parquet/checkpoint/HTML compatibility views and a
  configurable legacy path through `library.enabled: false`.
- Corpus readers select immutable published snapshots through `active_run_id`;
  run-selected exports use `snapshot_run_id`, and version-2 product references
  pin exact immutable product versions.
- SIMBAD source-reference snapshots, bibcode metadata, and DOI-to-arXiv results
  are reused across labels. Successful refreshes replace current state (including
  removed edges) without deleting history; failures leave the last successful
  current pointer intact.

### Safety

- Shared work uses bounded per-key leases with fencing tokens and keeps remote
  work outside SQLite write transactions. Read-only global queries do not create
  or migrate an absent database.
- Legacy imports are dry-run by default and explicit apply never modifies the
  source tree. Verification reports corrupt/orphan/staging state but performs no
  automatic deletion or garbage collection.
- Reference exports remain explicitly library-dependent; standalone exports
  verify and copy pinned bytes. Live SQLite backup uses the SQLite backup API
  rather than a direct database-file copy.

### Known limitations

- Pending publication recovery is diagnostic-only: verification reports it but
  does not activate a prepared run. Reconciliation only restores missing views
  for already-published runs; divergent user files are never overwritten.
- Legacy records with unknown selection/release or unverifiable product identity
  remain explicitly unverified or inventory-only. Historical source trees are
  not automatically migrated or pruned.

### Fixed

- Paper artifact publication closes its temporary writer before the blob store
  reads it, avoiding Windows file-sharing errors during full-text collection builds.
- Atomic files and shared objects sync through writable descriptors, as required
  by Windows, before replacing the destination.
- Offline fixtures handle Windows read-only files, UTF-8 text and SQLite restore
  locks; symlink-dependent cases explicitly skip when the account lacks permission.
- Library verification normalizes native object-path separators before reporting
  orphan files, so registered Windows objects are not misclassified as residue.
- VizieR discovery ignores invalid resource IDs; coordinate matching uses
  numeric columns and explicit units, preserves old-release identifiers as
  evidence, and does not confuse J2000 equinox with an observation epoch.
- Viewer current-run selection follows manifest pins, including revision
  changes caused by repinning without a SQLite write. Recent-run cards are sorted
  newest first. Library disappearance and failed detail/spectrum refreshes clear
  stale selections and plots, including late asynchronous responses; malformed
  request URLs return a structured error. Failed/unpublished historical attempts
  no longer imply active work in source badges or display as ongoing downloads.
  Failed collection reports no longer borrow the previous published run's inventory.
- DataLink requests use an isolated finite-timeout client without changing the
  global socket timeout. Automatic import-time server announcements are
  suppressed, and default XP batches are limited to 50 Sampled / 5 Continuous
  sources. Local capability hints avoid astroquery's networked singleton import
  and report unreadable declarations as unknown.
- Failed builds preserve the prior published corpus; immutable run metadata and
  SQLite preparation finish before activation. Interrupted view/index updates
  remain explicitly pending and recoverable.
- Product-view ownership checks and atomic replacements share a fenced
  transaction, and batch renewal no longer creates one thread per identifier.
- Gaia small catalogue, source-ID and position-matching queries use bounded
  synchronous TAP instead of waiting in the asynchronous job queue.
- Gaia TAP now honors environment proxies and enforces both per-request and
  total query deadlines, including retries and polling. Bulk remains async-only;
  job cleanup has a separate bounded, non-fatal allowance.
- TAP `ERROR` and `OVERFLOW` responses cannot become empty or silently truncated
  catalogues. Async phase/result retries reuse the existing job rather than
  submitting duplicates.
- Catalogue and metadata startup no longer imports astroquery's networked Gaia
  singleton; server-announcement requests cannot bypass the TAP query deadline.
- Metadata discovery defaults to a 30-second total query budget instead of a
  10-second request timeout that could reject otherwise available services.

## [0.5.0] — 2026-08-24

### Added

- Stable DR3 and provisional DR4 release profiles with release-qualified source
  identity, reference epochs, expected source tables, and DataLink products.
- Runtime TAP discovery through `TAP_SCHEMA` without guessing a DR4 schema.
- Generic release-aware DataLink request/artifact contracts, bounded explicit
  downloads, checksums, source checkpoints, and artifact provenance.
- SOCKS-aware HTTP discovery so Gaia metadata probes respect local proxy
  environments instead of hanging in astroquery's unbounded connection path.
- Declared pytest as a development dependency so a clean `uv sync` can run the
  documented verification command.
- MCP tools `probe_gaia_archive`, `describe_gaia_table`,
  `list_gaia_products`, `plan_gaia_product_download`, and
  `download_gaia_product`, plus local `gaia_product_status` inspection.
- Portable project-scoped Codex MCP configuration in `.codex/config.toml`,
  alongside `.mcp.json` for other compatible clients.

### Changed

- Corpus manifests and member tables record `gaia_release`; a label cannot be
  reused across releases even when numeric source IDs are equal.
- Existing DR3 XP downloads now use the generic DataLink layer while retaining
  `<source_id>.fits`, checkpoints, and visualization compatibility.
- Gaia bulk plans now record release, archive tag, schema, table, and partition
  strategy.

### Safety

- DR4 TAP schema and final columns remain discoverable rather than hardcoded.
- DR4 HEALPix bulk execution is disabled until its source-ID partition rule is
  officially confirmed.
- Client-declared DataLink products are reported separately from products
  confirmed by a live source-specific Archive response.
- Binary `CROWDED_FIELD_IMAGE` downloads are refused until a blob-preserving
  transport can be validated against the public DR4 service.

## [0.4.0] — 2026-08-13

### Summary

Generalizes corpus acquisition beyond the original TWA demo. Agents can now
distinguish geometric field searches from published physical membership,
inspect and select VizieR member tables, build resumable Gaia+paper corpora with
optional XP products, inspect exact progress, and materialize the Gaia DR3 main
source table through bounded HEALPix partitions.

### Added

- `search_membership_catalogs`, `preview_membership_catalog`, and
  `download_membership_corpus` MCP tools.
- VizieR membership provenance: unchanged original table, `member_` namespaced
  fields, Gaia DR3 match separations/candidate counts, and explicit unmatched
  rows. No hidden membership or quality filtering.
- Corpus-level Gaia XP Sampled/Continuous switches, availability tables,
  retrieval-specific checkpoints, and visualization loading from any corpus.
- Atomic `data/<label>/manifest.json`, read-only `download_plan`, enhanced
  `corpus_status`, and local corpus XP counts.
- Source-level SIMBAD checkpoints, including explicit completion for sources
  with zero references and retry state for transient failures; no placeholder
  paper rows are introduced.
- `bulk_gaia.py` plus MCP/CLI plan-run-status interfaces for all rows/selected
  columns in `gaiadr3.gaia_source`, using official source-ID HEALPix intervals,
  ordered keyset pages, atomic Parquet parts, and per-pixel resume state.
- `AGENTS.md` and integration, architecture, and operator documentation.

### Changed

- `region` and `cone` are documented and surfaced as geometric field-source
  searches, never as physical cluster/association membership.
- Position-only membership matching uses bounded unions of small ordinary ADQL
  cones plus local nearest-neighbour assignment. This avoids global-cone row
  truncation and observed TAP-upload queue delays.
- Gaia TAP catalogue queries remain anonymous; only DataLink attempts login.
- Corpus/XP visualization prefers Continuous products, falls back to Sampled,
  and can target a non-default corpus by label.
- Corpus and bulk labels are validated directory names and cannot escape their
  output roots.
- Corpus source-selection identity includes `limit`; uploaded CSV inputs also
  include a SHA-256 fingerprint to prevent stale checkpoint reuse.
- Generated corpus HTML inlines the package-vendored ECharts runtime and can be
  opened or shared as one offline file.

### Fixed

- Gaia source-ID queries now emit valid ADQL `TOP n` instead of unsupported
  trailing `LIMIT n`, and reject nonnumeric IDs.
- Gaia `ROW_LIMIT` is restored with `try/finally` after catalogue/XP queries.
- Bulk retry accounting includes pages committed before a later page failure
  exactly once.
- Bulk keyset pagination continues until an empty page rather than trusting a
  short TAP page as end-of-range.
- A DataLink login failure can no longer break otherwise valid anonymous TAP
  astrometry work.
- Exponential retry now covers astroquery's `requests` transport errors and
  HTTP 408/429/5xx responses, while permanent 4xx errors fail immediately.

### Verified

- Taurus member workflow: VizieR `J/AJ/156/271/table1`, first three rows,
  produced two Gaia DR3 matches and one explicit unmatched row on 2026-08-13.
- Gaia bulk workflow: the level-12 pixel containing TW Hya downloaded three
  `gaia_source` rows to one Parquet part and resumed idempotently.
- The full default offline test suite passes; network tests remain opt-in.

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
