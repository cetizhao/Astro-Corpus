# MCP integration and usage guide

This guide is for a new collaborator or an AI agent connecting to the pipeline
for the first time. Users normally describe the desired sample in natural
language; the agent chooses and calls the tools below.

## Install and connect

```bash
git clone <repo-url>
cd astro-pipeline
uv sync
uv run pytest -q
```

The root `.mcp.json` is project-relative and is the shared configuration:

```json
{
  "mcpServers": {
    "astro-corpus": {
      "command": "uv",
      "args": ["run", "python", "-m", "astro_pipeline.mcp_server"]
    }
  }
}
```

An MCP client that reads project configuration can clone the repository and
start from its root. Other clients use the equivalent command with the repo as
their working directory:

```text
uv run python -m astro_pipeline.mcp_server
```

The server starts even when the optional TWA demo files are absent. In that
case cached `lookup_source`, `lookup_paper`, and `summarize_corpus` are not
useful yet, but live search and all corpus-building tools remain available.

## Choose the correct input workflow

| User intent | MCP workflow | Meaning |
|---|---|---|
| A coordinate/sky patch | `cone_search` or `download_corpus(cone=...)` | Every Gaia field source in a geometric cone |
| A named sky center | `download_corpus(region=[name, radius])` | SIMBAD resolves the center; still field sources, not members |
| One named object | `resolve_name` or `download_corpus(name=...)` | One SIMBAD-resolved Gaia DR3 source |
| An uploaded source list | `download_corpus(catalog_path=...)` | Gaia IDs or RA/Dec in a local CSV |
| Members of a cluster/association | Search → preview → membership download | Published membership definition selected explicitly |
| A large part or all of `gaia_source` | Bulk plan → bounded worker → status | HEALPix-partitioned Parquet dataset |

The distinction between a region and membership is scientifically important.
For example, `region=["Taurus", 2]` returns foreground/background field stars
as well as Taurus members. It must not be described as a Taurus member list.

## Membership workflow

1. Call `search_membership_catalogs(target="Taurus")`.
2. Show the ranked VizieR candidates to the user.
3. Call `preview_membership_catalog(catalog_id=...)`.
4. Distinguish tables marked `members` from those marked `candidates`; inspect
   membership columns and the Gaia match strategy.
5. After the user/agent selects a table, call
   `download_membership_corpus(label=..., catalog_id=..., table_id=...)`.

Example selection validated during development:

```text
catalog_id = J/AJ/156/271
table_id   = J/AJ/156/271/table1
role       = Members of Taurus
```

The output preserves the selected VizieR table as
`membership_catalog.csv/parquet`. The Gaia-normalized `members.*` table keeps
literature columns with a `member_` prefix and adds
`membership_match_sep_arcsec` and `membership_match_candidate_count`.
Unmatched catalogue rows go to `unmatched_members.csv`; they are not silently
dropped. The default positional radius is 3 arcsec because older catalogues
may use an earlier coordinate epoch. Dense fields and high-proper-motion
sources still require scientific review.

## Corpus products and progress

Both `download_corpus` and `download_membership_corpus` accept:

- `skip_simbad`: save member data only;
- `xp_sampled`: opt in to Gaia XP Sampled FITS;
- `xp_continuous`: opt in to Gaia XP Continuous FITS;
- `enrich`: fetch or skip paper title/DOI/year metadata;
- `sleep_seconds`: SIMBAD pacing.

XP flags default to `false`, so a normal catalogue request never triggers a
large FITS download unexpectedly. The pipeline first queries
`has_xp_sampled/has_xp_continuous`, requests only available products, and saves
each format in a separate resumable directory.

Call `download_plan(...)` for a read-only preflight description. During or
after a build, call `corpus_status(label)`. New builds expose the atomic
`manifest.json` with these stages:

```text
resolve_members → save_members → gaia_xp_* → simbad_papers → visualization
```

`completed` means all requested stages completed; `partial` means the core
corpus exists but an optional product or visualization is incomplete;
`failed` means a required stage failed. Reusing a label for a different input
is refused to prevent mixed corpora.

## Gaia bulk workflow

This route is for materializing `gaiadr3.gaia_source`, not for a normal corpus.

1. `create_gaia_bulk_plan(label=..., healpix_level=5, ...)` writes a plan and
   downloads no Gaia rows.
2. `run_gaia_bulk_download(label=..., max_chunks=1)` processes one chunk by
   default. A positive number is a bound; `0` explicitly means all remaining.
3. `gaia_bulk_status(label)` reads local progress without querying Gaia.

`columns_mode="default"` returns a compact astrometry/photometry/QC/XP-flag
table. `custom` accepts validated column names. `all` selects every column in
the main source table but does not include auxiliary tables, epoch data, XP,
RVS, or other DataLink products.

The official Gaia `source_id` rule makes each chunk a non-overlapping nested
HEALPix range. Rows inside a chunk use keyset pagination:

```text
TOP page_size ... WHERE source_id >= cursor AND source_id < upper
ORDER BY source_id ASC
```

No random sampling or query truncation is used.

## Tool reference

| Tool | Network | Writes data | Purpose |
|---|---|---|---|
| `lookup_source` | no | no | Query the configured cached corpus by Gaia ID |
| `lookup_paper` | no | no | Query by arXiv ID, bibcode, or title |
| `summarize_corpus` | no | no | Summarize the configured cached corpus |
| `resolve_name` | yes | no | SIMBAD name → Gaia DR3 source |
| `cone_search` | yes | no | Small Gaia field-source cone |
| `gaia_xp_spectrum` | no | PNG cache | Render cached XP from default corpus or `label` |
| `download_plan` | no | no | Preview a corpus request and risks |
| `download_corpus` | yes | yes | Build from cone/name/region/uploaded CSV |
| `search_membership_catalogs` | yes | no | Discover VizieR member catalogues |
| `preview_membership_catalog` | yes | no | Inspect tables, columns, and samples |
| `download_membership_corpus` | yes | yes | Build from a selected member table |
| `corpus_status` | no | no | Read manifest/checkpoint progress |
| `list_corpora` | no | no | List local corpus snapshots and XP counts |
| `create_gaia_bulk_plan` | no | plan only | Define `gaia_source` HEALPix partitions |
| `run_gaia_bulk_download` | yes | yes | Process bounded bulk chunks |
| `gaia_bulk_status` | no | no | Read bulk plan progress |

## Known warnings an agent should surface

- Gaia, VizieR, SIMBAD, and arXiv are external services; throttling, 503s, and
  queued TAP jobs can occur. Retry/checkpoint support does not make them local.
- Gaia DataLink login can fail while anonymous TAP queries still work. The
  pipeline deliberately keeps astrometry queries anonymous.
- XP download failure does not invalidate the members/astrometry table. Report
  XP as partial and resume later.
- Membership catalogues can disagree scientifically. The pipeline ranks and
  previews candidates but does not decide which paper is authoritative.
- An HTML file is self-contained only when ECharts was successfully inlined;
  check the manifest warning when the vendored asset is absent.
