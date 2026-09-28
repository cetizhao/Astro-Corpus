# Agent working notes

This repository is a Gaia-first astronomical corpus pipeline. The stable
agent interface is the MCP server configured by `.codex/config.toml` for Codex
and the root `.mcp.json` for compatible clients such as Claude Code.

## Non-negotiable semantics

- `cone` and `region` are geometric Gaia field-source searches. A name such as
  `Taurus` in `region` does not imply physical membership.
- For cluster or association members use, in order:
  `search_membership_catalogs` → `preview_membership_catalog` →
  `download_membership_corpus`.
- A selected VizieR table is preserved unchanged. Membership probabilities,
  populations, flags, and candidate status are never silently filtered.
- High-level corpus and membership builds use the shared library by default.
  `library.enabled: false` is the explicit legacy-mode switch;
  `ASTRO_PIPELINE_LIBRARY_DIR` overrides `library.root`.
- Global source/product deduplication must not erase input provenance. Preserve
  every original row, match outcome, ordinal, unmatched row, and duplicate
  same-star sample occurrence in its run snapshot.
- The library is not a complete local sky index. Cone, region, and coordinate
  selections still query Gaia even when individual sources are cached.
- Gaia XP Sampled and Continuous downloads are opt-in, availability-filtered,
  checkpointed, and non-fatal to the astrometry/paper corpus.
- Gaia source identity is `(release, source_id)`, never `source_id` alone.
  DR3 and DR4 source lists must be treated as independent.
- Fresh positive cache observations default to 30 days and explicit negatives
  to one day. Failures stay retryable and must not replace the last successful
  pointer. `refresh=True` may replace current observations/edges but must retain
  historical snapshots.
- Per-key leases and fencing guard reusable work. Keep network/file preparation
  outside SQLite write transactions, honor bounded waits, and hold the final
  metadata replacement inside its fenced transaction. A stale owner must never
  overwrite a successor's product or corpus view.
- `active_run_id` selects the run published at the corpus root, but does not
  prove that every requested stage completed; inspect terminal status and errors.
  `snapshot_run_id` pins a run or export. Version-2 `product_refs.json` pins
  exact immutable product versions and is authoritative rather than a hint.
- A DR4 profile expectation or astroquery-declared product is not proof that
  the live Archive serves it. Use `probe_gaia_archive`, `describe_gaia_table`,
  and source-specific `list_gaia_products` before planning live work.
- `create_gaia_bulk_plan` and `run_gaia_bulk_download` default to
  `gaiadr3.gaia_source`. They do not mean all Gaia auxiliary/DataLink products.
  DR4 HEALPix bulk plans are disabled until its source-ID partition rule is
  confirmed by the published data model.
  The worker defaults to one HEALPix chunk; `max_chunks=0` explicitly means all
  remaining chunks.
- Corpus and bulk labels are directory names, not paths. Only letters, digits,
  dot, underscore, and hyphen are accepted.

## Local data and credentials

- `data/`, `*.fits`, `*.parquet`, `*.csv`, `*.jsonl`, library SQLite/WAL files,
  and content-addressed objects are generated data and stay out of Git.
- Personal Gaia credentials belong only in gitignored `config/local.yaml` or
  `GAIA_USERNAME` / `GAIA_PASSWORD`; never print or commit them.
- The shared library defaults to `data/_library/index.sqlite3` plus
  `data/_library/objects/`; corpus snapshots live in
  `data/<label>/runs/<run_id>/` with active compatibility views at the label root.
- Read-only global queries must use `get_library(create=False)` and must not
  create/migrate an absent database. There is no automatic deletion or garbage
  collection; verification reports residue but does not remove it.
- Legacy import is dry-run by default. Explicit apply must remain
  non-destructive to the source tree. Contracts/checksums alone do not prove
  product identity: verify normalized/archive table identifiers and reject
  conflicting release evidence before shared-cache promotion. Unknown selection
  provenance stays unverified. Managed exports must select an immutable run,
  never a mixture of top-level views. Back up live SQLite through
  `LibraryStore.backup()`, not by copying `index.sqlite3`.
- New corpus state lives in `data/<label>/manifest.json`. Bulk state lives in
  `data/gaia_bulk/<label>/bulk_plan.json` plus per-pixel checkpoints.
- Generic DataLink state lives in `data/gaia_products/<label>/`; parsed Archive
  tables are marked as re-serialized and are not claimed to be byte-identical
  HTTP responses. Inspect it with `gaia_product_status`.

## Verification

Run the offline suite before committing:

```bash
uv run pytest -q
```

Network tests are opt-in with `uv run pytest -m network`. The key live smoke
tests used for this release are documented in `docs/operator-runbook.md`.

## Documentation routing

- Human/agent setup and tool workflow: `docs/integration-guide.md`
- Data flow, schemas, and design boundaries: `docs/architecture.md`
- Shared-library config, cache/run semantics, CLI, export, and backup: `docs/library.md`
- Gaia DR4 preparation and known/unknown contracts: `docs/dr4-readiness.md`
- Smoke tests, progress, resume, and troubleshooting: `docs/operator-runbook.md`
- Public project overview: `README.md`
- Release history: `CHANGELOG.md`
