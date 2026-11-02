# Shared library operations and semantics

The shared library is the local, cross-corpus storage and index layer for
reusable Gaia sources, literature observations, and Gaia products. A corpus
under `data/<label>/` remains a user-facing snapshot; the library lets several
snapshots reuse the same observations and bytes without erasing each snapshot's
input provenance.

## Enablement and location

High-level corpus and membership builders use the library by default because
`LibraryConfig.enabled` defaults to `true`. Resolution order is:

1. `ASTRO_PIPELINE_LIBRARY_DIR`;
2. `library.root` in the merged pipeline configuration;
3. `<output.base_dir>/_library`, normally `data/_library`.

`library.enabled: false` disables the library even when the environment variable
is set and restores the legacy label-local build path.

Personal overrides belong in gitignored `config/local.yaml`:

```yaml
library:
  enabled: true
  root: /absolute/path/to/astro-library
  busy_timeout_ms: 5000
  lease_seconds: 120
  wait_timeout_seconds: 30
  positive_ttl_seconds: 2592000  # 30 days
  negative_ttl_seconds: 86400    # 1 day
```

For a temporary location override:

```bash
export ASTRO_PIPELINE_LIBRARY_DIR=/absolute/path/to/astro-library
```

The default layout is:

```text
data/_library/
  index.sqlite3
  objects/
    sha256/
      <first-two-hex>/<sha256>.<extension>
  staging/
```

`index.sqlite3` is the global index. Product and managed import bytes are
immutable, content-addressed objects. `staging/` contains temporary publication
files and is not a second authoritative store.

## Identity, provenance, and deduplication

A Gaia source is identified by `(release, source_id)`, with `release` stored in
the `gaia_release` field; the numeric ID alone is never an identity. A reusable
Gaia product is identified by its complete release-aware request contract plus
identifier; object bytes are independently deduplicated by SHA-256.

Deduplication does not collapse scientific sample provenance:

- every input CSV row is retained in the run's input tables;
- coordinate matches and unmatched/invalid rows remain auditable;
- duplicate rows that resolve to the same star remain separate sample-member
  rows;
- the canonical source table stores that star once per Gaia release;
- membership-catalogue columns and selection state remain attached to the run.

For uploaded CSVs, the original bytes are copied into the run snapshot under
`runs/<run_id>/inputs/`. Reusable source or product records therefore do not
replace the evidence for why a source appeared in a particular corpus.

## Corpus runs and published snapshots

Each library-backed build creates a run directory:

```text
data/<label>/
  manifest.json                  # current corpus view
  runs/
    <run_id>/                    # immutable run snapshot after publication
      manifest.json
      members.parquet
      input_rows.parquet
      input_matches.parquet
      product_refs.json
      ...
  members.csv                    # compatibility view of the active run
  members.parquet
  paper_edges.parquet
  product_refs.json
  ...
```

A run becomes eligible for publication only after its member snapshot is indexed
and its pipeline status is `completed` or `partial`. A terminal `failed` run
never replaces the prior active snapshot. Optional-product or visualization
failures can leave an explicitly partial but usable snapshot; inspect its stages
and warnings rather than treating publication as proof that every product exists.

Manifest fields have distinct meanings:

- `active_run_id` identifies the run selected by the label-root manifest.
  Readers resolve data through `runs/<active_run_id>/` rather than combining
  mutable top-level compatibility files. The run snapshot and SQLite preparation
  complete before activation. If the final view/index publication is interrupted,
  its `view_pending` metadata remains visible for reconciliation; the previous
  active snapshot is preserved for failures before activation.
- `snapshot_run_id` means the supplied manifest is already pinned to one run,
  as in a run directory or run-selected export; readers must not redirect it to
  a newer run.
- `current_run_id` identifies the current build attempt and is not a publication
  pointer.

Product reference documents pin exact immutable `version_id` values and blob
checksums. A version-2 `product_refs.json` is authoritative: readers do not
silently substitute the library's newest version or a legacy file when the
pinned product is missing, failed, or corrupt.

## Cache policy and refresh

Fresh cache observations are reused across labels. Default policy is:

| Observation | Default TTL |
|---|---:|
| Successful positive result | 30 days |
| Explicit negative result | 1 day |

Explicit negatives include a successfully observed missing Gaia ID, no XP
product, zero SIMBAD references, unresolved paper metadata, or no arXiv match
for a DOI. A transport/service failure is different: it remains retryable and
does not replace the last successful current pointer.

Literature state is separated into three caches:

- source-to-bibcode snapshots;
- per-bibcode metadata;
- DOI-to-arXiv resolution.

This separation avoids repeating known no-arXiv lookups and prevents a build
that skips DOI resolution from recording a false `not_found` result.

`refresh=True` bypasses otherwise-fresh observations for the requested build. A
successful refresh publishes replacement current observations and product
versions while retaining historical snapshots. If a refreshed source now has
zero references, its current source-paper edges disappear; a failed refresh
leaves the previous successful cache pointer intact and remains retryable.
Label-local Parquet/checkpoint files are compatibility projections, not cache
authority, so they cannot override TTL or refresh policy.

The refresh switch is available on the Python builders, as `--refresh` on
`scripts/build_corpus.py`, and as `refresh=true` on the MCP corpus, membership,
and generic Gaia-product download tools:

```python
from astro_pipeline.corpus_builder import build_corpus_snapshot
from astro_pipeline.membership import build_membership_corpus

build_corpus_snapshot(..., refresh=True)
build_membership_corpus(..., refresh=True)
```

A cache hit for known IDs is not a local sky index. Cone, named-region, and
coordinate-match selections still query Gaia because the library cannot prove
complete sky coverage from previously cached sources.

For downstream-only recovery, membership builds may explicitly supply
`resume_selection_run_id` for a published run of the same corpus. This is frozen
selection reuse, not a new spatial query: preflight verifies the contract,
publication record, file checksums, table schema/hash, and row-level match
provenance before creating a new run. The new run retains the frozen astrometry,
unmatched rows, and duplicate sample occurrences; it never rewrites the original
snapshot. It requires shared-library mode and cannot be combined with refresh.
The fixed region runner exposes this as `--resume-selection-run-id`; see the
[operator runbook](operator-runbook.md#fixed-cra-and-upper-sco-acquisition).

## Local query interfaces

The existing `Corpus`, `lookup_source`, `lookup_paper`, and `summarize_corpus`
interfaces remain scoped to one configured corpus. Use the global library tools
when the question spans labels.

The MCP server exposes 30 tools in total, including these three offline global
queries:

- `lookup_library_source(source_id, release="dr3", limit=50, offset=0)`
  returns canonical source attributes, current published dataset/run
  associations, the active bibliography snapshot, cached XP availability with
  staleness, and active SOURCE_ID-linked products.
- `lookup_library_paper(query, limit=50, offset=0)` resolves an exact bibcode,
  arXiv ID, or DOI, or a title substring. It returns sources only from active
  bibliography snapshots and their current dataset associations. Ambiguous
  title matches are returned for refinement.
- `library_status()` reports global counts and paths.

The separate [object–paper evidence layer](paper-evidence.md) adds
`get_paper_evidence_context`, `record_paper_annotations`, and
`query_source_evidence`. Schema v3 appends pending-review assessments without
altering bibliography links. Evidence queries retain all batches and pinned
paper versions instead of selecting a single accepted scientific conclusion.

Pagination limits must be between 1 and 500. These queries call
`get_library(create=False)`: they neither create nor migrate an absent database.
An absent or disabled library produces a structured `exists=false` result
instead of a new `data/_library` directory.

## Paper full-text collections

Full text is an explicit, arXiv-only workflow, not a side effect of bibliography
lookups. `scripts/library.py paper-build` and MCP `download_paper_corpus` accept
bounded selections; `paper-status` / `paper_corpus_status` inspect progress.
`lookup_library_paper` separately reports cached text versions, quality and local
artifact paths without returning document blocks.

Schema v2 adds paper-specific downloads, text versions, artifact references and
input checkpoints, sharing the existing blob store, leases, attempts and runs.
An explicit write migrates a v1 library additively; read-only operations still
read v1 without migrating it. Take a SQLite backup before upgrading a live
library and restart old server processes after the upgrade. Index backup does
not include blob bytes.

Paper selection/run views live under `data/paper_corpora/<label>/runs/<run_id>/`.
Original arXiv versions and parser/dependency versions are pinned independently.
Input occurrences and aliases are retained while downloaded content is deduplicated.
Failed refreshes leave successful global text pointers intact. The existing Gaia
corpus exporter refuses these license-unreviewed paper collections rather than
silently exporting incomplete references or redistributing full text.

See [paper corpus guide](paper-corpus.md) for resume, format limitations, rights,
CLI exit codes, and the representative pilot.

## Read-only full-library viewer

Launch the local viewer against the configured library root:

```bash
uv run python scripts/library.py serve
uv run python scripts/library.py serve --port 8765 --poll-seconds 5
uv run python scripts/library.py --library-root /path/to/library serve \
  --port 8765 --poll-seconds 5
```

Open `http://127.0.0.1:8765/`. The bind address is intentionally restricted to
`127.0.0.1`; this is a local inspection tool, not a network service. It accepts
only `GET` and `HEAD` on fixed viewer/API routes, validates the request host and
origin, and exposes no arbitrary paths, SQL, credentials, downloads, or write
operations. Like the offline query tools, it opens the library with
`get_library(create=False)`: serving an absent library shows an empty state and
does not create or migrate SQLite.

The source table and Hammer--Aitoff sky view enumerate the global index by Gaia
identity `(release, source_id)` (the index column is `gaia_release`). Search,
release, corpus, current-dataset, and spectrum-state filters use revision-pinned
keyset pagination rather than repeated point lookups. The browser checks for a
new revision about every five seconds. If SQLite or a corpus manifest's published
run selection changes during traversal, it restarts instead of combining pages
from two snapshots. Current-dataset views honor `active_run_id`/`snapshot_run_id`,
not whichever run has the newest timestamp; an invalid shared-library pin never
silently selects another run.
Missing or invalid coordinates remain in counts and the source table but are not
plotted at `(0, 0)`.

Spectrum arrays are not embedded in the viewer home page. Selecting a source
verifies and parses one local product on demand; browser actions never start a
Gaia or SIMBAD request. The default `global` scope selects the library's active
product version. A corpus scope selects the exact version pinned by that
corpus/run's version-2 `product_refs.json`; a missing or corrupt pin does not
fall back to the global product. Verified DR3 XP Continuous is preferred over XP
Sampled unless a product type is chosen explicitly. Current executable parsers
are registered only for the exact DR3 product contracts; similarly named DR4
products remain `unsupported` until validated against live Archive products.

## Administration CLI

The global `--library-root` option, when used, must appear before the subcommand.
Status, import, verification, and export commands emit JSON. `serve` instead
prints the local URL and runs until interrupted.

### Status

```bash
uv run python scripts/library.py status
uv run python scripts/library.py --library-root /path/to/library status
```

Status is read-only and does not create an absent database.

### Legacy import

Import is a dry run by default:

```bash
uv run python scripts/library.py import data/legacy_corpus --release dr3
```

Review the inventory, release evidence, warnings, errors, managed-copy bytes,
and promotable products. Apply only after review:

```bash
uv run python scripts/library.py import data/legacy_corpus \
  --release dr3 --catalog /path/to/catalog.csv --label legacy_label --apply
```

`--apply` writes managed copies and index rows but never modifies the source
directory. Reapplying the same hash-complete import is idempotent. Legacy
selection provenance remains explicitly unverified. A product is reusable only
when its declared contract and checksums agree and its parsed normalized/archive
tables establish the requested identifier without conflicting release evidence.
Unparseable, unsupported, or semantically mismatched files remain inventory-only;
a filename or a checksum alone is not proof of product identity.

### Verification and reconciliation

```bash
uv run python scripts/library.py verify
uv run python scripts/library.py verify --reconcile
uv run python scripts/library.py verify --apply-reconcile
```

`verify` checks SQLite integrity and foreign keys, verifies registered blobs,
and reports missing/corrupt blobs, orphan objects, and staging residue.
`--reconcile` only plans regeneration of missing compatibility views from
verified run artifacts. `--apply-reconcile` explicitly applies that safe plan.
Neither mode overwrites a divergent existing view, repairs corrupt product
bytes, nor deletes reported files. Verification also reports pending/failed-late
publication state and marks the check unclean. Activation of a pending run is
`diagnostic_only`: reconciliation does not guess which run should become active.
Rerun the same build or inspect the reported immutable snapshot explicitly;
missing-view repair is limited to already-published runs with no pending
publication for that corpus.

### Export and sharing

```bash
# References remain dependent on the same shared library.
uv run python scripts/library.py export my_label /path/to/export

# Pin one registered run.
uv run python scripts/library.py export my_label /path/to/export \
  --run-id <run-id>

# Copy pinned product bytes and remove the shared-library dependency.
uv run python scripts/library.py export my_label /path/to/export \
  --run-id <run-id> --standalone
```

`SOURCE` may be a corpus path, corpus UUID, or unambiguous label. The destination
must not already exist and must not overlap the source. Managed exports use the
selected immutable snapshot both by default and with explicit `--run-id`; they
do not copy a concurrently changing top-level view. Missing managed snapshots
fail closed. Unverified legacy trees have no invented historical run pin.
Standalone export rejects external file/directory symlinks unless the target is
inside the selected tree or a verified registered library object.

A reference export is typically smaller but needs access to the same library
root. A standalone export verifies and copies the exact pinned product bytes,
rewrites product references, and can be moved independently.

## Concurrency and failure behavior

Reusable source, cache, product, and corpus work is guarded by per-key leases
with fencing tokens. The default lease is 120 seconds, the lease wait is bounded
to 30 seconds, and SQLite's busy timeout is 5 seconds. Long network or file work
occurs outside SQLite write transactions; only short publication/index updates
are transactional.

Database/object commits and corpus compatibility-view renames assert the fencing
token at publication. Generic Gaia product label views hold one short fenced
transaction across replacement of their references, checkpoint, and artifact
metadata. Lease takeover cannot occur between the ownership check and these
replacements. Batch renewal uses one worker rather than a thread per identifier.

If another worker retains a key beyond the wait budget, the caller receives a
busy error instead of waiting forever. A worker that loses its lease cannot
publish with an old fencing token. Rerun the same request after the owning
worker finishes or its lease expires.

## Backup, retention, and recovery

Do not copy a live `index.sqlite3` file directly: the library uses SQLite WAL and
a filesystem copy can omit committed state. Use SQLite's backup API:

```python
from pathlib import Path

from astro_pipeline.library import get_library

store = get_library(create=False)
if store is None or not store.exists:
    raise RuntimeError("library does not exist")
store.backup(Path("/path/to/backups/index.sqlite3"))
```

A complete backup also needs the immutable `objects/` tree referenced by that
index backup. Copy it through the approved data channel and verify the restored
root before use:

```bash
uv run python scripts/library.py \
  --library-root /path/to/restored/library verify
```

On Windows, stop the viewer and close other library connections before replacing
an existing SQLite index during restoration. An open database file cannot be
renamed there. Restart the reader after restoration so it reopens the restored
database and discards pagination revisions from the previous file.

`staging/` is diagnostic residue, not authoritative data.

There is no automatic deletion, pruning, or garbage collection. Historical
runs, observations, product versions, and content-addressed objects are retained
until an operator follows a separately reviewed retention procedure. `verify`
reports orphan objects but intentionally does not delete them.

## Data and credential boundary

The library, corpus directories, exports, FITS/Parquet files, and local SQLite
index are generated data and must not be committed to Git. Gaia credentials
remain in gitignored `config/local.yaml` or environment variables and must not
appear in manifests, CLI output, logs, or product metadata.
