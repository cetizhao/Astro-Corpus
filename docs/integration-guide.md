# MCP integration and usage guide

This guide is for a new collaborator or an AI agent connecting to the pipeline
for the first time. Users normally describe the desired sample in natural
language; the agent chooses and calls the tools below. For a conceptual explanation
of the stack, storage, and MCP boundaries, read the Chinese
[technical overview](technical-overview.md).

## Install and connect

Install `uv` and a suitable Python runtime (Python 3.11 or newer), then clone a
branch/commit containing the features you intend to use:

```bash
git clone <repo-url> astro-pipeline
cd astro-pipeline
uv sync --locked
uv run --locked pytest -q
```

The explicit destination avoids assuming that the GitHub repository name is
`astro-pipeline`. A private repository requires access. Cloning copies committed
code, not another developer's uncommitted changes, generated data, or credentials.

Codex uses the project-scoped `.codex/config.toml` after the repository is
trusted. It starts the STDIO server with:

```toml
[mcp_servers.astro-corpus]
command = "uv"
args = ["run", "python", "-m", "astro_pipeline.mcp_server"]
startup_timeout_sec = 30
tool_timeout_sec = 600
enabled = true
```

Save MCP settings and restart Codex; use `/mcp` to verify that `astro-corpus`
is active. MCP tool definitions are loaded into a task's tool context, so after
adding or changing tools, restart Codex and reopen the task (start a new task if
the existing task still shows the old tool list).

The root `.mcp.json` provides the equivalent project-relative configuration for
clients that read that convention, including Claude Code:

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

An MCP client that reads either project convention can clone the repository and
start from its root. Other clients use the equivalent command with the repo as
their working directory:

```text
uv run python -m astro_pipeline.mcp_server
```

The server starts even when the optional TWA demo files are absent. In that
case cached `lookup_source`, `lookup_paper`, and `summarize_corpus` are not
useful yet, but live search and all corpus-building tools remain available.
Global `lookup_library_source`, `lookup_library_paper`, and `library_status`
remain read-only; if the shared database is absent they return structured empty
state and do not create or migrate it.

## Claude Code scopes and personal reuse

The repository supplies **project-scoped** configuration: `.mcp.json` for Claude
Code and `.codex/config.toml` for Codex. The Python server itself is not inherently
project- or user-scoped; scope belongs to the client's registration.

In Claude Code, the scopes differ as follows:

| Scope | Availability | Shared through this repository |
|---|---|---|
| `local` | One user's configuration for the current project | No |
| `project` | The current project, subject to each user's approval | Yes, `.mcp.json` |
| `user` | One user's sessions across projects | No, personal configuration |

This describes the checked-in configuration, not every personal registration on
a machine. If a same-name registration already exists, inspect its effective
scope and command before adding another one.

### Use the checked-in project configuration

From the prepared repository root, start Claude Code:

```bash
claude
```

Review and approve the project MCP server when prompted. In the session, run
`/mcp` to inspect connection status and the tools actually loaded. For command-line
configuration inspection, use:

```bash
claude mcp get astro-corpus
claude mcp list
```

The client starts the stdio child process; there is no separate HTTP endpoint to
start first. A server launched manually in a terminal waits for MCP protocol
messages, not ordinary natural-language input. If the tool list is stale after a
code update, restart/reconnect the server and open a fresh client session.

### Make it available in your other projects

The following is an **optional configuration-changing command**, not a step
required for project-scoped use. Replace the placeholder with your own absolute
checkout path; this example uses POSIX/WSL shell syntax:

```bash
claude mcp add --scope user --transport stdio astro-corpus -- \
  uv --directory /absolute/path/to/astro-pipeline \
  run --locked python -m astro_pipeline.mcp_server
```

This registers the server in that user's Claude Code configuration; it does not
edit the repository, install the tool for other operating-system users, or expose
it over the network. The checkout must remain at the configured path.

`uv --directory` is important: it fixes both project discovery and the server's
working directory. Merely pointing at a Python executable or using `--project`
is not equivalent to changing the working directory. Relative `config/`, `data/`,
and default cached-corpus paths would otherwise depend on the caller's project.
With this example, calls from other projects still use the configured checkout's
local data and configuration. User scope is **not** an independent database per
conversation or a multi-user authorization boundary.

For another client that supports local stdio commands but not repository MCP
configuration, enter the equivalent command/arguments in that client's own
settings. A JSON-shaped example is:

```json
{
  "mcpServers": {
    "astro-corpus": {
      "command": "uv",
      "args": [
        "--directory", "/absolute/path/to/astro-pipeline",
        "run", "--locked", "python", "-m", "astro_pipeline.mcp_server"
      ]
    }
  }
}
```

Not all clients use this JSON schema. Use the client's documented format, and
ensure that its process can find `uv`; GUI applications may require an absolute
path to the `uv` executable. Clients supporting only a remote MCP URL cannot
launch this stdio server directly. Installing Tailscale on a user's computer does
not automatically give a cloud-hosted connector access to that private network.

Claude Code references:

- [MCP quickstart](https://code.claude.com/docs/en/mcp-quickstart.md)
- [MCP reference](https://code.claude.com/docs/en/mcp.md)

## First-use checks and collaborator data

A safe first prompt is:

> Use astro-corpus to call library_status and list_corpora. Report local coverage
> only; do not start a download.

A new clone normally has no generated corpus or SQLite library. Structured empty
state is expected, not a failed installation. The old `lookup_source`,
`lookup_paper`, and `summarize_corpus` tools use one configured cached corpus;
prefer the global `lookup_library_source`, `lookup_library_paper`, and
`library_status` tools when asking about the shared local library.

A deliberately bounded acquisition example, **only when a download is wanted**:

```text
download_corpus(
    label="tw_hya_smoke",
    name="TW Hya",
    limit=1,
    skip_simbad=true,
    xp_sampled=false,
    xp_continuous=false
)
```

This is MCP-call notation, not executable Python. It accesses external services
and writes local output. For physical association members, use the search,
preview, explicit-table-selection workflow below instead of a named-region cone.

Users configure their own Gaia credentials in gitignored `config/local.yaml` or
`GAIA_USERNAME` / `GAIA_PASSWORD` when needed for DataLink. Do not copy another
person's credentials into MCP settings or Git. Anonymous astrometric TAP queries
remain separate from authenticated product requests.

There are three different sharing models:

| Goal | Mechanism | Where the data lives |
|---|---|---|
| Reuse the same tools | Clone code, install dependencies, run local MCP | Each collaborator's machine |
| Share a fixed dataset | Export a selected immutable run; use `--standalone` for independent pinned products | A separate snapshot copy |
| Update one shared main library | Deploy a remotely reachable, authenticated and authorized MCP service | One server |

The first two are supported by the current local tooling; see [library.md](library.md)
for export details. The third is a deployment project, not a property obtained by
sharing `.mcp.json`. Current stdio configuration has no remote endpoint, user
accounts, per-user authorization, quotas, or persistent job queue. Long downloads
may also exceed a client's tool timeout; use bounded tasks and inspect run status,
rather than assuming a timeout means that all server work was rolled back.

The viewer on loopback port 8765 is read-only HTTP, **not remote MCP**. Exposing
that port does not grant remote agents the download tools. A remote deployment
also needs controlled artifact access: local absolute paths and `file://` URLs
returned by the current tools do not automatically work on another computer.
Keep SQLite writers on the server's local filesystem, not on a client-mounted
SMB/OneDrive copy. See the [technical overview](technical-overview.md) for migration
and server-boundary caveats.

## Sharing the project through GitHub

GitHub can distribute the source, portable MCP configuration, dependency lockfile,
tests, and documentation. It does not automatically install dependencies, approve
local tool execution, host a running MCP server, or share the author's database.
GitHub Pages serves static content; it cannot run this Python/SQLite write service.
A private repository with authorized collaborators is also a valid way to share
the code without making it public.

Before publishing code:

- Confirm the right to publish and choose a project license. As of the
  2026-09-30 documentation check, no root project LICENSE/COPYING/NOTICE file was
  found; public visibility alone is not an open-source license.
- Retain third-party license notices, including vendored assets. Code licensing
  does not replace archive/catalogue redistribution terms or citation duties.
- Exclude personal credentials/configuration, environment files, private logs,
  SQLite/WAL files, cached objects, and generated FITS/Parquet/CSV/JSONL data.
  The default `data/` tree is ignored; custom locations need their own review.
- Review tracked files and Git history, not just `.gitignore`: ignore rules do
  not remove previously committed secrets or data. This guide is not a completed
  historical secrets audit.
- Keep project MCP configuration portable and free of personal absolute paths or
  embedded credentials. Each user must review and approve commands from a clone.
- Validate a clean installation, offline tests, and MCP startup without the
  optional demo data. Publish the actual commit containing the desired features;
  local uncommitted changes are not included in another person's clone.

Updating these instructions does not publish code, change repository visibility,
add collaborators, or choose a license. Those are separate maintainer actions.

## Choose the correct input workflow

| User intent | MCP workflow | Meaning |
|---|---|---|
| A source/paper across all local corpora | `library_status` → `lookup_library_source` or `lookup_library_paper` | Offline global index; current published snapshots only |
| A coordinate/sky patch | `cone_search` or `download_corpus(cone=...)` | Every Gaia field source in a geometric cone |
| A named sky center | `download_corpus(region=[name, radius])` | SIMBAD resolves the center; still field sources, not members |
| One named object | `resolve_name` or `download_corpus(name=...)` | One SIMBAD-resolved Gaia DR3 source |
| An uploaded source list | `download_corpus(catalog_path=...)` | Gaia IDs or RA/Dec in a local CSV |
| Members of a cluster/association | Search → preview → membership download | Published membership definition selected explicitly |
| A large part or all of `gaia_source` | Bulk plan → bounded worker → status | HEALPix-partitioned Parquet dataset |
| Inspect upcoming DR4 support | Probe → describe table → list products | Live Archive facts kept separate from provisional profile hints |
| Explicit DataLink product list | Product plan → bounded download | Release-qualified product artifacts with checkpoint/provenance |

The distinction between a region and membership is scientifically important.
For example, `region=["Taurus", 2]` returns foreground/background field stars
as well as Taurus members. It must not be described as a Taurus member list.
Cached release-qualified sources are not evidence of complete local sky
coverage: cone, region, and coordinate-match selections still query Gaia.

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

## Corpus scope and global library

High-level corpus and membership builds use the shared library by default.
`LibraryConfig.enabled` may disable it; `ASTRO_PIPELINE_LIBRARY_DIR` overrides
`library.root`, which otherwise defaults to `data/_library`.

A build retains original input rows, matches, duplicate same-star occurrences,
and unmatched rows in its run snapshot while deduplicating reusable source and
product identities globally. It writes `runs/<run_id>/` plus top-level
compatibility views. `active_run_id` selects the run published at the label root;
it does not guarantee a `completed` build, because a later-stage failure after
member indexing can still leave that run active. Inspect terminal status, stages,
and errors. A manifest with `snapshot_run_id` is already pinned. Shared product
references name exact immutable versions rather than "whatever is newest".

The existing `lookup_source`, `lookup_paper`, `summarize_corpus`, and `Corpus`
API remain scoped to one configured corpus. The three `lookup_library_*`/status
tools query across current published runs without changing storage. Full cache,
refresh, CLI, import/export, and backup semantics are documented in
[library.md](library.md).

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

Fresh exact-ID, XP-availability, product, and literature observations are reused
across labels. Positive observations default to 30 days and explicit negatives
to one day; failures stay retryable. Python builds and the `download_corpus` /
`download_membership_corpus` MCP tools expose `refresh=True` to refresh current
observations while preserving historical snapshots. Do not combine refresh with
an explicitly resumed frozen membership selection.

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

`download_plan(gaia_release="dr4")` is currently read-only and returns
`executable=false`. Ordinary DR4 corpus normalization remains disabled until
the final column model is published; use the release-discovery and generic
DataLink tools instead.

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

DR4 bulk execution is currently disabled even if a caller supplies a guessed
`gaiadr4` schema. Run `probe_gaia_archive(release="dr4")` first; the source-ID
partition strategy must additionally be confirmed from the published data
model before a DR4 plan becomes executable.

## Release and DataLink workflow

1. `probe_gaia_archive(release="dr4")` checks live `TAP_SCHEMA` without
   guessing a schema.
2. `describe_gaia_table(release, table)` retrieves live column metadata only.
3. `list_gaia_products(release)` returns profile/astroquery capability hints;
   provide `source_id` to confirm source-specific links from the live service.
4. `plan_gaia_product_download(...)` validates format, structure, product and
   batch count without writing.
5. `download_gaia_product(...)` accepts at most 5,000 explicit numeric IDs and
   writes under `data/gaia_products/<label>/`.
6. `gaia_product_status(label)` reads local request/checkpoint/artifact progress.

Supported table-oriented output formats are VOTable, VOTable Plain, FITS, CSV,
and ECSV. Data structures are `INDIVIDUAL` and `RAW`; the deprecated
`COMBINED` structure is intentionally absent. `CROWDED_FIELD_IMAGE` remains
blocked because its binary image payload needs a blob-preserving transport.

## Paper full-text workflow

Paper downloads are opt-in and separate from source bibliography enrichment.
Use `download_paper_corpus(label, papers=[{"arxiv_id": "2305.03557"}])` for
1–50 explicit arXiv IDs/records, followed by `paper_corpus_status(label)` and
`lookup_library_paper(query)`. `papers` may also be a list of ID strings.

The downloader pins exact arXiv versions, preserves raw files and provenance,
and derives structured JSON/Markdown locally. HTML is preferred, LaTeX source
is conservative, and PDF text is always lower-fidelity/partial. Per-paper
warnings and `acquisition_complete` matter even when text is present. Repeating
the same request resumes the pinned run; `refresh=True` creates a new run.
An optional `resume_run_id` must identify the active run and cannot accompany
refresh. A reference PDF is requested by default (`include_pdf=False` opts out).

A bibliography edge does not prove which paragraph discusses a Gaia source.
License evidence is retained but training/redistribution permission is not
inferred. No document text is sent to an external model or parsing service.
Full commands, schema and limits: [paper corpus guide](paper-corpus.md).
Restart existing MCP server processes after upgrading to load the new tools.

## Tool reference

The server currently registers 27 tools.

| Tool | Network | Writes data | Purpose |
|---|---|---|---|
| `lookup_library_source` | no | no | Global lookup by `(release, source_id)` with datasets, active bibliography, XP availability, and products |
| `lookup_library_paper` | no | no | Global paper lookup with active source links and cached full-text versions/paths |
| `download_paper_corpus` | yes | yes | Explicit 1–50-paper arXiv full-text collection with version pins and quality flags |
| `paper_corpus_status` | no | no | Read local paper-collection checkpoints and artifact paths |
| `library_status` | no | no | Read global SQLite coverage and paths without creating it |
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
| `probe_gaia_archive` | yes | no | Discover release schema/tables via TAP_SCHEMA |
| `describe_gaia_table` | yes | no | Read live table column metadata |
| `list_gaia_products` | optional | no | Profile hints; source ID enables live DataLink confirmation |
| `plan_gaia_product_download` | no | no | Validate a release-qualified product plan |
| `download_gaia_product` | yes | yes | Download explicit bounded DataLink identifiers |
| `gaia_product_status` | no | no | Read generic DataLink progress and provenance |

## Known warnings an agent should surface

- Gaia, VizieR, SIMBAD, and arXiv are external services; throttling, 503s, and
  queued TAP jobs can occur. Retry/checkpoint support does not make them local.
- A populated library is not a complete mirror of Gaia and cannot answer a new
  cone/region selection without a Gaia query.
- Shared work uses bounded leases and fencing. A busy error means another worker
  retained the same key beyond the wait budget; retry later rather than bypassing
  the shared state. Product-view checks and replacements share a short fenced
  transaction; a stale owner cannot overwrite the next owner's metadata.
- Managed exports use an immutable selected run, including by default. Use
  `--run-id` for an exact historical run and `--standalone` for an independent
  copy of its pinned products. Pending publication needs explicit inspection or
  rerun; verification does not automatically activate a prepared run.
- Gaia DataLink login can fail while anonymous TAP queries still work. The
  pipeline deliberately keeps astrometry queries anonymous.
- XP download failure does not invalidate the members/astrometry table. Report
  XP as partial and resume later.
- Membership catalogues can disagree scientifically. The pipeline ranks and
  previews candidates but does not decide which paper is authoritative.
- An HTML file is self-contained only when ECharts was successfully inlined;
  check the manifest warning when the vendored asset is absent.
