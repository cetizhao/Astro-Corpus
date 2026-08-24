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
- Gaia XP Sampled and Continuous downloads are opt-in, availability-filtered,
  checkpointed, and non-fatal to the astrometry/paper corpus.
- Gaia source identity is `(release, source_id)`, never `source_id` alone.
  DR3 and DR4 source lists must be treated as independent.
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

- `data/`, `*.fits`, `*.parquet`, `*.csv`, and `*.jsonl` are gitignored.
- Personal Gaia credentials belong only in gitignored `config/local.yaml` or
  `GAIA_USERNAME` / `GAIA_PASSWORD`; never print or commit them.
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
- Gaia DR4 preparation and known/unknown contracts: `docs/dr4-readiness.md`
- Smoke tests, progress, resume, and troubleshooting: `docs/operator-runbook.md`
- Public project overview: `README.md`
- Release history: `CHANGELOG.md`
