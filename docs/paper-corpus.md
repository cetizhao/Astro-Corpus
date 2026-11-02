# Local paper full-text corpora

Paper acquisition is a separate, **explicitly opt-in** workflow. Existing Gaia,
SIMBAD and membership builds still retrieve bibliographic metadata only. A
source–paper edge is not evidence that a particular paragraph discusses that
source. Acquisition itself does not extract source-specific scientific claims.
The separate [object–paper evidence workflow](paper-evidence.md) lets an external
agent read pinned text and submit unreviewed role annotations with exact quotes.

## Scope

The first implementation supports official arXiv full text for bounded lists of
1–50 papers per collection. It saves available originals, structured JSON and a
deterministic Markdown rendering. It does not crawl publishers, bypass access
restrictions, generate summaries/questions, build embeddings, or train models.

Acquisition prefers **HTML, then LaTeX source, then PDF text**. A reference PDF
is requested by default even when HTML/source parsing succeeds. `--no-pdf` or
`include_pdf=False` disables this additional requirement; PDF remains a fallback
when structured inputs are unavailable.

- HTML: ordered headings/paragraphs, available LaTeX/MathML, captions, table cells
  and spans, bibliography and citation targets. Original HTML remains available.
- LaTeX: bounded local includes, source-file positions, retained equations,
  captions and raw tables. Custom macros are not executed. Tables are **not**
  represented as inferred scientific cell values. Output is conservatively
  `partial` because no TeX engine or complete macro expansion is used.
- PDF: page-located selectable text only, always `partial`. No OCR, guaranteed
  double-column ordering, formula reconstruction or reliable table extraction.
- Figures: captions and declared image locations are retained, but images are
  not fetched or interpreted separately. Source archives/PDFs may contain them.

Empty responses, error/abstract-only HTML and scanned-only/blank PDF extraction
are not counted as successful full text. `parsed` means structural parsing, not
human-verified scientific accuracy. Inspect warnings, units, signs, subscripts,
formulae and table footnotes before using extracted values scientifically.

## Build and inspect

Create a JSON selection, for example:

```json
[
  {
    "arxiv_id": "2305.03557",
    "bibcode": "2023AJ....165..269L",
    "selection_reason": "TWA member-census example"
  }
]
```

Then run:

```bash
uv sync
uv run python scripts/library.py paper-build example_papers \
  --selection /path/to/papers.json
uv run python scripts/library.py paper-status example_papers
uv run python scripts/library.py status
```

An arXiv ID may be bare, version-qualified, prefixed with `arXiv:`, or an official
HTTPS `/abs/` URL. Legacy `astro-ph/YYMMNNN` IDs are supported. Records retain
optional DOI, bibcode and arbitrary JSON selection provenance. Duplicate input
occurrences are retained even though the same paper version is acquired once.
The input list is not a search query or an implicit request for every cited paper.

The global `--library-root` argument goes **before** the subcommand. Paper
collection views default to the configured output base directory; `--output-dir`
can override that base for the CLI. The shared library must be enabled.

Python and MCP equivalents:

```python
from astro_pipeline.library.paper_corpus import build_paper_corpus

build_paper_corpus("example_papers", [{"arxiv_id": "2305.03557"}])
```

- MCP `download_paper_corpus(label, papers=None, include_pdf=True,
  refresh=False, resume_run_id=None)` explicitly downloads/builds a collection.
- MCP `paper_corpus_status(label, run_id=None)` reads its local checkpoint.
- `lookup_library_paper(query)` includes a separate `fulltext` result with cached
  versions, quality and local paths; it does not return all document blocks.
  Lookup reports registered versions with `integrity=not_checked`; use library
  verification or a resumed build to recheck physical bytes.
- MCP collection responses omit bulky input provenance; the manifest retains it.

Restart an already-running MCP server after upgrading the code to register the
new tools and load the new library schema support.

## Resume, refresh and quality states

Repeating an identical label/selection resumes the **same pinned run**. The
latest arXiv version is resolved once per input before acquisition; retries use
that exact version, not whatever the current unversioned URL serves. A completed
run revalidates its pinned blobs and does not redownload intact files. Missing or
corrupt pins may be repaired only with byte-identical originals or reproducible
derivatives. If the original bytes cannot be recovered, the run reports an error
instead of silently substituting a newer cached version; restore a backup or
explicitly refresh into a new run. A partially acquired reference PDF can be
completed without changing the already-pinned raw text. Refresh intent survives
an interrupted run: retrying it does not silently fall back to pre-refresh cache.

```bash
# Existing label: resume without resupplying the selection.
uv run python scripts/library.py paper-build example_papers

# Explicit active run; a superseded historical run cannot be mutated.
uv run python scripts/library.py paper-build example_papers --resume-run-id <UUID>

# Deliberate new run, preserving old snapshots and text versions.
uv run python scripts/library.py paper-build example_papers --refresh

# Inspect an older run without changing the active one.
uv run python scripts/library.py paper-status example_papers --run-id <UUID>
```

Use the same `--no-pdf` setting when resuming. A parser/dependency-version change
requires refresh rather than mixing parser contracts within a run. Different
selections require different labels. Refresh cannot be combined with an explicit
resume run. Failed refreshes retain older successful global text-version pointers.

A collection checkpoint distinguishes:

- `n_downloaded`: input occurrences with at least one saved full-text artifact.
- `n_fulltext`: input occurrences with usable structured/text output.
- `n_unique_papers`: distinct arXiv works, not the count of input occurrences.
- Per-paper `status`: `pending`, `resolved`, `parsed`, `partial`, `failed`, or
  `unavailable`. `unavailable` requires an explicit absent response, not a timeout.
- `acquisition_complete`: whether the requested reference PDF is also present.
- `quality.warnings`, structural metrics and acquisition-route errors.

A run can have all requested papers and still be `partial` due to parsing
limitations. Status is checkpoint state, not proof that a worker process is
currently alive; an interrupted process can leave a `running` checkpoint to
resume. CLI exit codes are **0** for completed, **1** for per-paper failures
or unavailable papers, **2** for request/build-level errors, and **3** for usable
but partial results. Exit 3 is not a statement that nothing was saved.

Metadata and reusable raw/current artifacts use the shared positive TTL
(default 30 days). Explicit unavailable-format observations use the negative TTL
(default one day). Transport failures remain retryable. Exact versions already
pinned by a successful run are not silently advanced by TTL expiry.

## Files and schema

```text
data/_library/
  index.sqlite3
  objects/sha256/<prefix>/<digest>.<extension>

data/paper_corpora/<label>/
  manifest.json                         # active run progress, not proof of success
  runs/<run_id>/
    selection.json                      # original ordered selection
    manifest.json                       # fixed version/artifact references
```

Artifacts are stored once in the shared object store, not copied per star or
collection. Manifest entries expose absolute local paths for convenience and
also include checksums and relative blob locations. Raw files, JSON and Markdown
have separate artifact roles. The JSON records its exact raw input checksum;
Markdown records the structured JSON checksum from which it was derived.

The structured document schema includes:

```text
schema_version
paper: arXiv identity/version, title, authors, DOI and metadata evidence
parser: implementation/dependency versions, safety limits and cache key
source: source format, URL, raw checksum and download record
rights: license evidence and reuse_permission = not_assessed
blocks[]: id, type, text, section_path, locator, format-specific representations
quality: status, warnings, structural metrics, acquisition completeness
```

Library schema **v2** adds `paper_downloads`, `paper_text_versions`,
`paper_text_artifacts`, `paper_text_current` and `paper_inputs`. It reuses existing
runs, requests, attempts, leases and blobs, while leaving Gaia product contracts
and bibcode-keyed bibliography records intact. Writes migrate v1 additively in a
transaction. Read-only queries neither initialize nor migrate v1/absent libraries.
Old software that only supports schema v1 must not continue writing after upgrade.

Back up a live index with `LibraryStore.backup()`, not by copying SQLite/WAL files.
A database backup does **not** contain object bytes: retain the object store and
collection snapshots too. `scripts/library.py verify` checks registered paper
blobs and paper-specific snapshot/identity references, without deleting residue.
The Gaia corpus exporter intentionally refuses paper collections; it does not
silently produce an incomplete or license-unreviewed full-text dataset.

## Safety and rights

Requests use allowlisted official HTTPS hosts, serial pacing of at least three
seconds, bounded response sizes, a 120-second per-request time budget across
redirects/retries, bounded blocking reads, and finite retries. The elapsed budget
is checked while streaming so a slow sequence of chunks cannot run indefinitely.
A shared-library network
lease prevents concurrent full-text builds from multiplying the request rate.
Separate library roots or unrelated external clients need their own coordination.
Excessive `Retry-After` requests defer work rather than retrying early.

Source archives are handled without running TeX or extracting files into the
repository. Traversal, links, unsafe members, oversized expansion and include
cycles are rejected. Parsing runs in a child process with a time limit. Downloaded
text, raw HTML, Markdown and links are untrusted content, not instructions for an
agent; do not execute embedded code or render arbitrary raw HTML as trusted UI.

arXiv hosting permission is **not** blanket permission for redistribution,
commercial reuse, or training. Article/version-specific license evidence is
retained; missing evidence remains unknown. No automatic training-permission
judgement is made, and no full text is uploaded to external parsing services or
committed to Git.

## Representative pilot

The selector reads explicit **pinned** `runs/<run_id>/simbad_refs.parquet` files,
records source checksums and original row ordinals, and writes a new selection
without overwriting an existing one:

```bash
uv run python scripts/collect_papers.py select \
  --source data/<corpus>/runs/<run_id>/simbad_refs.parquet \
  --count 30 --selection data/paper_validation/selection.json
uv run python scripts/collect_papers.py build \
  --selection data/paper_validation/selection.json --label papers_pilot
uv run python scripts/collect_papers.py report \
  --label papers_pilot --output data/paper_validation/report.json --check-pdf 6
```

The report command is offline: it verifies local artifact checksums, records
structural metrics and optionally exercises the conservative PDF parser on
cached reference PDFs. It refuses to overwrite an existing report. It does not
certify scientific fidelity.

Selection starts with a TWA anchor, then cycles through publication-era/topic
strata using deterministic ID ordering. Topic labels are title-based hints, not
scientific relevance or content-quality certification. Bibliography-linked
papers may discuss methods or unrelated comparison objects. Failures remain in
the selected denominator. Do not report a parser finishing as scientific fidelity
validation; inspect representative original and derived files separately.

See the [operator runbook](operator-runbook.md) for local live-validation results.
