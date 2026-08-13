# Agent working notes

This repository is a Gaia-first astronomical corpus pipeline. The stable
agent interface is the MCP server configured by the root `.mcp.json`.

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
- `create_gaia_bulk_plan` and `run_gaia_bulk_download` cover only
  `gaiadr3.gaia_source`. They do not mean all Gaia auxiliary/DataLink products.
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
- Smoke tests, progress, resume, and troubleshooting: `docs/operator-runbook.md`
- Public project overview: `README.md`
- Release history: `CHANGELOG.md`
