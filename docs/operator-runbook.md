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

The viewer also has an opt-in real-Chrome regression test:

```bash
uv run --with playwright python -m pytest -q tests/test_viewer_browser.py
```

This uses an installed Chrome browser and injects Playwright only for the test;
Playwright is not a viewer runtime dependency.

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

### Serve the read-only full-library viewer

```bash
uv run python scripts/library.py serve
uv run python scripts/library.py serve --port 8765 --poll-seconds 5
uv run python scripts/library.py --library-root /path/to/library serve \
  --port 8765 --poll-seconds 5
```

Open `http://127.0.0.1:8765/` and stop the process with `Ctrl+C`. The viewer is
restricted to loopback, opens SQLite read-only, and does not create an absent
library. It shows all indexed release-qualified sources by default, can filter
to current published datasets, and retains sources with missing coordinates in
the table even though they are omitted from the sky plot. Library changes cause
a revision-safe refresh rather than mixed old/new pagination.

Selecting a source reads an already-cached spectrum on demand. `global` means
the current active library product; a named corpus snapshot means that run's
exact pinned product. The scopes do not fall back to each other, and page actions
never start a remote download. Only verified DR3 XP Continuous/Sampled contracts
currently have executable parsers.

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
Its import guard suppresses only the importing thread's automatic server-announcement
request and immediately restores the original transport; client initialization is
covered by a fresh-process test that forbids network connections.
It uses a per-owner finite-timeout HTTP(S) client and retains astroquery's direct
routing; it does not automatically inherit TAP's environment-proxy handling.
Default XP batches contain 50 Sampled sources or 5 Continuous sources. The Archive's
5,000-ID ceiling is not a recommended batch size: a 500-source Sampled batch
repeatedly exceeded the 300-second request timeout during the Upper Sco build.
Successful products remain reusable when resuming with smaller batches.

The following budgets apply to TAP:
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

## Fixed CrA and Upper Sco acquisition

`scripts/collect_regions.py` is the bounded runner for two fixed published
member tables. These presets are not geometric region searches:

| Target | Table and fixed full label | Expected full rows | DR3 match input |
|---|---|---:|---|
| `cra` | `J/AJ/163/64/table1` → `cra_esplin2022_table1_full` | 393 | EDR3 IDs are retained as evidence; match numeric `RA_ICRS` / `DE_ICRS` at 3 arcsec |
| `uppersco` | `J/AJ/160/44/table5` → `uppersco_luhman2020_table5_full` | 1,761 | DR2 IDs are retained as evidence; match numeric `RAJ2000` / `DEJ2000` at 3 arcsec |

The Upper Sco preset is the adopted-member mid-infrared table, not the candidate
`table1` or the 2,020-row mixed `table4`. Both selected tables are preserved in
full, with no implicit probability, candidate, RUWE, population, or quality cut.
Older Gaia IDs are not reused as DR3 IDs; matched, unmatched, invalid-coordinate,
duplicate, and multiple-candidate rows remain part of the report.

Start with the default 10-row smoke labels
`cra_esplin2022_table1_smoke` and
`uppersco_luhman2020_table5_smoke`:

```bash
# Membership acquisition and DR3 coordinate matching only.
uv run python scripts/collect_regions.py \
  --target all --stage members

# Exercise XP, bibliography, and enrichment on the same smoke labels.
uv run python scripts/collect_regions.py \
  --target all --stage all --static-viz-mode status_only
```

Then run the full tables one target at a time so failures and service load remain
separable:

```bash
uv run python scripts/collect_regions.py \
  --target cra --full --stage all \
  --retry-passes 1 --attempt-timeout-seconds 3600 \
  --static-viz-mode status_only

uv run python scripts/collect_regions.py \
  --target uppersco --full --stage all \
  --retry-passes 1 --attempt-timeout-seconds 3600 \
  --static-viz-mode status_only
```

`--stage` is cumulative: `members`, `sampled`, `continuous`, `papers`, and
`enrich` successively add work; `all` requests every stage. `--full` selects the
fixed permanent label and Python `limit=None`; smoke mode uses the fixed `_smoke`
label and accepts `--smoke-limit` (default 10). The fixed runner defaults to
`status_only`, which keeps the per-corpus overview and cached-spectrum status
without calibrating and embedding every spectrum array. Choose `embedded` only
when a self-contained spectral page is needed, or `off` to skip static HTML.

Normal recovery is to rerun the exact same command. The target, smoke/full mode,
and label preserve the selection contract, while source, product, and SIMBAD
checkpoints reuse completed work. `refresh=False` is hard-coded: successful
products are not deliberately discarded or redownloaded. One invocation makes
the initial attempt plus one bounded ordinary-resume pass by default;
`--retry-passes` accepts 0--3 additional passes. Each attempt runs in a child
process with a hard positive deadline set by `--attempt-timeout-seconds`
(default 3,600 seconds). A timeout terminates that attempt but retains existing
checkpoints and shared-library products. If the terminated worker held a lease,
a prompt rerun may report the item busy until that lease expires; wait and rerun
the same command rather than bypassing the library. Persistent authentication,
Archive, or SIMBAD failures therefore terminate with a report rather than an
unbounded loop.

To continue downstream work without repeating catalogue retrieval and positional
matching, explicitly pin a previously published selection from the same corpus:

```bash
uv run python scripts/collect_regions.py \
  --target uppersco --full --stage all \
  --resume-selection-run-id <published-run-uuid> \
  --retry-passes 0 --static-viz-mode status_only
```

The Python builder and membership MCP tool accept `resume_selection_run_id` too.
Preflight verifies the corpus/selection contract, publication record, registered
file checksums, original-table hash/schema, and every input/match ordinal before
creating a new run. It retains unmatched rows, duplicate same-star occurrences,
and frozen astrometry rather than substituting current global source values.
The original run is unchanged. Historical J2000 column-name epoch inference is
corrected to unknown only when the original schema supports that correction;
the inherited metadata and correction remain explicit in the new provenance.
This option requires shared-library mode, cannot be combined with `refresh=True`,
and is never implicit: ordinary new selections still query VizieR/Gaia.

Basic SIMBAD title/DOI metadata is checkpointed before optional DOI-to-arXiv
lookups. arXiv requests are spaced at least three seconds apart within a process.
Repeated HTTP 429 responses or a Retry-After beyond the bounded wait budget defer
the remaining uncached DOI queries for that build; fresh cached observations
remain usable. `simbad_refs.enrichment.json` records failed/deferred DOI keys
separately from successful no-preprint observations. The corpus becomes `partial`,
not `completed`, when this enrichment is incomplete. Ordinary recovery retries
those queries without replacing the last successful cache pointer with a failure.

The Python helper `sources.arxiv_doi.arxiv_ids_from_dois` supports explicit OR
batches of at most 20 DOIs on the same three-second request clock. It validates
Atom identities, exact normalized DOI fields/links, and OpenSearch completeness
before returning observations. Multiple arXiv IDs for one DOI are accepted only
when supplied `expected_titles` identify exactly one candidate after case,
whitespace, and punctuation normalization; this is not fuzzy title matching.
The result retains candidate/selection evidence, and an unresolved ambiguity
fails only that DOI. Malformed, truncated, or failed responses invalidate their
whole batch. Only `resolved` values may be promoted through the fenced
`library.papers.cached_doi_arxiv` API; `failed` and `deferred` values must never
be written as successful no-match observations.

System sleep can also interrupt sockets and pause lease heartbeats. After waking,
a `LeaseLostError` deliberately prevents the old worker from publishing; it is
not a reason to disable fencing or lengthen cache freshness. Resume normally to
reuse successfully committed products. On macOS, a long collection can be run
under `caffeinate -i uv run python scripts/collect_regions.py ...` to prevent
idle system sleep for that command's lifetime. This does not override lid-close
sleep or a deliberate suspension.

Progress and the final report are JSON Lines. Each report pins its inventory to
the attempted `run_id` and records the published `active_run_id` separately.
An attempt that fails before creating a run reports unknown inventory rather
than attributing the previous run's artifacts to itself. The process exits zero only when
every selected target has `completion="completed"` and the final report has
`overall="completed"`; `partial`, `blocked`, or `failed` exits nonzero and
supplies remaining IDs/errors. Completion requires:

- readable original, matched, and unmatched artifacts; completed
  `resolve_members` and `save_members` stages; and
  `matched_rows + unmatched_rows == original_rows`;
- in full mode, exactly 393 or 1,761 original rows for the selected preset;
- for every requested XP type, known availability and a verified ready pinned
  product for every availability-positive source, with no pending IDs;
- for requested literature, a known checkpoint with no pending or failed
  sources and a completed SIMBAD stage (successful zero-reference results are
  distinct from failures);
- for requested enrichment, a populated title for every unique bibcode and a
  run-matched `simbad_refs.enrichment.json` with completed metadata/DOI observations,
  no failed or deferred DOI queries, and no rate-limit flag (a successful lookup
  finding no preprint counts as completed; a missing arXiv ID alone proves nothing); and
- a terminal `completed` manifest.

Starting a job, creating the members table, or attempting every request is not
completion. The local delivery audit on **2026-09-29 at 13:11 UTC** verified both
full collections as published and `completed`:

| Check | CrA | Upper Sco |
|---|---:|---:|
| Original rows | 393 | 1,761 |
| Matched rows / unique DR3 sources | 380 / 380 | 1,698 / 1,696 |
| Unmatched rows retained | 13 | 63 |
| Verified Sampled / availability-positive | 114 / 114 | 590 / 590 |
| Verified Continuous / availability-positive | 234 / 234 | 1,098 / 1,098 |
| Completed SIMBAD source queries | 380 / 380 | 1,696 / 1,696 |
| Successful zero-reference sources | 1 | 4 |
| Unique bibcodes with title and year | 1,122 | 4,500 |
| Source–paper edges | 5,678 | 42,729 |
| Completed DOI queries | 893 / 893 | 3,541 / 3,541 |
| arXiv matches / successful API no-match results | 472 / 421 | 1,723 / 1,818 |

Published immutable runs:

- `cra_esplin2022_table1_full`: `cf0bdfaf-64a0-4a6c-b4b0-18995d4a8e41`
- `uppersco_luhman2020_table5_full`: `c21f106c-e6ae-4244-9e1f-24f6c9dde026`

The audit verified original-table hashes, all input/match ordinals, exactly-once
matched/unmatched row accounting, active-run pins, and every requested pinned XP
product. SIMBAD and DOI stages have no failed or pending/deferred items. A
successful arXiv API no-match is an observation, not proof that no preprint exists.
That September delivery did not download paper PDFs/full text; the separate
paper pilot below does.

These are acquisition checks, **not independent scientific validation of the
matches**. There are 105 CrA and 226 Upper Sco rows with multiple candidates
within 3 arcsec. Upper Sco has eight separations above 1 arcsec (maximum 2.9531
arcsec), and two repeated Gaia sources, each present twice:
`6243841249531772800` and `6048935358761628288`. All occurrences remain in the
snapshot. No proper-motion epoch propagation was applied; unknown catalogue
observation epochs remain unknown. The audit lists the affected row ordinals for
review, rather than discarding them or widening the matching radius.

The shared-library audit found 2,343 sources, six corpora, 5,199 unique paper
records, 2,036 ready product records, and 2,237 registered blobs. SQLite integrity
and foreign keys passed; no missing/corrupt blobs, orphan objects, staging files,
or pending publications were found. Historical failed attempts remain visible;
the new completed snapshots do not overwrite their evidence. Legacy TWA and
Orion retain unverified selection provenance, and Orion remains a field sample.

The live browser acceptance covered all 2,343 sky positions, 1,332 registered
ready-XP sources, both dataset filters, a real 343-point Continuous spectrum,
light/dark themes, and a 390-pixel mobile viewport without overflow or JavaScript
errors. These are dated local results, not data bundled with a repository clone.
Evidence remains under gitignored `data/collection_validation/`:

- `report.json`: run-bound counts, matching caveats, and full library verification;
- `code-validation.json`: offline, browser, live-Gaia, packaging, and installed-wheel checks;
- `viewer-accepted-20260929.json` and its `-light.png` / `-dark.png` screenshots;
- `cra-delivery-build.log`, `upper-delivery-build.log`, and retained arXiv batch observations;
- `dr4-probe-delivery.json`: reachable Archive, but no discoverable DR4 TAP schema.

For later runs, use the emitted report and pinned local artifacts rather than
assuming this dated delivery proves the new attempt complete.

## Paper full-text pilot (2026-10-09)

The first full-text pilot selected 30 unique arXiv works **before acquisition**
from the fixed Upper Sco/CrA bibliography runs above, plus the TWA census
`2305.03557`. Selection preserved snapshot hashes and original bibliography row
ordinals. Title-based topic strata were sampling hints, not independent
scientific membership/relevance determinations. The fixed selection contains
nine pre-2010, ten 2010–2019 and eleven 2020-or-later catalogue publication years.

Local collection: `papers_pilot30_20261008`; run:
`0ee03665-f671-4ae3-b84c-e4a57c82e2e2`. The label reflects the start date; final
checks finished on 2026-10-09 in UTC+08.

| Check | Result |
|---|---:|
| Selected / acquired / usable full-text papers | 30 / 30 / 30 |
| Primary parse route | 30 HTML |
| Same-version reference PDFs | 30 |
| Structured JSON / Markdown documents | 30 / 30 |
| Verified main-collection artifact references | 180 |
| Papers with tables / display equations / figure captions | 30 / 20 / 30 |
| Reported license evidence / unknown | 26 / 4 |
| Real PDF fallback smoke checks | 6 / 6 passed |
| Additional real LaTeX source-bundle checks | 3 / 3 parsed as partial |
| Completed-run replay | Same run and versions; zero network calls or new attempts |

All 30 primary parses are **`partial`**, not scientifically certified: figure
images are not separately fetched/interpreted, and some HTML title/table
footnote markers are duplicated or folded into text. Selected formulae, signed
values, units, table rows and citation targets were checked against original
PDFs/HTML for six papers. This was an AI-assisted representation spot check,
not a human expert sign-off or a whole-corpus accuracy guarantee. Six cached
PDFs independently exercised the lower-fidelity text fallback. The source-only
checks used `2305.03557v1`, `astro-ph/0012261v1` and `astro-ph/0410106v1`; they did
not replace the HTML-derived collection pins. Unknown licenses remain unknown,
and reported licenses are not automatically treated as training permission.

The shared index was backed up through `LibraryStore.backup()` before the
additive v1→v2 migration. Final global verification passed with 2,417 registered
blobs, no missing/corrupt bytes, no paper-reference errors, no foreign-key
violations and no orphan/staging residue. Existing Gaia source/product counts
were not replaced by this paper workflow.

Evidence is local and gitignored under `data/paper_validation/`:

- `pilot30_selection.json`: fixed inputs and selection provenance;
- `pilot30_report.json`: per-paper metadata, parser metrics, checksums and PDF fallback checks;
- `manual_spot_checks.md`: six-paper visual/structural observations and limitations;
- `source_smoke_report.json` and `source_smoke/`: three source-format validation receipts;
- `resume_check.json`: same-run/same-version, zero-network replay;
- `library_verification.json`: full shared-library integrity check;
- `code_validation.json`: 508 passing offline tests (1 skipped, 6 deselected),
  package build, real MCP stdio smoke, and final zero-network replay;
- `library-before-fulltext-20261008T132457Z.sqlite3`: pre-migration index backup
  (the separately retained object store is still required).

Inspect or repeat the bounded workflow:

```bash
uv run python scripts/library.py paper-status papers_pilot30_20261008
uv run python scripts/collect_papers.py build \
  --selection data/paper_validation/pilot30_selection.json \
  --label papers_pilot30_20261008
uv run python scripts/collect_papers.py report \
  --label papers_pilot30_20261008 \
  --output data/paper_validation/new_report.json --check-pdf 6
```

A usable but partial build exits **3**, which is expected for this pilot; see
[paper corpus guide](paper-corpus.md) for the other exit codes. Report outputs
refuse overwrite. No live data is bundled with a fresh clone.

Two operational issues were exercised rather than concealed:

- Laptop sleep interrupted lease heartbeats. The build stopped safely; resuming
  the same run retained version pins and downloaded artifacts. On macOS, an
  operator may use process-scoped `caffeinate -i <build command>` for a long run;
  this does not change permanent power settings or bypass expired leases.
- Slow PDF streams can outlast a per-read socket timeout. The downloader now
  budgets 120 seconds across a request's redirects/retries, checks elapsed time
  while streaming, and bounds individual blocking reads. Exhausting the budget
  remains a retryable failure, not an unavailable-paper observation. A missing
  reference PDF does not discard otherwise usable structured text.

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
a completed or explicitly partial run can be published through `active_run_id`,
so always inspect terminal status, warnings, and errors. The immutable run
manifest and SQLite prepared record are saved before activation; a late
view/index publication failure remains visible as `view_pending`, while an
earlier terminal failure preserves the previous active run. Inspect a pinned run
or export through its `snapshot_run_id`; do not infer full stage completion from
`active_run_id` or `current_run_id` alone. A different target, radius, `limit`,
membership table, or uploaded-CSV content under the same label is rejected.
`partial` usually means an optional XP or visualization stage needs another run;
inspect `warnings`, `errors`, and the per-stage status.

Shared exact-ID, XP, literature, and product observations use 30-day positive
and one-day explicit-negative TTLs by default. Service failures remain
retryable and do not replace the last successful current cache pointer. Current
Python builders, `scripts/build_corpus.py --refresh`, and the corresponding MCP
corpus/membership/product tools can request refresh, but use it only when
replacement observations are intentional: a successful empty refresh can remove
current source-paper edges while history remains. Ordinary resume, including the
fixed CrA/Upper Sco runner, uses `refresh=False`.

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
