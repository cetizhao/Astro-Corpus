# Architecture and data contracts

## System boundary

The production corpus path remains Gaia DR3 + literature metadata. A
release-profile and runtime-discovery layer prepares the transport boundary for
DR4 without claiming support for unpublished schemas or columns. LAMOST and
SDSS adapters are retained but are not the current development focus. The
pipeline is a data preparation and retrieval layer, not a foundation-model
trainer.

```text
Natural-language request
  → MCP tool selection
  → input resolver (name / cone / CSV / membership catalogue)
  → canonical Gaia DR3 member table
  → optional Gaia XP products
  → SIMBAD source-paper edges
  → Parquet / FITS / JSONL / self-contained HTML
```

The MCP server is a thin adapter. Domain logic remains callable from Python so
other agents and future services are not coupled to one MCP client.

## Input semantics

`resolve_members` normalizes four inputs to canonical Gaia fields:

- `cone`: coordinates plus radius;
- `region`: SIMBAD-resolved center plus radius;
- `name`: one SIMBAD-resolved object;
- `catalog`: user CSV with Gaia DR3 IDs or coordinates.

The first two are geometric field selections. Physical membership is a
separate literature workflow in `membership.py`:

```text
VizieR discovery → table preview → explicit table selection
  → Gaia DR3 ID match or batched small-cone coordinate match
  → original table + matched members + unmatched rows
```

The coordinate matcher used by both uploaded catalogues and literature tables
queries a union of small Gaia cones in bounded batches and performs
nearest-neighbour assignment locally. It avoids the old single large-cone
failure mode, where a global row limit could truncate candidates before all
coordinates were represented. It also avoids TAP upload jobs that were
observed to remain queued for minutes.

## Canonical members table

Core Gaia columns use the existing names required by corpus and visualization
code:

| Canonical column | Gaia DR3 source |
|---|---|
| `gaia_release` | release-qualified identity component (`dr3` currently) |
| `Gaia` | `source_id` (stored as string) |
| `RAICRS`, `DEICRS` | `ra`, `dec` |
| `Plx_gdr3`, `e_Plx_gdr3` | parallax and error |
| `pmRA_gdr3`, `pmDE_gdr3` | proper motion |
| `Gmag_gdr3`, `BPmag_gdr3`, `RPmag_gdr3` | mean photometry |
| `RV_gdr3`, `RUWE_gdr3` | radial velocity and RUWE |

Quality fields such as BP/RP excess, astrometric excess noise, visibility
periods, good AL observations, and duplicate flag are preserved without an
automatic quality cut.

For literature membership corpora, every source-table column is namespaced as
`member_<original_name>`. Provenance columns identify catalogue/table, match
method/radius, angular separation, and candidate multiplicity. This keeps the
published sample definition available to downstream reviewers.

## Corpus directory contract

```text
data/<label>/
  manifest.json
  members.csv
  members.parquet
  membership_catalog.csv          # membership workflow only
  membership_catalog.parquet      # membership workflow only
  unmatched_members.csv            # membership workflow only
  catalogs/
    gaia_xp_availability.parquet   # if XP requested
  gaia_xp_sampled/
    <source_id>.fits
    checkpoint.json
  gaia_xp_continuous/
    <source_id>.fits
    checkpoint.json
  simbad_refs.checkpoint.json
  simbad_refs.parquet
  paper_edges.parquet
  source_paper_viz.html
```

`manifest.json` is atomically replaced after every stage/batch. It records the
input identity, requested products, stage status/counts, artifacts, warnings,
errors, and run history. The same label cannot be reused for a different input.
Gaia source identity is the pair `(gaia_release, source_id)`; `members.*` and
the manifest both record the release, so equal numeric IDs from two releases
cannot share a corpus checkpoint.
The selection identity includes `limit`; uploaded catalogues additionally use
a SHA-256 content fingerprint, so replacing a same-named CSV cannot reuse old
checkpoints silently. Product-specific checkpoints remain the detailed resume
source.

`paper_edges.parquet` links a Gaia source to a SIMBAD-curated bibliography. It
is not paper-text substring matching. Core columns are `source_id`, `arxiv_id`,
`bibcode`, `title`, `year`, `journal`, `doi`, and `main_id`.

The SIMBAD checkpoint separately records successfully queried sources,
including sources with zero references, and retryable failures. It never adds
placeholder paper rows to `simbad_refs.parquet`. Reference rows and checkpoint
state are atomically replaced after each bounded source batch.

## XP products and visualization

Gaia availability is queried anonymously from `gaiadr3.gaia_source`. DataLink
downloads are opt-in and can use configured credentials. Sampled and Continuous
products have separate directories/checkpoints. The builder treats DataLink
failure as an optional-product warning, leaving members and paper data usable.

The HTML embeds member data, paper edges, and cached spectra. It prefers
Continuous and falls back to Sampled. `gaiaxpy` reconstructs Continuous
coefficients on a wavelength grid for display. ECharts is inlined when the
package asset `viz/vendor/echarts.min.js` exists; otherwise the manifest records
that the HTML requires a sibling script.

## Release profiles and Archive discovery

`gaia_releases.py` contains stable DR3 facts and deliberately provisional DR4
expectations. DR4 has no static TAP schema in the profile. `gaia_archive.py`
queries `TAP_SCHEMA.schemas`, `TAP_SCHEMA.tables`, and `TAP_SCHEMA.columns` at
runtime; discovered metadata takes precedence over local expectations.

DataLink requests use an explicit contract:

```text
(release, retrieval_type, identifiers, linking_parameter,
 data_structure, output_format, valid_data)
```

`gaia_products.py` persists that contract, a source checkpoint, artifact
metadata/checksums, Archive table copies, and normalized per-identifier files.
Astroquery returns parsed Astropy tables, so `archive_tables/` is correctly
described as re-serialized table content rather than byte-identical HTTP
payload. Binary image/blob transport remains a separate adapter; in particular
`CROWDED_FIELD_IMAGE` is refused until the public DR4 service can be tested.

## Gaia main-table bulk dataset

Gaia DR3 `source_id` stores the level-12 nested HEALPix in its most significant
bits. At HEALPix level `L`:

```text
divisor = 2^35 × 4^(12-L)
pixel   = floor(source_id / divisor)
```

Thus pixel `p` is exactly the half-open source-ID interval
`[p × divisor, (p+1) × divisor)`. `bulk_gaia.py` creates immutable plans and
downloads each interval with ordered keyset pages. Every page is atomically
written as Parquet, then its checkpoint advances. A failed page resumes from
the last committed source ID. Global progress increments only after a whole
HEALPix chunk completes.

This design can eventually materialize all rows and columns of
`gaiadr3.gaia_source`, subject to storage/time/service policy. It explicitly
does not include Gaia auxiliary tables or DataLink products. Those require
additional product-specific planners.

The bulk plan schema is now release/table-aware, but only DR3 currently has a
confirmed `source_id_healpix12` partition strategy. Supplying an assumed DR4
schema does not bypass this safety boundary.

## Safety boundaries

- MCP never exposes arbitrary ADQL; `execute_adql` is an internal read-only
  primitive used by validated planners.
- IDs, column names, table names, labels, ranges, page sizes, and HEALPix levels
  are validated before query construction.
- Bulk planning downloads no rows; running defaults to one chunk.
- Outputs are atomic where they represent progress state or a complete page.
- Credentials are loaded from gitignored local configuration and never placed
  in manifests.
- Network retries use bounded exponential backoff for transport failures and
  HTTP 408/429/5xx from both `httpx` and astroquery's `requests` stack;
  permanent authentication/query 4xx errors are not retried blindly.
