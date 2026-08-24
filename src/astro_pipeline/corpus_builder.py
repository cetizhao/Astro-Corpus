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

import hashlib
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from astro_pipeline.config import PipelineConfig
from astro_pipeline.gaia_releases import (
    get_gaia_release_profile,
    normalize_gaia_release,
)
from astro_pipeline.intake import resolve_members
from astro_pipeline.io.writer import save_catalog_parquet
from astro_pipeline.manifest import CorpusManifest, validate_label
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
    gaia_release: str = "dr3"
    members_csv: str = ""
    members_parquet: str = ""
    paper_edges_parquet: str = ""
    simbad_refs_parquet: str = ""
    viz_html: str = ""  # file:// URL or ""
    manifest_json: str = ""
    xp_products: dict[str, Any] = field(default_factory=dict)
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
    xp_sampled: bool = False,
    xp_continuous: bool = False,
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
        then a Gaia field-source cone search. This does not select physical
        members; use :mod:`astro_pipeline.membership` for that.
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
    label = validate_label(label)
    gaia_release = normalize_gaia_release(PipelineConfig.load().gaia.release)
    release_profile = get_gaia_release_profile(gaia_release)
    if release_profile.status != "stable":
        raise NotImplementedError(
            f"ordinary corpus resolution for {release_profile.display_name} is "
            "disabled until its TAP schema and canonical column mapping are confirmed; "
            "use Gaia Archive discovery and generic DataLink tools"
        )

    t0 = time.time()
    out_dir = Path(out_dir)

    input_summary = _describe_input(
        cone=cone,
        name=name,
        region=region,
        catalog=catalog,
        limit=limit,
    )

    logger.info("Building corpus %r: %s", label, input_summary)

    manifest = CorpusManifest.open_or_create(
        out_dir / "manifest.json",
        label=label,
        input_mode=input_mode,
        input_summary=input_summary,
        requested_products=_requested_products(
            skip_simbad=skip_simbad,
            xp_sampled=xp_sampled,
            xp_continuous=xp_continuous,
        ),
        gaia_release=gaia_release,
    )
    manifest.update_stage("resolve_members", status="running", completed=0)

    # --- 1. Resolve members ---
    try:
        members = resolve_members(
            cone=cone, name=name, region=region, catalog=catalog, limit=limit,
        )
    except Exception as e:
        manifest.update_stage("resolve_members", status="failed", error=str(e))
        manifest.add_error(f"member resolution failed: {e}")
        manifest.finish("failed")
        return CorpusBuildResult(
            label=label, input_mode=input_mode, input_summary=input_summary,
            n_members=0, n_paper_edges=0, n_unique_papers=0,
            out_dir=str(out_dir), skipped_simbad=skip_simbad,
            elapsed_seconds=time.time() - t0, error=str(e),
            manifest_json=str(manifest.path.resolve()),
            gaia_release=gaia_release,
        )

    logger.info("Resolved %d members", len(members))
    manifest.update_stage(
        "resolve_members",
        status="completed",
        total=len(members),
        completed=len(members),
    )

    return build_corpus_from_members(
        label=label,
        out_dir=out_dir,
        members=members,
        input_mode=input_mode,
        input_summary=input_summary,
        skip_simbad=skip_simbad,
        sleep_seconds=sleep_seconds,
        enrich=enrich,
        xp_sampled=xp_sampled,
        xp_continuous=xp_continuous,
        started_at=t0,
        manifest=manifest,
        gaia_release=gaia_release,
    )


def build_corpus_from_members(
    *,
    label: str,
    out_dir: Path | str,
    members: pd.DataFrame,
    input_mode: str,
    input_summary: str,
    skip_simbad: bool = False,
    sleep_seconds: float = 0.3,
    enrich: bool = True,
    xp_sampled: bool = False,
    xp_continuous: bool = False,
    started_at: Optional[float] = None,
    manifest: Optional[CorpusManifest] = None,
    gaia_release: str = "dr3",
) -> CorpusBuildResult:
    """Persist an already-resolved Gaia member table as a corpus snapshot.

    This shared second half keeps user-uploaded catalogues and literature
    membership catalogues on the same output contract as cone/name builds.
    """
    label = validate_label(label)
    gaia_release = normalize_gaia_release(gaia_release)
    t0 = started_at if started_at is not None else time.time()
    out_dir = Path(out_dir)

    if manifest is None:
        manifest = CorpusManifest.open_or_create(
            out_dir / "manifest.json",
            label=label,
            input_mode=input_mode,
            input_summary=input_summary,
            requested_products=_requested_products(
                skip_simbad=skip_simbad,
                xp_sampled=xp_sampled,
                xp_continuous=xp_continuous,
            ),
            gaia_release=gaia_release,
        )
        manifest.update_stage(
            "resolve_members",
            status="completed",
            total=len(members),
            completed=len(members),
            note="members DataFrame supplied directly",
        )
    manifest_path = str(manifest.path.resolve())
    manifest_release = normalize_gaia_release(
        manifest.data.get("corpus", {}).get("gaia_release", "dr3")
    )
    if manifest_release != gaia_release:
        raise ValueError(
            f"members release {gaia_release!r} does not match manifest release "
            f"{manifest_release!r}"
        )

    if members.empty:
        manifest.update_stage(
            "save_members", status="failed", total=0, completed=0,
            error="no members resolved for the given input",
        )
        manifest.add_error("no members resolved for the given input")
        manifest.finish("failed")
        return CorpusBuildResult(
            label=label, input_mode=input_mode, input_summary=input_summary,
            n_members=0, n_paper_edges=0, n_unique_papers=0,
            out_dir=str(out_dir), skipped_simbad=skip_simbad,
            elapsed_seconds=time.time() - t0,
            error="no members resolved for the given input",
            manifest_json=manifest_path,
            gaia_release=gaia_release,
        )

    # --- Save members ---
    out_dir.mkdir(parents=True, exist_ok=True)
    members_csv = out_dir / "members.csv"
    members_parquet = out_dir / "members.parquet"
    members = members.copy()
    members["gaia_release"] = gaia_release
    members.to_csv(members_csv, index=False)
    save_catalog_parquet(members, members_parquet)
    manifest.update_stage(
        "save_members", status="completed", total=len(members), completed=len(members)
    )
    manifest.add_artifact("members_csv", members_csv, rows=len(members))
    manifest.add_artifact("members_parquet", members_parquet, rows=len(members))

    # Optional Gaia XP products are independent of SIMBAD and therefore also
    # work for quick ``skip_simbad`` corpus builds.
    xp_products: dict[str, Any] = {}
    if xp_sampled or xp_continuous:
        from astro_pipeline.corpus_products import download_gaia_xp_products

        def update_product_stage(stage: str, update: dict[str, Any]) -> None:
            manifest.update_stage(stage, **update)

        xp_products = download_gaia_xp_products(
            members,
            out_dir,
            sampled=xp_sampled,
            continuous=xp_continuous,
            progress_callback=update_product_stage,
        )
        if xp_products.get("availability_path"):
            manifest.add_artifact(
                "gaia_xp_availability", xp_products["availability_path"]
            )
        for product in ("sampled", "continuous"):
            info = xp_products.get(product, {})
            if info.get("requested") and info.get("output_dir"):
                manifest.add_artifact(
                    f"gaia_xp_{product}",
                    info["output_dir"],
                    cached=info.get("cached", 0),
                    available=info.get("available", 0),
                )
        for error in xp_products.get("errors", []):
            manifest.add_warning(error)
    else:
        _set_stage_if_absent(manifest, "gaia_xp_sampled", status="not_requested")
        _set_stage_if_absent(manifest, "gaia_xp_continuous", status="not_requested")

    if skip_simbad:
        logger.info("--skip-simbad; stopping after members.*")
        _set_stage_if_absent(manifest, "simbad_papers", status="skipped")
        _set_stage_if_absent(manifest, "visualization", status="skipped")
        terminal = (
            "partial"
            if xp_products.get("errors") or _has_incomplete_requested_stage(manifest)
            else "completed"
        )
        manifest.finish(terminal)
        return CorpusBuildResult(
            label=label, input_mode=input_mode, input_summary=input_summary,
            n_members=len(members), n_paper_edges=0, n_unique_papers=0,
            out_dir=str(out_dir.resolve()),
            members_csv=str(members_csv.resolve()),
            members_parquet=str(members_parquet.resolve()),
            skipped_simbad=True,
            elapsed_seconds=time.time() - t0,
            manifest_json=manifest_path,
            xp_products=xp_products,
            gaia_release=gaia_release,
        )

    # --- SIMBAD paper edges (inherits resumable cache via cache_path) ---
    logger.info(
        "Querying SIMBAD for paper bibliography per source "
        "(sleep=%.2fs, enrich=%s; interruptible, resumable)...",
        sleep_seconds, enrich,
    )
    manifest.update_stage(
        "simbad_papers", status="running", total=len(members), completed=0
    )
    simbad_progress: dict[str, Any] = {}

    def update_simbad_progress(update: dict[str, Any]) -> None:
        simbad_progress.update(update)
        # Source lookup can finish before metadata enrichment, so keep the
        # overall stage running until build_paper_edges returns.
        manifest.update_stage(
            "simbad_papers",
            status="running",
            total=int(update.get("total", len(members))),
            completed=int(update.get("completed", 0)),
            failed=int(update.get("failed", 0)),
            remaining=int(update.get("remaining", len(members))),
        )

    try:
        edges, refs = build_paper_edges(
            members,
            id_col="Gaia",
            cache_path=out_dir / "simbad_refs.parquet",
            sleep_seconds=sleep_seconds,
            enrich=enrich,
            progress_callback=update_simbad_progress,
        )
    except Exception as exc:
        message = f"SIMBAD paper stage failed: {exc}"
        manifest.update_stage("simbad_papers", status="failed", error=str(exc))
        manifest.add_error(message)
        manifest.finish("failed")
        return CorpusBuildResult(
            label=label,
            input_mode=input_mode,
            input_summary=input_summary,
            n_members=len(members),
            n_paper_edges=0,
            n_unique_papers=0,
            out_dir=str(out_dir.resolve()),
            members_csv=str(members_csv.resolve()),
            members_parquet=str(members_parquet.resolve()),
            manifest_json=manifest_path,
            xp_products=xp_products,
            skipped_simbad=False,
            elapsed_seconds=time.time() - t0,
            error=message,
            gaia_release=gaia_release,
        )
    edges_path = out_dir / "paper_edges.parquet"
    edges.save(edges_path)
    source_progress = refs.attrs.get("source_progress", simbad_progress)
    simbad_failed = int(source_progress.get("failed", 0))
    simbad_stage_status = "partial" if simbad_failed else "completed"
    manifest.update_stage(
        "simbad_papers",
        status=simbad_stage_status,
        total=len(members),
        completed=int(source_progress.get("completed", len(members))),
        failed=simbad_failed,
        remaining=int(source_progress.get("remaining", 0)),
        n_edges=len(edges),
        n_unique_papers=int(refs["bibcode"].nunique()) if not refs.empty else 0,
    )
    if simbad_failed:
        manifest.add_warning(
            f"SIMBAD bibliography lookup failed for {simbad_failed} source(s); "
            "rerun the same label to retry them from the source checkpoint"
        )
    manifest.add_artifact("paper_edges_parquet", edges_path, rows=len(edges))
    manifest.add_artifact("simbad_refs_parquet", out_dir / "simbad_refs.parquet")
    checkpoint_path = out_dir / "simbad_refs.checkpoint.json"
    if checkpoint_path.exists():
        manifest.add_artifact("simbad_refs_checkpoint", checkpoint_path)

    # --- Viz HTML ---
    # inline_echarts=True so the HTML is self-contained (no sibling file dep).
    # The ~1 MB cost is acceptable and avoids the "blank page" failure mode
    # when the corpus dir is moved/shared without echarts.min.js.
    from astro_pipeline.viz import build_overview_html

    viz_path = out_dir / "source_paper_viz.html"
    vendored_echarts = Path(__file__).parent / "viz" / "vendor" / "echarts.min.js"
    legacy_echarts = out_dir.parent / "twa" / "echarts.min.js"
    echarts_src = vendored_echarts if vendored_echarts.exists() else legacy_echarts
    inline_echarts = echarts_src.exists()
    if not inline_echarts:
        manifest.add_warning(
            "vendored echarts.min.js was not found; HTML was generated with "
            "a sibling script reference and is not fully self-contained"
        )
    manifest.update_stage("visualization", status="running", total=1, completed=0)
    try:
        build_overview_html(
            output_path=viz_path,
            catalog_path=members_csv,
            edges_path=edges_path,
            corpus_name=label,
            inline_echarts=inline_echarts,
            echarts_path=echarts_src if inline_echarts else None,
            spectra_dir=out_dir,
        )
    except Exception as exc:
        message = f"visualization stage failed: {exc}"
        manifest.update_stage("visualization", status="failed", error=str(exc))
        manifest.add_error(message)
        manifest.finish("partial")
        return CorpusBuildResult(
            label=label,
            input_mode=input_mode,
            input_summary=input_summary,
            n_members=len(members),
            n_paper_edges=len(edges),
            n_unique_papers=int(refs["bibcode"].nunique()) if not refs.empty else 0,
            out_dir=str(out_dir.resolve()),
            members_csv=str(members_csv.resolve()),
            members_parquet=str(members_parquet.resolve()),
            paper_edges_parquet=str(edges_path.resolve()),
            simbad_refs_parquet=str((out_dir / "simbad_refs.parquet").resolve()),
            manifest_json=manifest_path,
            xp_products=xp_products,
            skipped_simbad=False,
            elapsed_seconds=time.time() - t0,
            error=message,
            gaia_release=gaia_release,
        )
    manifest.update_stage("visualization", status="completed", total=1, completed=1)
    manifest.add_artifact("visualization_html", viz_path)

    n_papers = int(refs["bibcode"].nunique()) if not refs.empty else 0
    elapsed = time.time() - t0
    logger.info(
        "Corpus %r built: %d members, %d edges, %d papers (%.1fs)",
        label, len(members), len(edges), n_papers, elapsed,
    )
    terminal = (
        "partial"
        if (
            xp_products.get("errors")
            or simbad_failed
            or _has_incomplete_requested_stage(manifest)
        )
        else "completed"
    )
    manifest.finish(terminal)

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
        manifest_json=manifest_path,
        xp_products=xp_products,
        skipped_simbad=False,
        elapsed_seconds=elapsed,
        gaia_release=gaia_release,
    )


def result_to_dict(result: CorpusBuildResult) -> dict[str, Any]:
    """Serialize a CorpusBuildResult to a JSON-safe dict (for MCP/CLI output)."""
    return _json_safe(asdict(result))


def _requested_products(
    *, skip_simbad: bool, xp_sampled: bool, xp_continuous: bool
) -> dict[str, bool]:
    return {
        "members": True,
        "simbad_papers": not skip_simbad,
        "visualization": not skip_simbad,
        "gaia_xp_sampled": xp_sampled,
        "gaia_xp_continuous": xp_continuous,
    }


def _set_stage_if_absent(
    manifest: CorpusManifest, stage: str, **updates: Any
) -> None:
    if stage not in manifest.data.get("stages", {}):
        manifest.update_stage(stage, **updates)


def _has_incomplete_requested_stage(manifest: CorpusManifest) -> bool:
    products = manifest.data.get("requested_products", {})
    stages = manifest.data.get("stages", {})
    product_stages = {
        "simbad_papers": "simbad_papers",
        "visualization": "visualization",
        "gaia_xp_sampled": "gaia_xp_sampled",
        "gaia_xp_continuous": "gaia_xp_continuous",
    }
    return any(
        products.get(product)
        and stages.get(stage, {}).get("status") != "completed"
        for product, stage in product_stages.items()
    )


def _describe_input(
    *,
    cone: Optional[tuple[float, float, float]],
    name: Optional[str],
    region: Optional[tuple[str, float]],
    catalog: Optional[str | Path],
    limit: Optional[int],
) -> str:
    """Build the stable source-selection identity stored in a manifest."""
    limit_text = "all" if limit is None else str(limit)
    if cone:
        ra, dec, radius = cone
        return f"cone(ra={ra}, dec={dec}, radius={radius}deg); limit={limit_text}"
    if name:
        return f"name={name!r}; limit={limit_text}"
    if region:
        center, radius = region
        return (
            f"region(center={center!r}, radius={radius}deg); limit={limit_text}"
        )

    catalog_path = Path(catalog)  # type: ignore[arg-type]
    if not catalog_path.is_file():
        raise FileNotFoundError(f"Member catalog not found: {catalog_path}")
    digest = hashlib.sha256()
    with catalog_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return (
        f"catalog={catalog_path.name}; sha256={digest.hexdigest()}; "
        f"limit={limit_text}"
    )


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
