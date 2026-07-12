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
from typing import Any

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
    """Cone search Gaia DR3 around a sky position; return canonical fields per source.

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
