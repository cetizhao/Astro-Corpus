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
same source selection to resume; optional products may be added. A different
target, radius, `limit`, membership table, or uploaded-CSV content under the
same label is rejected. `partial` usually means an optional XP or visualization
stage needs another run; inspect `warnings`, `errors`, and the per-stage status.

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
| Region result is not cluster members | Region is a geometric cone | Use the membership search/preview/download workflow |
| `Gaia.login`/DataLink returns 503 | Product service unavailable | Keep TAP anonymous; resume XP later |
| TAP job remains queued | Archive load, especially upload jobs | Current membership matcher uses ordinary small-cone ADQL; retry later |
| XP stage is `partial` | Some products failed or were unavailable | Inspect availability and checkpoint; rerun same label |
| One membership row is unmatched | No Gaia source inside radius or bad/old coordinates | Review `unmatched_members.csv`; consider epoch/proper motion and radius |
| Multiple match candidates | Dense field or wide radius | Review `membership_match_candidate_count` and separation |
| Blank HTML / ECharts warning | Vendored JavaScript is missing/corrupt | Restore `src/astro_pipeline/viz/vendor/echarts.min.js` and rebuild |
| Bulk plan is `partial` | Worker stopped at first failed pixel | Inspect `last_error`; rerun same label |
| Label rejected | Labels are directory names, not paths | Use letters/digits plus `.`, `_`, or `-` only |

## Data sharing and Git

The repository intentionally ignores generated catalogues and spectra. A clone
gets code and documentation, not local corpus data. Share a corpus directory
through an approved data channel, or let the collaborator rebuild it from its
manifest inputs. Do not force-add credentials, proprietary files, or large
generated datasets into Git.
