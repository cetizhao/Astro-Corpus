"""MCP server exposing astro-pipeline to AI agents (Claude Code, Cursor, Cline, ...).

Five tools map onto the existing API surface:

  * lookup_source(gaia_id)          -- Corpus.lookup_source
  * lookup_paper(query)             -- Corpus.lookup_paper (arxiv_id/bibcode/title)
  * resolve_name(name)              -- intake.resolve_members(name=...)
  * cone_search(ra, dec, radius)    -- intake.resolve_members(cone=...)
  * summarize_corpus()              -- Corpus.summary

Run standalone::

    python -m astro_pipeline.mcp_server

Or via uv from the repo root::

    uv run python -m astro_pipeline.mcp_server

Connect from Claude Code::

    claude mcp add astro-corpus -- uv run --directory <repo-path> python -m astro_pipeline.mcp_server

Configuration via environment variables (all optional):

    ASTRO_PIPELINE_DATA_DIR    default: data/twa
    ASTRO_PIPELINE_CATALOG     default: TWAxgdr3.csv

TWA corpus is loaded once at startup (~1s) and held in memory; subsequent
tool calls are milliseconds (offline queries) or seconds (online SIMBAD/Gaia).
"""
from __future__ import annotations

import logging
import math
import os
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from astro_pipeline.corpus import Corpus
from astro_pipeline.intake import resolve_members
from astro_pipeline.spectra import get_xp_spectrum_png

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("ASTRO_PIPELINE_DATA_DIR", "data/twa"))
CATALOG = Path(os.environ.get("ASTRO_PIPELINE_CATALOG", "TWAxgdr3.csv"))

# --- Module-level init: load corpus once at startup ---
if not CATALOG.exists():
    # Allow falling back to data-dir-relative path (common when agent cwd != repo root).
    alt = DATA_DIR / CATALOG.name
    if alt.exists():
        CATALOG = alt
if not CATALOG.exists():
    logger.warning(
        "Catalog %s not found; lookup_source/paper will return errors. "
        "Set ASTRO_PIPELINE_CATALOG or bootstrap via README §13.",
        CATALOG,
    )

corpus = Corpus(data_dir=DATA_DIR, catalog_path=CATALOG, offline=True)
mcp = FastMCP("astro-corpus")


# --------------------------------------------------------------------- tools
@mcp.tool()
def lookup_source(gaia_id: str) -> dict:
    """Look up astrometry, multimodal availability, and SIMBAD-curated paper list for a Gaia DR3 source.

    Parameters
    ----------
    gaia_id : Gaia DR3 source_id (19-digit string).

    Returns
    -------
    dict with keys: gaia_id, resolved, astrometry, multimodal, papers.
    Offline (uses cached TWA data + paper_edges); milliseconds.
    """
    return _json_safe(corpus.lookup_source(gaia_id))


@mcp.tool()
def lookup_paper(query: str) -> dict:
    """Look up a paper by arxiv_id, bibcode, or unique title substring.

    Returns the paper metadata plus the list of catalog sources it studies,
    each with its own astrometry and multimodal availability.

    Parameters
    ----------
    query : arxiv_id (e.g. "2305.03557"), bibcode (e.g. "2023AJ....165..269L"),
            or unique title substring (e.g. "ACRONYM"). For ambiguous titles
            the call fails -- refine the substring.

    Returns
    -------
    dict with keys: query, resolved, paper, sources.
    Offline; milliseconds.
    """
    return _json_safe(corpus.lookup_paper(query))


@mcp.tool()
def resolve_name(name: str) -> dict:
    """Resolve an astronomical source name via SIMBAD to a Gaia DR3 source_id and basic astrometry.

    Useful when the user gives a name (e.g. "TW Hya", "HD 109573", "V* TW Hya")
    rather than a numeric Gaia id. Pair with lookup_source to also get papers.

    Parameters
    ----------
    name : SIMBAD-resolvable object name.

    Returns
    -------
    dict of canonical Gaia DR3 fields (Gaia, RAICRS, DEICRS, Plx_gdr3,
    RUWE_gdr3, Gmag_gdr3, ...) plus quality-control extras. Empty /
    unresolved names return {"error": "unresolved: <name>"}.

    Live call (~3-5 s; hits SIMBAD and Gaia).
    """
    try:
        df = resolve_members(name=name)
    except LookupError as e:
        return {"error": str(e)}
    if df.empty:
        return {"error": f"unresolved: {name!r}"}
    return _json_safe(df.iloc[0].to_dict())


@mcp.tool()
def cone_search(ra: float, dec: float, radius_deg: float, limit: int = 50) -> list:
    """Cone search Gaia DR3 field sources; this does NOT select physical members.

    Parameters
    ----------
    ra, dec : ICRS center coordinates in degrees.
    radius_deg : Cone radius in degrees (use small radii, < 1 deg, to keep
                 result sizes manageable).
    limit : Cap on rows returned. Gaia's ROW_LIMIT is advisory and occasionally
            flaky; the server truncates if exceeded.

    Returns
    -------
    list of dicts (one per source), each shaped like resolve_name's return.

    Live call (~2-5 s).

    For members of a cluster/association, use search_membership_catalogs,
    preview_membership_catalog, then download_membership_corpus instead.
    """
    try:
        df = resolve_members(cone=(ra, dec, radius_deg), limit=limit)
    except LookupError as e:
        return [{"error": str(e)}]
    return [_json_safe(row.to_dict()) for _, row in df.iterrows()]


@mcp.tool()
def summarize_corpus() -> dict:
    """Return coverage stats for the currently loaded corpus (TWA by default).

    Useful for an agent to discover what data is available before deciding
    which other tools to call.
    """
    return _json_safe(corpus.summary())


@mcp.tool()
def gaia_xp_spectrum(gaia_id: str) -> dict:
    """Render the Gaia DR3 BP/RP spectrum for a source and return a file URL.

    The agent or user opens the returned URL (file:// path) to view the plot.
    Spectrum is fetched from cache (data_dir/gaia_xp_continuous/ or
    data_dir/gaia_xp_sampled/) -- XP_CONTINUOUS is preferred; falls back to
    XP_SAMPLED if only that is available. The rendered PNG is cached at
    data_dir/gaia_xp_png/<gaia_id>.png so repeated calls are free.

    Parameters
    ----------
    gaia_id : Gaia DR3 source_id (19-digit string).

    Returns
    -------
    dict with: url (file://), format ("continuous" | "sampled" | "cached"),
    cached (bool), gaia_id, bp_n_points, rp_n_points. Returns {"error": ...}
    if the source has no XP spectrum cached (download requires Gaia archive
    credentials -- see README §13.2).

    Note
    ----
    Live download from Gaia is not enabled by default; the tool reads from
    cache only. Use ``examples/twa_sample.py`` to populate the cache first.
    """
    result = get_xp_spectrum_png(gaia_id, DATA_DIR, allow_download=False)
    return _json_safe(result)


@mcp.tool()
def download_corpus(
    label: str,
    cone: Optional[list[float]] = None,
    name: Optional[str] = None,
    region: Optional[list] = None,
    catalog_path: Optional[str] = None,
    limit: int = 200,
    skip_simbad: bool = False,
    sleep_seconds: float = 0.3,
    enrich: bool = True,
    xp_sampled: bool = False,
    xp_continuous: bool = False,
) -> dict:
    """Download a corpus snapshot and persist to ``data/<label>/`` on disk.

    Exactly one of ``cone`` / ``name`` / ``region`` / ``catalog_path`` must
    be provided.

    Pipeline:
      1. Resolve members via Gaia DR3 (cone/name/region) or read user CSV.
      2. Query SIMBAD for paper bibliography per source (resumable).
      3. Optionally download Gaia XP Sampled/Continuous FITS (resumable).
      4. Render interactive viz HTML, including locally cached XP spectra.
    Output goes under ``data/<label>/`` (members.csv, members.parquet,
    paper_edges.parquet, simbad_refs.parquet, source_paper_viz.html).

    The build is interruptible and resumable: if interrupted during the
    SIMBAD phase, re-running with the same label picks up where it left
    off via the simbad_refs.parquet cache.

    Parameters
    ----------
    label : directory name under data/ (e.g. "orion", "tw_hya_region").
    cone : [ra_deg, dec_deg, radius_deg] for a Gaia cone search.
    name : SIMBAD-resolvable source name (e.g. "TW Hya").
    region : [center_name, radius_deg]; SIMBAD resolves the center and returns
        arbitrary Gaia field sources inside that cone. This is geometric and
        does NOT mean members of the named cluster/association. Use the three
        membership-catalogue tools for physical members.
    catalog_path : path to a CSV with Gaia ids OR RA/Dec columns.
    limit : cap on members (default 200; raise for bigger builds, but
        SIMBAD phase scales as ~0.3 s/source).
    skip_simbad : if True, stop after members are resolved (no paper edges,
        no viz). Useful for a quick "what's in this region" check.
    sleep_seconds : pause between SIMBAD requests (default 0.3, polite).
        Lower to 0.1 for ~3x speedup at small risk of SIMBAD throttle
        (auto-retries on failure). Set higher (0.5-1.0) if you want to be
        extra cautious.
    enrich : if False, skip per-bibcode metadata fetch (title/DOI/year).
        Source<->paper links are still built, but paper titles are empty.
        ~5-10x faster for dense fields. Use when you only need the linkage.
    xp_sampled : if True, query availability and download XP_SAMPLED FITS.
        Default False so a catalog request never triggers large downloads by
        surprise. Requires Gaia DataLink access; checkpointed and resumable.
    xp_continuous : if True, also download XP_CONTINUOUS coefficient FITS in
        small batches. Default False; usually slower than Sampled.

    Returns
    -------
    dict with: label, input_mode, n_members, n_paper_edges, n_unique_papers,
    out_dir, members_csv, paper_edges_parquet, viz_html, elapsed_seconds.
    On failure: dict with ``error`` key.

    Tip
    ----
    To peek at progress during a long build, run ``corpus_status(label)``
    from another agent session or CLI -- it reads the checkpoint files
    written every ~25 bibcodes.
    """
    from astro_pipeline.corpus_builder import build_corpus_snapshot, result_to_dict

    corpora_root = DATA_DIR.parent if DATA_DIR.name == "twa" else Path("data")
    out_dir = corpora_root / label

    try:
        result = build_corpus_snapshot(
            label=label,
            out_dir=out_dir,
            cone=tuple(cone) if cone else None,
            name=name,
            region=(region[0], float(region[1])) if region else None,
            catalog=catalog_path,
            limit=limit if limit and limit > 0 else None,
            skip_simbad=skip_simbad,
            sleep_seconds=sleep_seconds,
            enrich=enrich,
            xp_sampled=xp_sampled,
            xp_continuous=xp_continuous,
        )
    except (ValueError, LookupError) as e:
        return {"error": str(e), "label": label}
    return result_to_dict(result)


@mcp.tool()
def download_plan(
    label: str,
    input_mode: str,
    input_summary: str,
    expected_sources: int = 0,
    skip_simbad: bool = False,
    xp_sampled: bool = False,
    xp_continuous: bool = False,
) -> dict:
    """Preview stages, outputs and known risks without downloading anything.

    Use before a large or personalized corpus request. ``input_mode`` should
    describe the intended route (cone, region, catalog, membership_catalog or
    bulk_healpix); ``input_summary`` is a human-readable description. The plan
    is read-only and can be shown to the user before invoking a download tool.
    """
    from astro_pipeline.manifest import make_download_plan

    if not label.strip() or not input_mode.strip() or not input_summary.strip():
        return {"error": "label, input_mode and input_summary must not be empty"}
    return _json_safe(
        make_download_plan(
            label=label,
            input_mode=input_mode,
            input_summary=input_summary,
            expected_sources=expected_sources if expected_sources > 0 else None,
            skip_simbad=skip_simbad,
            xp_sampled=xp_sampled,
            xp_continuous=xp_continuous,
        )
    )


@mcp.tool()
def search_membership_catalogs(target: str, max_results: int = 10) -> dict:
    """Find published VizieR member catalogues for a cluster or association.

    This is the correct first step when a user asks for "Taurus members",
    "Pleiades members", etc. It is intentionally separate from a Gaia cone
    search, which only returns all field sources in a geometric region.

    Returns ranked catalogue candidates with catalog_id, title, relevance
    signals, source URL, and a reminder to preview before choosing. The tool
    does not automatically decide which scientific membership definition is
    correct and does not apply hidden quality/probability cuts.

    Live call to CDS VizieR. Next call preview_membership_catalog(catalog_id).
    """
    from astro_pipeline.membership import search_membership_catalogs as _search

    try:
        return _json_safe(_search(target, max_results=max_results))
    except (ValueError, LookupError) as exc:
        return {"error": str(exc), "target": target}


@mcp.tool()
def preview_membership_catalog(
    catalog_id: str,
    table_id: Optional[str] = None,
    sample_size: int = 8,
) -> dict:
    """Inspect tables, columns and sample rows before choosing a member list.

    A VizieR catalogue can contain separate established-member and candidate
    tables. The preview labels those roles, detects Gaia IDs/coordinates and
    membership-related columns, and explains the planned Gaia DR3 match.

    Parameters
    ----------
    catalog_id : e.g. "J/AJ/156/271".
    table_id : optional exact table, e.g. "J/AJ/156/271/table1". Omit to
        preview all tables in the catalogue.
    sample_size : 1-50 rows per table; preview never downloads the full table.
    """
    from astro_pipeline.membership import preview_membership_catalog as _preview

    try:
        return _json_safe(
            _preview(catalog_id, table_id=table_id, sample_size=sample_size)
        )
    except (ValueError, LookupError) as exc:
        return {"error": str(exc), "catalog_id": catalog_id, "table_id": table_id}


@mcp.tool()
def download_membership_corpus(
    label: str,
    catalog_id: str,
    table_id: Optional[str] = None,
    limit: int = 0,
    match_radius_arcsec: float = 3.0,
    skip_simbad: bool = False,
    sleep_seconds: float = 0.3,
    enrich: bool = True,
    xp_sampled: bool = False,
    xp_continuous: bool = False,
) -> dict:
    """Build a Gaia DR3 corpus from an explicitly selected member table.

    Use only after search_membership_catalogs and preview_membership_catalog.
    The full selected VizieR table is saved unchanged, then matched to Gaia
    DR3 by source_id when available or by batched coordinates otherwise.
    Literature columns (membership probability, population, spectral type,
    flags, names, etc.) are preserved with a ``member_`` prefix. No scientific
    filtering is silently applied.

    Set xp_sampled/xp_continuous to download only the Gaia products marked
    available for matched members. Both are off by default and resumable.

    Outputs under data/<label>/ include membership_catalog.csv/parquet,
    members.csv/parquet, unmatched_members.csv and, unless skip_simbad=True,
    paper edges plus the self-contained HTML visualization.

    ``limit=0`` means all selected table rows; use a small positive value for
    a test. ``match_radius_arcsec`` defaults to 3 arcsec because older member
    lists can use pre-Gaia-DR3 epochs; inspect match separations and unmatched
    rows, especially in dense fields or for high-proper-motion sources.
    """
    from astro_pipeline.membership import build_membership_corpus

    corpora_root = DATA_DIR.parent if DATA_DIR.name == "twa" else Path("data")
    try:
        return _json_safe(
            build_membership_corpus(
                label=label,
                out_dir=corpora_root / label,
                catalog_id=catalog_id,
                table_id=table_id,
                limit=limit if limit > 0 else None,
                match_radius_arcsec=match_radius_arcsec,
                skip_simbad=skip_simbad,
                sleep_seconds=sleep_seconds,
                enrich=enrich,
                xp_sampled=xp_sampled,
                xp_continuous=xp_continuous,
            )
        )
    except (ValueError, LookupError) as exc:
        return {
            "error": str(exc),
            "label": label,
            "catalog_id": catalog_id,
            "table_id": table_id,
        }


@mcp.tool()
def corpus_status(label: str) -> dict:
    """Report build progress for a corpus by reading its checkpoint files.

    Useful both during a long ``download_corpus`` build (call from another
    agent session or CLI) and after the fact to summarize what's in a
    corpus without loading it.

    Parameters
    ----------
    label : directory name under data/ (e.g. "orion").

    Returns
    -------
    dict with: label, path, exists, n_members, simbad_phase (one of
    "not_started" | "in_progress" | "done"), n_sources_queried,
    n_unique_bibcodes, n_enriched (bibcodes with title/DOI), n_with_arxiv,
    has_paper_edges, has_viz, members_modified (timestamp),
    simbad_modified (timestamp).

    The SIMBAD phase is detected as ``in_progress`` when the simbad cache
    exists but no final paper_edges.parquet is written yet.
    """
    import pandas as pd

    corpora_root = DATA_DIR.parent if DATA_DIR.name == "twa" else Path("data")
    path = corpora_root / label

    if not path.exists():
        return {"label": label, "exists": False, "error": f"no corpus at {path}"}

    out: dict = {"label": label, "path": str(path.resolve()), "exists": True}

    # New builds expose exact per-stage progress in an atomic manifest. Keep
    # the legacy file-derived fields below for older corpora and convenience.
    from astro_pipeline.manifest import read_manifest

    manifest = read_manifest(path / "manifest.json")
    if manifest is not None:
        out["manifest"] = manifest
        out["build_status"] = manifest.get("status")
        out["stages"] = manifest.get("stages", {})
        out["requested_products"] = manifest.get("requested_products", {})
        out["manifest_modified"] = _iso_mtime(path / "manifest.json")

    # Members
    members_csv = path / "members.csv"
    members_parquet = path / "members.parquet"
    if members_parquet.exists():
        out["n_members"] = len(pd.read_parquet(members_parquet))
        out["members_modified"] = _iso_mtime(members_parquet)
    elif members_csv.exists():
        out["n_members"] = len(pd.read_csv(members_csv))
        out["members_modified"] = _iso_mtime(members_csv)
    else:
        out["n_members"] = 0

    # SIMBAD progress
    simbad_path = path / "simbad_refs.parquet"
    edges_path = path / "paper_edges.parquet"
    has_edges = edges_path.exists()

    if not simbad_path.exists() and not has_edges:
        out["simbad_phase"] = "not_started"
    elif has_edges:
        out["simbad_phase"] = "done"
    else:
        out["simbad_phase"] = "in_progress"

    if simbad_path.exists() or has_edges:
        src = simbad_path if simbad_path.exists() else edges_path
        try:
            df = pd.read_parquet(src)
            n_src = int(df["gaia_id"].nunique()) if "gaia_id" in df.columns else None
            out["n_sources_queried"] = n_src
            out["n_unique_bibcodes"] = int(df["bibcode"].nunique()) if "bibcode" in df.columns else 0
            if "title" in df.columns:
                titles = df.groupby("bibcode")["title"].first()
                out["n_enriched"] = int((titles.astype(str).str.strip() != "").sum())
            if "arxiv_id" in df.columns:
                arxiv = df.groupby("bibcode")["arxiv_id"].first()
                out["n_with_arxiv"] = int((arxiv.astype(str).str.strip() != "").sum())
            out["simbad_modified"] = _iso_mtime(src)
            # If no members file (TWA-style), infer n_members from edges
            if not out.get("n_members") and n_src:
                out["n_members"] = n_src
        except Exception as e:
            out["simbad_read_error"] = str(e)

    out["has_paper_edges"] = has_edges
    out["has_viz"] = (path / "source_paper_viz.html").exists()
    return out


def _iso_mtime(p: Path) -> str:
    """ISO-formatted modification time of a file."""
    import datetime
    return datetime.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")


@mcp.tool()
def list_corpora() -> list[dict]:
    """List all corpus snapshots under the data root.

    Each corpus is a subdirectory of ``data/`` containing at least a
    ``members.csv``, ``members.parquet``, or ``paper_edges.parquet`` file
    (the TWA demo uses ``paper_edges.parquet`` without a members file).

    Returns
    -------
    list of dicts, one per corpus, with: name, path, n_members (if known),
    has_paper_edges, has_viz.
    """
    import pandas as pd

    corpora_root = DATA_DIR.parent if DATA_DIR.name == "twa" else Path("data")
    out: list[dict] = []

    if not corpora_root.exists():
        return out

    for sub in sorted(corpora_root.iterdir()):
        if not sub.is_dir():
            continue
        members_csv = sub / "members.csv"
        members_parquet = sub / "members.parquet"
        has_edges = (sub / "paper_edges.parquet").exists()
        if not (members_csv.exists() or members_parquet.exists() or has_edges):
            continue
        n_members: Optional[int] = None
        try:
            if members_parquet.exists():
                n_members = len(pd.read_parquet(members_parquet))
            elif members_csv.exists():
                n_members = len(pd.read_csv(members_csv))
            elif has_edges:
                # TWA-style: members count = unique source_id in edges
                n_members = int(pd.read_parquet(sub / "paper_edges.parquet")["source_id"].nunique())
        except Exception:
            pass
        out.append({
            "name": sub.name,
            "path": str(sub.resolve()),
            "n_members": n_members,
            "has_paper_edges": has_edges,
            "has_viz": (sub / "source_paper_viz.html").exists(),
        })
    return out


# --------------------------------------------------------------------- helpers
def _json_safe(obj: Any) -> Any:
    """Convert pandas/numpy scalars to native Python so MCP JSON serialization works."""
    import numpy as np
    import pandas as pd

    if obj is None:
        return None
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        v = float(obj)
        return None if math.isnan(v) else v
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (pd.Timestamp,)):
        return obj.isoformat()
    if isinstance(obj, float):
        return None if math.isnan(obj) else obj
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(x) for x in obj]
    return obj


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logger.info(
        "astro-corpus MCP server starting (data_dir=%s, catalog=%s)",
        DATA_DIR, CATALOG,
    )
    mcp.run()


if __name__ == "__main__":
    main()
