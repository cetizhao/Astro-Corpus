"""Build a complete corpus snapshot from any input mode, persisted to disk.

Wraps :mod:`astro_pipeline.intake` (resolve members) +
:mod:`astro_pipeline.sources.simbad_refs` (paper edges) +
:mod:`astro_pipeline.viz` (HTML overview) into a single orchestrator.

Used by:
  * ``scripts/build_corpus.py``  -- CLI wrapper
  * ``mcp_server.download_corpus`` -- MCP tool

Inherits retry + checkpoint behavior from underlying modules:
  * Network calls in ``intake`` go through GaiaSource / astroquery (with retry)
  * SIMBAD per-source queries use an incremental parquet cache at
    ``<out_dir>/simbad_refs.parquet`` -- interruptible and resumable
  * Output files are written atomically per stage

The high-level entry point :func:`build_corpus_snapshot` returns a
:dataclass:`CorpusBuildResult` describing what was built and where.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from astro_pipeline.intake import resolve_members
from astro_pipeline.io.writer import save_catalog_parquet
from astro_pipeline.sources.simbad_refs import build_paper_edges

logger = logging.getLogger(__name__)


@dataclass
class CorpusBuildResult:
    """Outcome of a corpus build. Serializable to dict for MCP/CLI output."""

    label: str
    input_mode: str  # "cone" | "name" | "region" | "catalog"
    input_summary: str
    n_members: int
    n_paper_edges: int
    n_unique_papers: int
    out_dir: str
    members_csv: str = ""
    members_parquet: str = ""
    paper_edges_parquet: str = ""
    simbad_refs_parquet: str = ""
    viz_html: str = ""  # file:// URL or ""
    skipped_simbad: bool = False
    elapsed_seconds: float = 0.0
    error: Optional[str] = None


def build_corpus_snapshot(
    *,
    label: str,
    out_dir: Path | str,
    cone: Optional[tuple[float, float, float]] = None,
    name: Optional[str] = None,
    region: Optional[tuple[str, float]] = None,
    catalog: Optional[str | Path] = None,
    limit: Optional[int] = None,
    skip_simbad: bool = False,
    sleep_seconds: float = 0.3,
    enrich: bool = True,
) -> CorpusBuildResult:
    """Build a corpus snapshot from any input mode and persist to disk.

    Exactly one of ``cone`` / ``name`` / ``region`` / ``catalog`` must be
    provided. Output layout under ``out_dir``::

        members.csv             # input to corpus pipeline
        members.parquet         # same, parquet
        simbad_refs.parquet     # SIMBAD bibliography cache (resumable)
        paper_edges.parquet     # source<->paper edge table
        source_paper_viz.html   # interactive overview

    Returns a :class:`CorpusBuildResult`. On failure (e.g., no members
    resolved), ``error`` is set and file paths are empty.

    Parameters
    ----------
    label : human-readable name for this corpus (used in logging + result).
    out_dir : directory to write artifacts. Created if missing.
    cone : ``(ra_deg, dec_deg, radius_deg)``.
    name : SIMBAD-resolvable source name (e.g. ``"TW Hya"``).
    region : ``(center_name, radius_deg)`` -- SIMBAD resolves the center,
        then a Gaia cone search around it.
    catalog : path to a CSV with a Gaia source_id column OR RA/Dec columns.
    limit : cap on members. Cone/region queries can return thousands; this
        prevents accidental huge downloads and long SIMBAD phases.
    skip_simmad : if True, stop after ``members.*`` are written (no SIMBAD,
        no viz, no edges). Useful for quick "what's in this region" checks.
    sleep_seconds : pause between SIMBAD requests. Default 0.3 is polite;
        lower to 0.1 for ~3x speedup at risk of occasional SIMBAD throttle
        (the retry decorator recovers automatically).
    enrich : if False, skip per-bibcode metadata fetch (title/DOI/year).
        Bibcodes are still linked to sources, but the paper list has empty
        titles. ~5-10x faster for dense fields. Use when you only need the
        source<->paper linkage, not human-readable paper metadata.
    """
    inputs = {"cone": cone, "name": name, "region": region, "catalog": catalog}
    provided = [k for k, v in inputs.items() if v is not None]
    if len(provided) != 1:
        raise ValueError(
            f"Exactly one of cone/name/region/catalog required; got {provided or 'none'}."
        )
    input_mode = provided[0]

    t0 = time.time()
    out_dir = Path(out_dir)

    if cone:
        ra, dec, radius = cone
        input_summary = f"cone(ra={ra}, dec={dec}, radius={radius}deg)"
    elif name:
        input_summary = f"name={name!r}"
    elif region:
        center, radius = region
        input_summary = f"region(center={center!r}, radius={radius}deg)"
    else:  # catalog
        input_summary = f"catalog={Path(catalog).name}"

    logger.info("Building corpus %r: %s", label, input_summary)

    # --- 1. Resolve members ---
    try:
        members = resolve_members(
            cone=cone, name=name, region=region, catalog=catalog, limit=limit,
        )
    except LookupError as e:
        return CorpusBuildResult(
            label=label, input_mode=input_mode, input_summary=input_summary,
            n_members=0, n_paper_edges=0, n_unique_papers=0,
            out_dir=str(out_dir), skipped_simbad=skip_simbad,
            elapsed_seconds=time.time() - t0, error=str(e),
        )

    logger.info("Resolved %d members", len(members))

    if members.empty:
        return CorpusBuildResult(
            label=label, input_mode=input_mode, input_summary=input_summary,
            n_members=0, n_paper_edges=0, n_unique_papers=0,
            out_dir=str(out_dir), skipped_simbad=skip_simbad,
            elapsed_seconds=time.time() - t0,
            error="no members resolved for the given input",
        )

    # --- 2. Save members ---
    out_dir.mkdir(parents=True, exist_ok=True)
    members_csv = out_dir / "members.csv"
    members_parquet = out_dir / "members.parquet"
    members.to_csv(members_csv, index=False)
    save_catalog_parquet(members, members_parquet)

    if skip_simbad:
        logger.info("--skip-simbad; stopping after members.*")
        return CorpusBuildResult(
            label=label, input_mode=input_mode, input_summary=input_summary,
            n_members=len(members), n_paper_edges=0, n_unique_papers=0,
            out_dir=str(out_dir.resolve()),
            members_csv=str(members_csv.resolve()),
            members_parquet=str(members_parquet.resolve()),
            skipped_simbad=True,
            elapsed_seconds=time.time() - t0,
        )

    # --- 3. SIMBAD paper edges (inherits resumable cache via cache_path) ---
    logger.info(
        "Querying SIMBAD for paper bibliography per source "
        "(sleep=%.2fs, enrich=%s; interruptible, resumable)...",
        sleep_seconds, enrich,
    )
    edges, refs = build_paper_edges(
        members,
        id_col="Gaia",
        cache_path=out_dir / "simbad_refs.parquet",
        sleep_seconds=sleep_seconds,
        enrich=enrich,
    )
    edges_path = out_dir / "paper_edges.parquet"
    edges.save(edges_path)

    # --- 4. Viz HTML ---
    # inline_echarts=True so the HTML is self-contained (no sibling file dep).
    # The ~1 MB cost is acceptable and avoids the "blank page" failure mode
    # when the corpus dir is moved/shared without echarts.min.js.
    from astro_pipeline.viz import build_overview_html

    viz_path = out_dir / "source_paper_viz.html"
    echarts_src = out_dir.parent / "twa" / "echarts.min.js"
    build_overview_html(
        output_path=viz_path,
        catalog_path=members_csv,
        edges_path=edges_path,
        corpus_name=label,
        inline_echarts=True,
        echarts_path=echarts_src if echarts_src.exists() else None,
    )

    n_papers = int(refs["bibcode"].nunique()) if not refs.empty else 0
    elapsed = time.time() - t0
    logger.info(
        "Corpus %r built: %d members, %d edges, %d papers (%.1fs)",
        label, len(members), len(edges), n_papers, elapsed,
    )

    return CorpusBuildResult(
        label=label,
        input_mode=input_mode,
        input_summary=input_summary,
        n_members=len(members),
        n_paper_edges=len(edges),
        n_unique_papers=n_papers,
        out_dir=str(out_dir.resolve()),
        members_csv=str(members_csv.resolve()),
        members_parquet=str(members_parquet.resolve()),
        paper_edges_parquet=str(edges_path.resolve()),
        simbad_refs_parquet=str((out_dir / "simbad_refs.parquet").resolve()),
        viz_html=f"file://{viz_path.resolve()}",
        skipped_simbad=False,
        elapsed_seconds=elapsed,
    )


def result_to_dict(result: CorpusBuildResult) -> dict[str, Any]:
    """Serialize a CorpusBuildResult to a JSON-safe dict (for MCP/CLI output)."""
    return _json_safe(asdict(result))


def _json_safe(obj: Any) -> Any:
    """Convert pandas/numpy scalars to native Python for JSON serialization."""
    import math

    if obj is None:
        return None
    try:
        import numpy as np
        import pandas as pd
    except ImportError:
        return obj

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
