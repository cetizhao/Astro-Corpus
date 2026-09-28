# Operator runbook

## Baseline checks

```bash
uv sync
uv run pytest -q
uv run python -m astro_pipeline.mcp_server
```

The default test suite is offline. Explicit live tests use:

```bash
uv run pytest -m network
```

Do not make network tests the default CI gate; archive availability is external
state rather than a deterministic code property.

## Credentials

Anonymous Gaia TAP covers astrometry, availability, and bulk main-table
queries. XP DataLink may require a Gaia account. Put credentials only in
gitignored `config/local.yaml`:

```yaml
gaia:
  username: your_username
  password: your_password
```

Never commit, log, or paste the real password into issues or review comments.
If DataLink login is unavailable, TAP must remain anonymous; logging in and
then failing DataLink authentication can invalidate an otherwise working TAP
session.

## Shared library configuration and status

High-level corpus and membership builders use the shared library by default.
Its root resolves from `ASTRO_PIPELINE_LIBRARY_DIR`, then `library.root`, then
`<output.base_dir>/_library` (`data/_library` normally). Set personal values only
in gitignored `config/local.yaml`; `library.enabled: false` restores the legacy
label-local path.

Read global status without creating an absent database:

```bash
uv run python scripts/library.py status
uv run python scripts/library.py --library-root /path/to/library status
```

The default library contains `index.sqlite3`, immutable
`objects/sha256/...`, and temporary `staging/` files. Corpus labels retain
`runs/<run_id>/` snapshots and publish top-level compatibility views through
`active_run_id`. Only prepared completed/partial snapshots can become active;
a terminal failed build preserves the previous active run. The immutable run
manifest and SQLite completion record are saved before activation. Late
view/index publication failures are recorded as `view_pending` so they can be
inspected and reconciled without repeating a successful download. A run/export
manifest with `snapshot_run_id` is pinned and must not redirect to a newer run.

See [library.md](library.md) for exact identity, cache, product-reference, and
query semantics.

## Gaia TAP routing and timeouts

Catalogue/XP queries with an explicit positive limit up to 2,000 rows and
source-ID queries with at most 2,000 selected IDs use synchronous TAP. Small
position-matching batches also use synchronous TAP. Larger or unbounded
catalogue queries use asynchronous UWS jobs. Bulk `execute_adql` always uses
async, even for a small `TOP`, and never falls back to sync.

All TAP requests use HTTPX and honor the normal `HTTP_PROXY`, `HTTPS_PROXY`,
`ALL_PROXY`, and `NO_PROXY` environment handling. TAP startup and metadata
probing do not import astroquery's Gaia singleton, which otherwise fetches
server announcements before any query deadline can apply. The separate
DataLink/login transport still uses astroquery, loaded only for product work.

- `network.request_timeout_seconds` bounds each HTTP exchange, including the
  response body; connection setup is capped at 10 seconds or the smaller
  request budget.
- `gaia.default_query_timeout` bounds the complete query's network work,
  including submission, polling, result retrieval, retries and retry sleeps.
  It defaults to 600 seconds (120 in the benchmark configuration).
- `gaia.metadata_timeout_seconds` is a separate total budget for each
  `TAP_SCHEMA` query, defaulting to 30 seconds. It does not enter the async queue.
- Async job cleanup is best-effort and can add at most 15 seconds after query
  completion/failure. A cleanup failure logs the job URL without discarding a
  successful result or masking the original error. Set agent tool timeouts
  above the query budget plus this cleanup allowance.

Retries of phase/result requests reuse the same job. Submission retries are
limited to connection-establishment failures. An ambiguous failed submission
is not blindly repeated, because the server may already have created a job.
TAP `QUERY_STATUS=ERROR` and `OVERFLOW`, including status elements
after the table, are failures rather than empty or complete catalogues. A
position-matching overflow requires a smaller batch/radius; it must not be
interpreted as missing members. Bulk failures remain resumable via checkpoint.

## Corpus smoke tests

Fast member-only source test:

```bash
uv run python scripts/build_corpus.py \
  --name "TW Hya" \
  --label tw_hya_smoke \
  --out-dir data/tw_hya_smoke \
  --skip-simbad
```

Taurus membership live smoke used during v0.4.0 development:

```python
from astro_pipeline.membership import build_membership_corpus

result = build_membership_corpus(
    label="taurus_members_smoke",
    out_dir="data/taurus_members_smoke",
    catalog_id="J/AJ/156/271",
    table_id="J/AJ/156/271/table1",
    limit=3,
    skip_simbad=True,
)
print(result)
```

On 2026-08-13 this returned 3 catalogue rows, 2 Gaia DR3 matches, and 1
explicit unmatched row. Live counts can change only if an external catalogue
or matching service changes; the invariant is that totals and unmatched rows
remain auditable.

## Gaia release discovery smoke

Discovery reads only `TAP_SCHEMA`; it downloads no catalogue rows:

```python
from astro_pipeline.config import PipelineConfig
from astro_pipeline.gaia_archive import probe_gaia_release
from astro_pipeline.sources.gaia import GaiaSource

config = PipelineConfig.load()
source = GaiaSource(config.gaia, config.network)
print(probe_gaia_release("dr3", source.execute_metadata_adql))
print(probe_gaia_release("dr4", source.execute_metadata_adql))
```

Expected before DR4 publication: DR3 resolves to `gaiadr3`; DR4 returns
`archive_reachable=true` but `published_in_tap=false`, or a structured network
error if the Archive is unavailable. Never turn a missing DR4 schema into a
guessed `gaiadr4` value. Each metadata query has a separate 30-second total budget.

For DataLink, first use `list_gaia_products(release="dr4")` without a source
to inspect client hints. Passing a source ID performs a live link query and is
the only result that may mark products as source-specific Archive-confirmed.

## Library administration

### Verify and reconcile

```bash
uv run python scripts/library.py verify
uv run python scripts/library.py verify --reconcile
uv run python scripts/library.py verify --apply-reconcile
```

Verification checks SQLite, foreign keys, registered object checksums, orphan
objects, and staging residue. `--reconcile` is a dry-run plan for missing
compatibility views from verified runs; `--apply-reconcile` explicitly applies
that plan. Existing divergent files, corrupt bytes, and orphan objects are not
overwritten or deleted. Pending/failed-late publication is reported as unclean
with `diagnostic_only` recovery: the command does not activate a pending run.
Rerun or inspect that build explicitly. Missing-view repair is limited to
already-published runs without a pending publication for the same corpus.

### Import legacy corpora

```bash
# Dry run: inventory/hash only; writes neither source nor library.
uv run python scripts/library.py import data/legacy --release dr3

# Explicit non-destructive apply after reviewing the plan.
uv run python scripts/library.py import data/legacy --release dr3 \
  --catalog /path/to/catalog.csv --label legacy --apply
```

Apply writes managed copies and index rows but does not modify the legacy tree.
Unknown/conflicting release evidence or incomplete product provenance is
reported rather than guessed. Reapplying the same import is idempotent. Product
promotion requires contract/checksum consistency and parsed identifier evidence
in normalized/archive tables, with no conflicting release metadata. Unsupported,
unparseable, or mismatched payloads stay inventory-only. Original legacy sample
selection provenance remains unverified rather than being reconstructed by guess.

### Export and share

```bash
# Reference export; still depends on this shared library.
uv run python scripts/library.py export my_label /path/to/export

# Exact run, with pinned product bytes copied into a portable tree.
uv run python scripts/library.py export my_label /path/to/export \
  --run-id <run-id> --standalone
```

`SOURCE` may be a path, corpus UUID, or unambiguous label. Export refuses an
existing or overlapping destination. Managed exports select an immutable run by
default; `--run-id` selects a specific historical run. Missing managed snapshots
fail closed, while unverified legacy exports get no fabricated run pin. External
symlink targets are refused unless confined to the source tree or verified as
registered library objects. Without `--standalone`, references still need the
shared library. Standalone mode copies exact pinned versions, not newer products.

### Back up the library

Never copy a live `index.sqlite3` directly because WAL state may be omitted. Use
the SQLite backup API:

```python
from pathlib import Path
from astro_pipeline.library import get_library

store = get_library(create=False)
if store is None or not store.exists:
    raise RuntimeError("library does not exist")
store.backup(Path("/path/to/backups/index.sqlite3"))
```

A complete backup also needs the immutable `objects/` tree referenced by that
index. Copy it through the approved data channel and verify the restored root:

```bash
uv run python scripts/library.py \
  --library-root /path/to/restored/library verify
```

Do not treat `staging/` as authoritative data.

The library performs no automatic pruning or garbage collection. Verification
reports orphan/staging files but never removes them.

## Progress and resume

For corpus builds inspect:

```text
data/<label>/manifest.json
data/<label>/gaia_xp_sampled/checkpoint.json
data/<label>/gaia_xp_continuous/checkpoint.json
data/<label>/simbad_refs.checkpoint.json
data/<label>/simbad_refs.parquet
```

Via MCP, use `corpus_status(label)`. Rerun a build with the same label and the
same source selection to resume; optional products may be added. Library-backed
builds create a new `runs/<run_id>/` attempt. Once members exist and are indexed,
the current implementation can publish that run through `active_run_id` even if
a later requested stage fails, so always inspect terminal status, warnings, and
errors. A root-pointer update also precedes final snapshot-manifest and SQLite
run metadata. After interruption, compare the root/run manifests and library
status manually; `verify` checks database/blob integrity but not this pointer
ordering. Inspect a pinned run or export through its `snapshot_run_id`; do not
infer publication from `current_run_id` alone. A different target, radius,
`limit`, membership table, or uploaded-CSV content under the same label is
rejected. `partial` usually means an optional XP or visualization stage needs
another run; inspect `warnings`, `errors`, and the per-stage status.

Shared exact-ID, XP, literature, and product observations use 30-day positive
and one-day explicit-negative TTLs by default. Service failures remain
retryable and do not replace the last successful current cache pointer. Use the
Python builder's `refresh=True` only when replacement observations are actually
required; a successful empty refresh can remove current source-paper edges while
history remains. The corpus MCP wrappers and `scripts/build_corpus.py` do not
currently expose this switch.

For bulk downloads:

```bash
uv run python scripts/gaia_bulk.py plan demo \
  --level 12 --pixel-start 157212945 --pixel-stop 157212946 --page-size 1000
uv run python scripts/gaia_bulk.py run demo --max-chunks 1
uv run python scripts/gaia_bulk.py status demo
```

That pixel was the real smoke test used on 2026-08-13; it contained 3 Gaia DR3
sources and wrote one Parquet part. For larger work, start with a short pixel
range and one chunk. `--max-chunks 0` means every remaining chunk and should be
used only after storage and service impact are understood.

## Common failures

| Symptom | Meaning | Action |
|---|---|---|
| `library_status` says `exists=false` | Library disabled or not initialized at the resolved root | Check `library.enabled`, `library.root`, and `ASTRO_PIPELINE_LIBRARY_DIR`; run a write build only if initialization is intended |
| `library item is busy` | Another worker retained the same fenced key beyond the bounded wait | Let that worker finish or the lease expire, then retry; do not bypass the index |
| Two generic product writers target one label | Product label-view lease check and file replacement are not one fenced operation | Serialize writers for that output label; do not rely on last-writer order |
| `verify` reports missing/corrupt blobs | A pinned immutable object failed size/checksum validation | Preserve evidence, restore the exact object from backup, and verify again; do not substitute the active version |
| `verify` reports orphan/staging files | Bytes are not registered or a publication was interrupted | Review them manually; verification and reconciliation never delete them |
| Region result is not cluster members | Region is a geometric cone | Use the membership search/preview/download workflow |
| `Gaia.login`/DataLink returns 503 | Product service unavailable | Keep TAP anonymous; resume XP later |
| TAP job remains queued | Archive load, especially upload jobs | Current membership matcher uses ordinary small-cone ADQL; retry later |
| XP stage is `partial` | Some products failed or were unavailable | Inspect availability and checkpoint; rerun same label |
| One membership row is unmatched | No Gaia source inside radius or bad/old coordinates | Review `unmatched_members.csv`; consider epoch/proper motion and radius |
| Multiple match candidates | Dense field or wide radius | Review `membership_match_candidate_count` and separation |
| Blank HTML / ECharts warning | Vendored JavaScript is missing/corrupt | Restore `src/astro_pipeline/viz/vendor/echarts.min.js` and rebuild |
| Bulk plan is `partial` | Worker stopped at first failed pixel | Inspect `last_error`; rerun same label |
| Label rejected | Labels are directory names, not paths | Use letters/digits plus `.`, `_`, or `-` only |
| DR4 probe finds no schema | DR4 is not published in TAP or Archive is unavailable | Keep the profile provisional; do not guess the schema |
| DR4 bulk plan rejected | Source-ID partition rule is not confirmed | Wait for the official data model and update the release profile |
| `Using SOCKS proxy ... socksio` | Environment uses a SOCKS proxy without optional HTTP support | Run `uv sync`; v0.5 declares `httpx[socks]` |
| `CROWDED_FIELD_IMAGE` rejected | Astroquery omits the binary image from its returned table dictionary | Wait for the blob-preserving transport adapter |

## Data sharing and Git

The repository intentionally ignores generated catalogues, spectra, the shared
SQLite index, and content-addressed objects. A clone gets code and documentation,
not local corpus/library data. Use `scripts/library.py export` when the recipient
shares the same library, or `--standalone` to copy exact pinned product bytes.
Otherwise share a corpus or complete library backup through an approved data
channel, or let the collaborator rebuild from manifest inputs. Do not force-add
credentials, proprietary files, SQLite/WAL files, or large generated datasets
into Git.
