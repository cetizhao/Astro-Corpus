"""Discover published membership catalogues and turn one into a Gaia corpus.

The existing ``region`` intake mode answers a geometric question: *which Gaia
sources lie inside this cone?*  It cannot establish physical membership in a
cluster or association.  This module implements the separate, literature-led
workflow needed for that question:

1. search VizieR for published membership catalogues;
2. preview the candidate tables and their scientific columns;
3. fetch the explicitly selected table without applying hidden quality cuts;
4. cross-match its rows to Gaia DR3 while retaining catalogue provenance.

VizieR is used as the catalogue registry and data service.  The original table
is always saved alongside the Gaia-normalised ``members`` table so downstream
users can audit membership probabilities, flags, populations and citations.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import quote

import pandas as pd
from astroquery.vizier import Vizier

from astro_pipeline.config import PipelineConfig
from astro_pipeline.intake import QC_EXTRA_COLUMNS, _canonicalize
from astro_pipeline.models.query import SourceIdQuery
from astro_pipeline.manifest import CorpusManifest, validate_label
from astro_pipeline.sources.gaia import GaiaSource

logger = logging.getLogger(__name__)

VIZIER_SOURCE_URL = "https://vizier.cds.unistra.fr/viz-bin/VizieR?-source={}"


@dataclass
class MembershipResolution:
    """A selected literature table and its Gaia DR3 cross-match."""

    catalog_id: str
    table_id: str
    table_description: str
    catalog_title: str
    original: pd.DataFrame
    members: pd.DataFrame
    unmatched: pd.DataFrame
    match_method: str
    match_radius_arcsec: Optional[float]
    all_rows: Optional[pd.DataFrame] = None
    table_schema: list[dict[str, Any]] = field(default_factory=list)
    coordinate_descriptor: Optional[dict[str, Any]] = None


def search_membership_catalogs(target: str, max_results: int = 10) -> dict[str, Any]:
    """Search VizieR for published member lists for ``target``.

    The call intentionally returns catalogue-level candidates only.  It is a
    quick discovery pass; callers should use :func:`preview_membership_catalog`
    before selecting a table for download.
    """
    target = target.strip()
    if not target:
        raise ValueError("target must not be empty")
    if max_results < 1 or max_results > 50:
        raise ValueError("max_results must be between 1 and 50")

    queries = list(
        dict.fromkeys(
            [
                f"{target} membership",
                f"{target} members",
                f"{target} census",
                f"{target} candidate members",
                target,
            ]
        )
    )
    merged: dict[str, dict[str, Any]] = {}
    scan_limit = min(100, max(20, max_results * 4))

    for query_index, query in enumerate(queries):
        try:
            found = Vizier.find_catalogs(query, max_catalogs=scan_limit)
        except Exception as exc:
            logger.warning("VizieR catalogue search failed for %r: %s", query, exc)
            continue
        if not isinstance(found, Mapping):
            logger.warning(
                "VizieR catalogue search returned an unsupported result for %r",
                query,
            )
            continue
        for rank, (raw_catalog_id, resource) in enumerate(found.items()):
            catalog_id = _catalog_id_from_search_resource(raw_catalog_id, resource)
            if catalog_id is None:
                logger.warning(
                    "Skipping VizieR search resource with no valid catalogue ID "
                    "for query %r",
                    query,
                )
                continue
            description = getattr(resource, "description", "")
            title = (
                description.strip()
                if isinstance(description, str) and description.strip()
                else catalog_id
            )
            score, signals = _membership_score(
                target=target,
                catalog_id=catalog_id,
                title=title,
                query_index=query_index,
                rank=rank,
            )
            row = merged.setdefault(
                catalog_id,
                {
                    "catalog_id": catalog_id,
                    "title": title,
                    "provider": "CDS VizieR",
                    "url": VIZIER_SOURCE_URL.format(quote(catalog_id, safe="/")),
                    "score": score,
                    "relevance_signals": signals,
                    "matched_queries": [],
                },
            )
            row["score"] = max(int(row["score"]), score)
            row["relevance_signals"] = sorted(
                set(row["relevance_signals"]) | set(signals)
            )
            row["matched_queries"].append(query)

    candidates = sorted(
        merged.values(),
        key=lambda item: (-int(item["score"]), str(item["catalog_id"])),
    )[:max_results]
    return {
        "target": target,
        "query_kind": "published_membership_catalogues",
        "provider": "CDS VizieR",
        "n_candidates": len(candidates),
        "candidates": candidates,
        "next_step": (
            "Preview one or more catalog_id values, inspect table descriptions "
            "and membership columns, then explicitly choose a table_id."
        ),
        "warning": (
            "A catalogue candidate is not automatically authoritative for every "
            "science case. The pipeline does not silently filter membership "
            "probabilities or candidate flags."
        ),
    }


def preview_membership_catalog(
    catalog_id: str,
    *,
    table_id: Optional[str] = None,
    sample_size: int = 8,
) -> dict[str, Any]:
    """Return table structure and sample rows for a VizieR catalogue."""
    catalog_id = _validate_vizier_id(catalog_id, "catalog_id")
    if table_id is not None:
        table_id = _validate_vizier_id(table_id, "table_id")
    if sample_size < 1 or sample_size > 50:
        raise ValueError("sample_size must be between 1 and 50")

    vizier = Vizier(columns=["**"], row_limit=sample_size)
    tables = vizier.get_catalogs(catalog_id)
    if not tables:
        raise LookupError(f"VizieR returned no tables for {catalog_id!r}")

    metadata = _catalog_metadata(vizier, catalog_id)
    selected = _select_tables(tables, table_id=table_id, require_one=False)
    previews: list[dict[str, Any]] = []
    for current_id, table in selected:
        frame = _table_to_dataframe(table)
        detection = _detect_columns(table)
        role = _table_role(str(table.meta.get("description", "")))
        previews.append(
            {
                "table_id": current_id,
                "description": str(table.meta.get("description", "") or ""),
                "role": role,
                "recommended_member_table": role == "members",
                "sample_row_count": len(frame),
                "row_count": None,
                "row_count_note": (
                    "Preview is deliberately row-limited; exact count is recorded "
                    "when the selected table is downloaded."
                ),
                "columns": _column_metadata(table),
                **detection,
                "sample_rows": _json_records(frame),
            }
        )

    return {
        "catalog_id": catalog_id,
        "provider": "CDS VizieR",
        "url": VIZIER_SOURCE_URL.format(quote(catalog_id, safe="/")),
        **metadata,
        "n_tables": len(previews),
        "tables": previews,
        "selection_note": (
            "Prefer a table whose role is 'members' when the request is for "
            "established members. A 'candidates' table is a different sample."
        ),
    }


def resolve_membership_catalog(
    catalog_id: str,
    *,
    table_id: Optional[str] = None,
    limit: Optional[int] = None,
    match_radius_arcsec: float = 3.0,
    match_batch_size: int = 25,
    id_fetcher=None,
) -> MembershipResolution:
    """Fetch one selected membership table and cross-match it to Gaia DR3.

    ``limit`` is applied to catalogue rows *before* Gaia matching and is mainly
    intended for small tests.  No probability, quality, population or
    candidate/member filters are applied.
    """
    catalog_id = _validate_vizier_id(catalog_id, "catalog_id")
    if table_id is not None:
        table_id = _validate_vizier_id(table_id, "table_id")
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive or None")

    # Fetch the full selected VizieR table.  row_limit=-1 is important:
    # astroquery otherwise defaults to a small preview-sized result.
    vizier = Vizier(columns=["**"], row_limit=-1)
    tables = vizier.get_catalogs(catalog_id)
    if not tables:
        raise LookupError(f"VizieR returned no tables for {catalog_id!r}")
    selected_id, table = _select_tables(
        tables, table_id=table_id, require_one=True
    )[0]
    metadata = _catalog_metadata(vizier, catalog_id)
    description = str(table.meta.get("description", "") or "")
    all_rows = _table_to_dataframe(table)
    all_rows.insert(0, "membership_catalog_row", range(len(all_rows)))
    original = all_rows.head(limit).copy() if limit is not None else all_rows.copy()
    if original.empty:
        raise LookupError(f"Selected VizieR table {selected_id!r} is empty")

    detection = _detect_columns(table)
    gaia_dr3_columns = detection["gaia_dr3_id_columns"]
    coordinate_descriptor = detection["coordinate_descriptor"]
    coordinate_pair = detection["coordinate_columns"]
    config = PipelineConfig.load()
    # Published Gaia-ID columns detected by this workflow are explicitly DR3.
    # A user's provisional DR4 config must not reinterpret them silently.
    gaia_config = config.gaia.model_copy(
        update={"release": "dr3", "db_schema": "gaiadr3"}
    )
    gaia = GaiaSource(gaia_config, config.network)

    if gaia_dr3_columns:
        source_col = gaia_dr3_columns[0]
        members, unmatched = _match_by_gaia_id(
            original,
            gaia=gaia,
            source_col=source_col,
            coordinate_pair=coordinate_pair,
            coordinate_descriptor=coordinate_descriptor,
            id_fetcher=id_fetcher,
        )
        match_method = f"gaia_dr3_source_id:{source_col}"
        radius: Optional[float] = None
    elif coordinate_pair:
        members, unmatched = _match_by_position(
            original,
            gaia=gaia,
            ra_col=coordinate_pair[0],
            dec_col=coordinate_pair[1],
            ra_unit=coordinate_descriptor["ra_unit"],
            dec_unit=coordinate_descriptor["dec_unit"],
            radius_arcsec=match_radius_arcsec,
            batch_size=match_batch_size,
        )
        match_method = "gaia_dr3_position"
        radius = match_radius_arcsec
    else:
        raise ValueError(
            f"Table {selected_id!r} has neither a Gaia DR3 source-id column "
            "nor a recognized RA/Dec pair; inspect preview columns and provide "
            "a different member table."
        )

    provenance = {
        "membership_catalog_id": catalog_id,
        "membership_table_id": selected_id,
        "membership_table_description": description,
        "membership_catalog_title": str(metadata.get("title", "")),
        "membership_match_method": match_method,
        "membership_match_radius_arcsec": radius,
        "membership_match_ra_column": (
            coordinate_descriptor.get("ra_column")
            if coordinate_descriptor is not None
            else None
        ),
        "membership_match_dec_column": (
            coordinate_descriptor.get("dec_column")
            if coordinate_descriptor is not None
            else None
        ),
        "membership_match_ra_unit": (
            coordinate_descriptor.get("ra_unit")
            if coordinate_descriptor is not None
            else None
        ),
        "membership_match_dec_unit": (
            coordinate_descriptor.get("dec_unit")
            if coordinate_descriptor is not None
            else None
        ),
        "membership_coordinate_frame": (
            coordinate_descriptor.get("frame")
            if coordinate_descriptor is not None
            else None
        ),
        "membership_coordinate_epoch": (
            coordinate_descriptor.get("reference_epoch")
            if coordinate_descriptor is not None
            else None
        ),
        "membership_coordinate_epoch_source": (
            coordinate_descriptor.get("epoch_source")
            if coordinate_descriptor is not None
            else None
        ),
    }
    for key, value in provenance.items():
        members[key] = value
        unmatched[key] = value

    return MembershipResolution(
        catalog_id=catalog_id,
        table_id=selected_id,
        table_description=description,
        catalog_title=str(metadata.get("title", "")),
        original=original,
        members=members,
        unmatched=unmatched,
        match_method=match_method,
        match_radius_arcsec=radius,
        all_rows=all_rows,
        table_schema=_column_metadata(table),
        coordinate_descriptor=coordinate_descriptor,
    )


def build_membership_corpus(
    *,
    label: str,
    out_dir: Path | str,
    catalog_id: str,
    table_id: Optional[str] = None,
    limit: Optional[int] = None,
    match_radius_arcsec: float = 3.0,
    match_batch_size: int = 25,
    skip_simbad: bool = False,
    sleep_seconds: float = 0.3,
    enrich: bool = True,
    xp_sampled: bool = False,
    xp_continuous: bool = False,
    refresh: bool = False,
    static_viz_mode: str = "embedded",
    resume_selection_run_id: Optional[str] = None,
) -> dict[str, Any]:
    """Build a corpus; opt in to a validated, published frozen selection by run ID."""
    if resume_selection_run_id is not None and refresh:
        raise ValueError("refresh and resume_selection_run_id cannot be combined")
    label = validate_label(label)
    static_viz_mode = _validate_static_viz_mode(static_viz_mode)
    out_dir = Path(out_dir)
    from astro_pipeline.library import get_library
    library = get_library()
    if library is not None:
        return _build_indexed_membership(
            library=library, label=label, out_dir=out_dir, catalog_id=catalog_id,
            table_id=table_id, limit=limit, match_radius_arcsec=match_radius_arcsec,
            match_batch_size=match_batch_size, skip_simbad=skip_simbad,
            sleep_seconds=sleep_seconds, enrich=enrich, xp_sampled=xp_sampled,
            xp_continuous=xp_continuous, refresh=refresh,
            static_viz_mode=static_viz_mode,
            resume_selection_run_id=resume_selection_run_id,
        )
    if resume_selection_run_id is not None:
        raise ValueError("resume_selection_run_id requires the shared library")
    out_dir.mkdir(parents=True, exist_ok=True)
    limit_text = "all" if limit is None else str(limit)
    input_summary = (
        f"VizieR catalog={catalog_id}, table={table_id or 'auto'}, "
        f"limit={limit_text}, match_radius={match_radius_arcsec}arcsec"
    )
    manifest = CorpusManifest.open_or_create(
        out_dir / "manifest.json",
        label=label,
        input_mode="membership_catalog",
        input_summary=input_summary,
        requested_products={
            "members": True,
            "membership_catalog": True,
            "simbad_papers": not skip_simbad,
            "visualization": not skip_simbad and static_viz_mode != "off",
            "gaia_xp_sampled": xp_sampled,
            "gaia_xp_continuous": xp_continuous,
        },
        gaia_release="dr3",
    )
    manifest.update_stage("resolve_members", status="running", completed=0)
    try:
        resolution = resolve_membership_catalog(
            catalog_id,
            table_id=table_id,
            limit=limit,
            match_radius_arcsec=match_radius_arcsec,
            match_batch_size=match_batch_size,
        )
    except Exception as exc:
        manifest.update_stage("resolve_members", status="failed", error=str(exc))
        manifest.add_error(f"membership catalogue resolution failed: {exc}")
        manifest.finish("failed")
        raise
    manifest.update_stage(
        "resolve_members",
        status="completed",
        total=len(resolution.original),
        completed=len(resolution.members),
        unmatched=len(resolution.unmatched),
        catalog_id=resolution.catalog_id,
        table_id=resolution.table_id,
        match_method=resolution.match_method,
    )

    original_csv = out_dir / "membership_catalog.csv"
    original_parquet = out_dir / "membership_catalog.parquet"
    unmatched_csv = out_dir / "unmatched_members.csv"
    resolution.original.to_csv(original_csv, index=False)
    resolution.original.to_parquet(original_parquet, index=False)
    resolution.unmatched.to_csv(unmatched_csv, index=False)
    manifest.add_artifact(
        "membership_catalog_csv", original_csv, rows=len(resolution.original)
    )
    manifest.add_artifact(
        "membership_catalog_parquet", original_parquet, rows=len(resolution.original)
    )
    manifest.add_artifact(
        "unmatched_members_csv", unmatched_csv, rows=len(resolution.unmatched)
    )

    from astro_pipeline.corpus_builder import build_corpus_from_members, result_to_dict

    result = build_corpus_from_members(
        label=label,
        out_dir=out_dir,
        members=resolution.members,
        input_mode="membership_catalog",
        input_summary=input_summary,
        skip_simbad=skip_simbad,
        sleep_seconds=sleep_seconds,
        enrich=enrich,
        xp_sampled=xp_sampled,
        xp_continuous=xp_continuous,
        manifest=manifest,
        gaia_release="dr3",
        static_viz_mode=static_viz_mode,
    )
    output = result_to_dict(result)
    output.update(
        {
            "membership_catalog_id": resolution.catalog_id,
            "membership_table_id": resolution.table_id,
            "membership_table_description": resolution.table_description,
            "membership_catalog_title": resolution.catalog_title,
            "membership_match_method": resolution.match_method,
            "membership_match_radius_arcsec": resolution.match_radius_arcsec,
            "membership_coordinate_descriptor": resolution.coordinate_descriptor,
            "static_viz_mode": static_viz_mode,
            "n_catalog_rows": len(resolution.original),
            "n_gaia_matched": len(resolution.members),
            "n_unmatched": len(resolution.unmatched),
            "membership_catalog_csv": str(original_csv.resolve()),
            "membership_catalog_parquet": str(original_parquet.resolve()),
            "unmatched_members_csv": str(unmatched_csv.resolve()),
            "filtering_applied": False,
        }
    )
    return output


def _build_indexed_membership(
    *, library, label, out_dir, catalog_id, table_id, limit, match_radius_arcsec,
    match_batch_size, skip_simbad, sleep_seconds, enrich, xp_sampled, xp_continuous,
    refresh, static_viz_mode, resume_selection_run_id,
):
    from astro_pipeline.corpus_builder import build_corpus_from_members, result_to_dict
    from astro_pipeline.intake import InputResolution
    from astro_pipeline.library.build import LibraryRun, id_cache_fetcher
    from astro_pipeline.library.files import atomic_copy, atomic_file, sha256_file
    from astro_pipeline.library.identity import canonical_json, identity_hash, json_value
    from astro_pipeline.manifest import read_manifest
    from astro_pipeline.membership_resume import load_published_membership_selection

    catalog_id = _validate_vizier_id(catalog_id, "catalog_id")
    if table_id is not None:
        table_id = _validate_vizier_id(table_id, "table_id")
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive or None")
    previous = read_manifest(out_dir / "manifest.json") or {}
    previous_selector = previous.get("corpus", {}).get("input_contract", {}).get("selector", {})
    effective_table = table_id
    if effective_table is not None and "/" not in effective_table:
        effective_table = f"{catalog_id}/{effective_table}"
    if effective_table is None and previous_selector.get("catalog_id") == catalog_id:
        effective_table = previous_selector.get("table_id")
    frozen = None
    if resume_selection_run_id is not None:
        frozen = load_published_membership_selection(
            library, out_dir=out_dir, label=label, run_id=resume_selection_run_id,
            catalog_id=catalog_id, table_id=table_id, limit=limit,
            match_radius_arcsec=match_radius_arcsec,
        )
        effective_table = frozen.resolution.table_id
    selector = {
        "catalog_id": catalog_id, "table_id": effective_table,
        "match_radius_arcsec": float(match_radius_arcsec),
    }
    contract = {"release": "dr3", "input_mode": "membership_catalog", "limit": limit, "selector": selector}
    limit_text = "all" if limit is None else str(limit)
    summary = f"VizieR catalog={catalog_id}, table={table_id or 'auto'}, limit={limit_text}, match_radius={match_radius_arcsec}arcsec"
    products = {
        "members": True, "membership_catalog": True, "simbad_papers": not skip_simbad,
        "visualization": not skip_simbad and static_viz_mode != "off",
        "gaia_xp_sampled": xp_sampled,
        "gaia_xp_continuous": xp_continuous,
    }
    with LibraryRun(
        store=library, out_dir=out_dir, label=label, input_mode="membership_catalog",
        input_summary=summary, contract=contract, release="dr3", requested_products=products,
        options={
            "refresh": refresh,
            "requested_table_id": table_id,
            "skip_simbad": skip_simbad,
            "static_viz_mode": static_viz_mode,
            "resume_selection_run_id": resume_selection_run_id,
        },
    ) as run:
        run.manifest.update_stage("resolve_members", status="running", completed=0)
        if frozen is None:
            resolution = resolve_membership_catalog(
                catalog_id, table_id=effective_table, limit=limit,
                match_radius_arcsec=match_radius_arcsec, match_batch_size=match_batch_size,
                id_fetcher=id_cache_fetcher(library, "dr3", refresh=refresh),
            )
        else:
            resolution = frozen.resolution
        if effective_table is not None and resolution.table_id != effective_table:
            raise ValueError("Resolved membership table changed; choose a new label")
        contract = {**contract, "selector": {**selector, "table_id": resolution.table_id}}
        all_rows = resolution.all_rows if resolution.all_rows is not None else resolution.original
        metadata = frozen.resolved_metadata.copy() if frozen is not None else resolution_metadata(resolution)
        if frozen is None:
            metadata["table_content_hash"] = identity_hash(
                {"rows": _json_records(all_rows), "schema": resolution.table_schema},
                namespace="vizier-table",
            )
        run.set_resolved_contract(contract, metadata)
        if frozen is not None:
            input_rows, matches = frozen.input_rows, frozen.matches
        else:
            selected = set(resolution.original["membership_catalog_row"].astype(int))
            matched = {int(row["membership_catalog_row"]): row for row in _json_records(resolution.members)}
            unmatched_rows = {
                int(row["membership_catalog_row"]): row
                for row in _json_records(resolution.unmatched)
            }
            input_rows = []
            matches = []
            for ordinal, row in enumerate(_json_records(all_rows)):
                row_id = int(row["membership_catalog_row"])
                member = matched.get(row_id)
                unmatched_row = unmatched_rows.get(row_id, {})
                status = "not_selected_by_limit" if row_id not in selected else "matched" if member else "unmatched"
                if status == "unmatched" and unmatched_row.get("membership_match_status") == "invalid_coordinates":
                    status = "invalid"
                if status == "unmatched" and resolution.match_method.startswith("gaia_dr3_source_id:"):
                    id_column = resolution.match_method.split(":", 1)[1]
                    status = "id_not_found" if _normalise_gaia_id(row.get(id_column)) else "invalid"
                input_rows.append({"input_row_id": row_id, "ordinal": ordinal, "status": status, "raw_json": canonical_json(row)})
                matches.append({
                    "input_row_id": row_id, "source_id": member.get("Gaia") if member else None,
                    "status": status, "match_method": resolution.match_method,
                    "match_sep_arcsec": member.get("membership_match_sep_arcsec") if member else None,
                    "match_candidate_count": member.get("membership_match_candidate_count", 1) if member else 0,
                })
            input_rows = pd.DataFrame(input_rows)
            matches = pd.DataFrame(json_value(matches))
        members = resolution.members.copy()
        if "membership_catalog_row" in members:
            members["input_row_id"] = members["membership_catalog_row"]
        rich = InputResolution(
            members=members, input_rows=input_rows,
            matches=matches, contract=contract,
            resolved_input=metadata,
        )
        run.set_resolution(rich)
        run.manifest.update_stage(
            "resolve_members", status="completed", total=len(resolution.original),
            completed=len(members), unmatched=len(resolution.unmatched),
            catalog_id=resolution.catalog_id, table_id=resolution.table_id,
            match_method=resolution.match_method,
            **({"resume_selection_run_id": frozen.source_run_id} if frozen is not None else {}),
        )
        frames = {
            "membership_catalog.csv": resolution.original,
            "membership_catalog.parquet": resolution.original,
            "membership_catalog_full.parquet": all_rows,
            "unmatched_members.csv": resolution.unmatched,
        }
        for name, frame in frames.items():
            path = run.run_dir / name
            if frozen is not None and not (name == "unmatched_members.csv" and frozen.coordinate_corrected):
                atomic_copy(frozen.source_files[name], path)
                if sha256_file(path) != frozen.source_hashes[name]:
                    raise ValueError(f"Frozen selection artifact changed while copying: {name}")
            elif path.suffix == ".csv":
                atomic_file(path, lambda temporary, frame=frame: frame.to_csv(temporary, index=False))
            else:
                atomic_file(path, lambda temporary, frame=frame: frame.to_parquet(temporary, index=False))
            run.record_file(name.replace(".", "_"), path, len(frame))
        if members.empty:
            run.index_members(members)
        result = build_corpus_from_members(
            label=label, out_dir=run.run_dir, members=members, input_mode="membership_catalog",
            input_summary=summary, skip_simbad=skip_simbad, sleep_seconds=sleep_seconds,
            enrich=enrich, xp_sampled=xp_sampled, xp_continuous=xp_continuous,
            manifest=run.manifest, gaia_release="dr3", library_run=run, refresh=refresh,
            static_viz_mode=static_viz_mode,
        )
        output = result_to_dict(run.finish(result))
        visible = run.out_dir if run.published else run.run_dir
        output.update({
            "membership_catalog_id": resolution.catalog_id, "membership_table_id": resolution.table_id,
            "membership_table_description": resolution.table_description,
            "membership_catalog_title": resolution.catalog_title,
            "membership_match_method": resolution.match_method,
            "membership_match_radius_arcsec": resolution.match_radius_arcsec,
            "membership_coordinate_descriptor": resolution.coordinate_descriptor,
            "static_viz_mode": static_viz_mode,
            "n_catalog_rows": len(resolution.original), "n_catalog_total_rows": len(all_rows),
            "n_gaia_matched": len(members), "n_unmatched": len(resolution.unmatched),
            "membership_catalog_csv": str(visible / "membership_catalog.csv"),
            "membership_catalog_parquet": str(visible / "membership_catalog.parquet"),
            "unmatched_members_csv": str(visible / "unmatched_members.csv"),
            "filtering_applied": False,
        })
        return output


def _match_by_gaia_id(
    original: pd.DataFrame,
    *,
    gaia: GaiaSource,
    source_col: str,
    coordinate_pair: Optional[list[str]],
    coordinate_descriptor: Optional[dict[str, Any]] = None,
    id_fetcher=None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    work = original.copy()
    work["_membership_gaia_id"] = work[source_col].map(_normalise_gaia_id)
    valid = work["_membership_gaia_id"].notna()
    ids = work.loc[valid, "_membership_gaia_id"].drop_duplicates().tolist()
    gaia_frames: list[pd.DataFrame] = []
    for start in range(0, len(ids), 2000):
        if id_fetcher is not None:
            gaia_frames.append(id_fetcher(ids[start : start + 2000]))
        else:
            result = gaia._query_by_ids(
                SourceIdQuery(source_ids=ids[start : start + 2000]),
                extra_columns=QC_EXTRA_COLUMNS + ["has_xp_sampled", "has_xp_continuous"],
            )
            gaia_frames.append(result.data)
    native = pd.concat(gaia_frames, ignore_index=True) if gaia_frames else pd.DataFrame()
    canonical = _canonicalize(native)
    if canonical.empty:
        unmatched = work.copy()
        member = _prefix_member_columns(work.iloc[0:0])
        return _clean_internal(pd.concat([canonical, member], axis=1)), _clean_internal(unmatched)

    canonical["Gaia"] = canonical["Gaia"].map(_normalise_gaia_id)
    member = _prefix_member_columns(work)
    matched = member.merge(
        canonical,
        left_on="member__membership_gaia_id",
        right_on="Gaia",
        how="inner",
        validate="many_to_one",
    )
    matched = matched.rename(
        columns={"member_membership_catalog_row": "membership_catalog_row"}
    )
    matched_ids = set(matched["membership_catalog_row"].tolist())
    unmatched = work.loc[~work["membership_catalog_row"].isin(matched_ids)].copy()
    matched = _clean_internal(matched)
    matched["membership_match_candidate_count"] = 1

    matched["membership_match_sep_arcsec"] = pd.NA
    if coordinate_pair and coordinate_descriptor and not matched.empty:
        from astropy import units as u
        from astropy.coordinates import SkyCoord

        ra_col, dec_col = coordinate_pair
        catalog_ra = _series_to_degrees(
            matched[f"member_{ra_col}"], coordinate_descriptor["ra_unit"]
        )
        catalog_dec = _series_to_degrees(
            matched[f"member_{dec_col}"], coordinate_descriptor["dec_unit"]
        )
        gaia_ra = pd.to_numeric(matched["RAICRS"], errors="coerce")
        gaia_dec = pd.to_numeric(matched["DEICRS"], errors="coerce")
        valid = _valid_coordinate_mask(catalog_ra, catalog_dec)
        valid &= _valid_coordinate_mask(gaia_ra, gaia_dec)
        if valid.any():
            cat_coord = SkyCoord(
                catalog_ra.loc[valid].to_numpy() * u.deg,
                catalog_dec.loc[valid].to_numpy() * u.deg,
            )
            gaia_coord = SkyCoord(
                gaia_ra.loc[valid].to_numpy() * u.deg,
                gaia_dec.loc[valid].to_numpy() * u.deg,
            )
            matched.loc[valid, "membership_match_sep_arcsec"] = (
                cat_coord.separation(gaia_coord).arcsec
            )
    return matched, _clean_internal(unmatched)


def _match_by_position(
    original: pd.DataFrame,
    *,
    gaia: GaiaSource,
    ra_col: str,
    dec_col: str,
    ra_unit: str = "deg",
    dec_unit: str = "deg",
    radius_arcsec: float,
    batch_size: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    match_input = original.copy()
    match_ra = _series_to_degrees(match_input[ra_col], ra_unit)
    match_dec = _series_to_degrees(match_input[dec_col], dec_unit)
    valid_coordinates = _valid_coordinate_mask(match_ra, match_dec)
    invalid_rows = original.loc[~valid_coordinates].copy()
    if not invalid_rows.empty:
        invalid_rows["membership_match_status"] = "invalid_coordinates"

    match_input = match_input.loc[valid_coordinates].copy()
    match_input[ra_col] = match_ra.loc[valid_coordinates]
    match_input[dec_col] = match_dec.loc[valid_coordinates]
    if match_input.empty:
        native = pd.DataFrame(columns=["membership_catalog_row"])
    else:
        native = gaia.crossmatch_positions(
            match_input,
            ra_col=ra_col,
            dec_col=dec_col,
            row_id_col="membership_catalog_row",
            radius_arcsec=radius_arcsec,
            batch_size=batch_size,
            extra_columns=QC_EXTRA_COLUMNS + ["has_xp_sampled", "has_xp_continuous"],
        )
    canonical = _canonicalize(native)
    member = _prefix_member_columns(original)
    matched = canonical.merge(
        member,
        left_on="membership_catalog_row",
        right_on="member_membership_catalog_row",
        how="inner",
        validate="one_to_one",
    )
    matched = matched.drop(columns=["member_membership_catalog_row"])
    matched = matched.rename(
        columns={
            "match_sep_arcsec": "membership_match_sep_arcsec",
            "match_candidate_count": "membership_match_candidate_count",
        }
    )
    matched_rows = set(matched["membership_catalog_row"].tolist())
    unmatched_valid = original.loc[
        valid_coordinates
        & ~original["membership_catalog_row"].isin(matched_rows)
    ].copy()
    if not unmatched_valid.empty:
        unmatched_valid["membership_match_status"] = "no_gaia_match"
    unmatched = pd.concat([unmatched_valid, invalid_rows]).sort_index()
    return matched, unmatched.reset_index(drop=True)


def _prefix_member_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Namespace every literature column so Gaia canonical names stay stable."""
    return frame.rename(columns={column: f"member_{column}" for column in frame.columns})


def _clean_internal(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.drop(
        columns=["_membership_gaia_id", "member__membership_gaia_id"],
        errors="ignore",
    ).reset_index(drop=True)


def _membership_score(
    *, target: str, catalog_id: str, title: str, query_index: int, rank: int
) -> tuple[int, list[str]]:
    text = f"{catalog_id} {title}".casefold()
    target_text = target.casefold()
    score = max(0, 12 - query_index * 2 - min(rank, 10))
    signals: list[str] = []
    if target_text in text:
        score += 35
        signals.append("target name")
    for term, weight, signal in (
        ("membership", 50, "membership"),
        ("members", 42, "members"),
        ("census", 38, "census"),
        ("candidate", 10, "candidate list"),
        ("gaia", 8, "Gaia-based"),
    ):
        if term in text:
            score += weight
            signals.append(signal)
    if "photometr" in text and not ({"membership", "members"} & set(signals)):
        score -= 10
    return score, signals


def _catalog_metadata(vizier: Vizier, catalog_id: str) -> dict[str, Any]:
    try:
        table = vizier.get_catalog_metadata(catalog=catalog_id)
    except Exception as exc:
        logger.warning("Could not fetch VizieR metadata for %s: %s", catalog_id, exc)
        return {}
    if table is None or len(table) == 0:
        return {}
    row = table.to_pandas().iloc[0].to_dict()
    return {str(key): _json_value(value) for key, value in row.items()}


def _iter_tables(tables: Any) -> list[tuple[str, Any]]:
    keys = list(tables.keys())
    raw_items = (
        [(key, tables[key]) for key in keys]
        if isinstance(tables, Mapping)
        else list(zip(keys, tables))
    )
    items: list[tuple[str, Any]] = []
    for raw_key, table in raw_items:
        candidates = (
            raw_key,
            getattr(table, "meta", {}).get("table_id")
            if isinstance(getattr(table, "meta", None), Mapping)
            else None,
        )
        table_key = next(
            (
                candidate.strip()
                for candidate in candidates
                if isinstance(candidate, str) and candidate.strip()
            ),
            None,
        )
        if table_key is None:
            logger.warning("Skipping VizieR table with no string table ID")
            continue
        items.append((table_key, table))
    return items


def _select_tables(
    tables: Any, *, table_id: Optional[str], require_one: bool
) -> list[tuple[str, Any]]:
    items = _iter_tables(tables)
    if table_id:
        needle = table_id.casefold().rstrip("/")
        selected = [
            item
            for item in items
            if item[0].casefold().rstrip("/") == needle
            or item[0].casefold().rstrip("/").endswith("/" + needle)
        ]
        if not selected:
            available = ", ".join(key for key, _ in items)
            raise LookupError(
                f"VizieR table {table_id!r} not found; available: {available}"
            )
        return selected
    if not require_one:
        return items
    if len(items) == 1:
        return items
    established = [
        item
        for item in items
        if _table_role(str(item[1].meta.get("description", ""))) == "members"
    ]
    if len(established) == 1:
        return established
    available = ", ".join(
        f"{key} ({str(table.meta.get('description', ''))})" for key, table in items
    )
    raise ValueError(
        "The catalogue contains multiple plausible tables; preview it and pass "
        f"table_id explicitly. Available: {available}"
    )


def _table_role(description: str) -> str:
    text = description.casefold()
    if "candidate" in text:
        return "candidates"
    if "member" in text or "membership" in text:
        return "members"
    return "other"


def _detect_columns(table: Any) -> dict[str, Any]:
    columns = list(table.colnames)
    descriptions = {
        name: str(getattr(table[name], "description", "") or "") for name in columns
    }
    dr3: list[str] = []
    older_gaia: list[str] = []
    older_gaia_evidence: list[dict[str, str]] = []
    membership: list[str] = []
    for name in columns:
        normalized = _normalise_name(name)
        description = descriptions[name]
        text = f"{name} {description}".casefold()
        release = _gaia_identifier_release(name, description)
        if release == "dr3":
            dr3.append(name)
        elif release in {"edr3", "dr2"}:
            older_gaia.append(name)
            older_gaia_evidence.append({"column": name, "release": release})
        if any(
            token in text
            for token in (
                "membership",
                "member probability",
                "membership probability",
                "population",
                "candidate flag",
                "member flag",
            )
        ) or normalized in {"pmember", "membership", "member", "pop", "class"}:
            membership.append(name)

    coordinate_descriptor = _find_coordinate_descriptor(table)
    coordinates = (
        [
            coordinate_descriptor["ra_column"],
            coordinate_descriptor["dec_column"],
        ]
        if coordinate_descriptor is not None
        else None
    )
    return {
        "gaia_dr3_id_columns": dr3,
        "older_gaia_id_columns": older_gaia,
        "older_gaia_id_evidence": older_gaia_evidence,
        "coordinate_columns": coordinates,
        "coordinate_descriptor": coordinate_descriptor,
        "coordinate_status": (
            "ready_numeric_angular"
            if coordinate_descriptor is not None
            else "unsupported_or_missing_numeric_angular_pair"
        ),
        "membership_columns": membership,
        "gaia_match_ready": bool(dr3 or coordinates),
        "match_strategy": (
            "gaia_dr3_source_id"
            if dr3
            else "position_to_gaia_dr3"
            if coordinates
            else "unsupported_without_column_mapping"
        ),
    }


def _gaia_identifier_release(name: str, description: str) -> Optional[str]:
    """Return release evidence only for a semantic Gaia source-ID column."""
    normalized = _normalise_name(name)
    text = f"{name} {description}".casefold()
    non_identifier_terms = (
        "proper motion",
        "parallax",
        "magnitude",
        "photometry",
        "flux",
        "error",
        "uncertainty",
        "ruwe",
        "radial velocity",
    )
    normalized_non_identifier_terms = (
        "pmra",
        "pmdec",
        "plx",
        "parallax",
        "mag",
        "flux",
        "ruwe",
        "radialvelocity",
    )
    if any(term in text for term in non_identifier_terms) or any(
        term in normalized for term in normalized_non_identifier_terms
    ):
        return None

    explicit_name = normalized in {
        "gaiadr3",
        "dr3sourceid",
        "gaiadr3sourceid",
        "gaiaedr3",
        "edr3sourceid",
        "gaiaedr3sourceid",
        "gaiadr2",
        "dr2sourceid",
        "gaiadr2sourceid",
    }
    semantic_description = bool(
        re.search(r"\bsource[ _-]*(?:id|identifier)\b", text)
        or re.search(
            r"\bgaia\s*(?:e?dr[23]\s*)?(?:id|identifier)\b",
            text,
        )
    )
    semantic_name = "sourceid" in normalized and "gaia" in normalized
    if not (explicit_name or semantic_description or semantic_name):
        return None

    if "gaiaedr3" in normalized or re.search(r"\bgaia\s+edr3\b", text):
        return "edr3"
    if "gaiadr2" in normalized or re.search(r"\bgaia\s+dr2\b", text):
        return "dr2"
    if "gaiadr3" in normalized or re.search(r"\bgaia\s+dr3\b", text):
        return "dr3"
    return None


def _find_coordinate_descriptor(table: Any) -> Optional[dict[str, Any]]:
    """Select one numeric angular coordinate pair without name collisions."""
    columns = list(table.colnames)
    by_normalized: dict[str, list[str]] = {}
    for name in columns:
        by_normalized.setdefault(_normalise_name(name), []).append(name)

    pair_families = (
        ("raicrs", "deicrs", "ICRS"),
        ("raj2000", "dej2000", "equatorial"),
        ("raj", "dej", "equatorial"),
        ("ra", "dec", "equatorial"),
    )
    ranked: list[tuple[tuple[int, int, int, int], dict[str, Any]]] = []
    for family_rank, (ra_name, dec_name, frame) in enumerate(pair_families):
        for ra_column in by_normalized.get(ra_name, []):
            ra_metadata = _numeric_angular_column(table[ra_column])
            if ra_metadata is None:
                continue
            for dec_column in by_normalized.get(dec_name, []):
                dec_metadata = _numeric_angular_column(table[dec_column])
                if dec_metadata is None:
                    continue
                reference_epoch, epoch_source = _coordinate_epoch(
                    table,
                    ra_column=ra_column,
                    dec_column=dec_column,
                    family=ra_name,
                )
                descriptor = {
                    "ra_column": ra_column,
                    "dec_column": dec_column,
                    "ra_unit": ra_metadata["unit"],
                    "dec_unit": dec_metadata["unit"],
                    "ra_dtype": ra_metadata["dtype"],
                    "dec_dtype": dec_metadata["dtype"],
                    "match_unit": "deg",
                    "frame": frame,
                    "reference_epoch": reference_epoch,
                    "epoch_source": epoch_source,
                    "selection_reason": "numeric columns with explicit angular units",
                }
                exact_rank = _coordinate_exact_name_rank(
                    ra_column, dec_column, family=ra_name
                )
                ranked.append(
                    (
                        (
                            family_rank,
                            exact_rank,
                            columns.index(ra_column),
                            columns.index(dec_column),
                        ),
                        descriptor,
                    )
                )
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0])
    return ranked[0][1]


def _numeric_angular_column(column: Any) -> Optional[dict[str, str]]:
    dtype = getattr(column, "dtype", None)
    kind = getattr(dtype, "kind", "")
    if kind not in {"i", "u", "f"}:
        return None
    unit = getattr(column, "unit", None)
    if unit is None:
        return None
    try:
        from astropy import units as u

        parsed_unit = u.Unit(unit)
    except (TypeError, ValueError):
        return None
    if not parsed_unit.is_equivalent(u.deg):
        return None
    return {"dtype": str(dtype), "unit": str(parsed_unit)}


def _coordinate_exact_name_rank(
    ra_column: str, dec_column: str, *, family: str
) -> int:
    pair = (ra_column.casefold(), dec_column.casefold())
    preferred = {
        "raicrs": (("ra_icrs", "de_icrs"), ("raicrs", "deicrs")),
        "raj2000": (("raj2000", "dej2000"),),
        "raj": (("raj", "dej"),),
        "ra": (("ra", "dec"),),
    }
    try:
        return preferred.get(family, ()).index(pair)
    except ValueError:
        return len(preferred.get(family, ())) + 1


def _coordinate_epoch(
    table: Any,
    *,
    ra_column: str,
    dec_column: str,
    family: str,
) -> tuple[Optional[str], str]:
    descriptions = " ".join(
        str(getattr(table[name], "description", "") or "")
        for name in (ra_column, dec_column)
    )
    # J2000 column names describe the coordinate equinox, not an observation epoch.
    match = re.search(
        r"\b(?:reference\s+)?epoch\s*[=:]?\s*([JB]?\d{4}(?:\.\d+)?)\b",
        descriptions,
        re.IGNORECASE,
    )
    if match:
        return match.group(1), "column_descriptions"
    return None, "not_declared"


def _column_metadata(table: Any) -> list[dict[str, Any]]:
    out = []
    for name in table.colnames:
        column = table[name]
        out.append(
            {
                "name": name,
                "dtype": str(column.dtype),
                "unit": str(column.unit) if column.unit is not None else None,
                "description": str(column.description or ""),
            }
        )
    return out


def _table_to_dataframe(table: Any) -> pd.DataFrame:
    frame = table.to_pandas()
    for column in frame.columns:
        if frame[column].map(lambda value: isinstance(value, bytes)).any():
            frame[column] = frame[column].map(
                lambda value: value.decode("utf-8", errors="replace")
                if isinstance(value, bytes)
                else value
            )
    return frame


def _series_to_degrees(values: pd.Series, unit: str) -> pd.Series:
    from astropy import units as u

    numeric = pd.to_numeric(values, errors="coerce")
    array = numeric.to_numpy(dtype=float, na_value=float("nan"))
    converted = (array * u.Unit(unit)).to_value(u.deg)
    return pd.Series(converted, index=values.index, dtype=float)


def _valid_coordinate_mask(ra: pd.Series, dec: pd.Series) -> pd.Series:
    import numpy as np

    return pd.Series(
        np.isfinite(ra.to_numpy(dtype=float, na_value=float("nan")))
        & np.isfinite(dec.to_numpy(dtype=float, na_value=float("nan")))
        & (ra.to_numpy(dtype=float, na_value=float("nan")) >= 0.0)
        & (ra.to_numpy(dtype=float, na_value=float("nan")) < 360.0)
        & (dec.to_numpy(dtype=float, na_value=float("nan")) >= -90.0)
        & (dec.to_numpy(dtype=float, na_value=float("nan")) <= 90.0),
        index=ra.index,
    )


def _catalog_id_from_search_resource(
    raw_catalog_id: Any, resource: Any
) -> Optional[str]:
    """Recover only explicitly named and syntactically valid VizieR IDs."""
    candidates = (
        raw_catalog_id,
        getattr(resource, "name", None),
        getattr(resource, "ID", None),
    )
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        try:
            validated = _validate_vizier_id(candidate, "catalog_id")
        except ValueError:
            continue
        if "/" in validated:
            return validated
    return None


def _validate_static_viz_mode(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("static_viz_mode must be embedded, status_only, or off")
    value = value.strip().lower()
    if value not in {"embedded", "status_only", "off"}:
        raise ValueError("static_viz_mode must be embedded, status_only, or off")
    return value


def _normalise_gaia_id(value: Any) -> Optional[str]:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, float):
        if not value.is_integer() or abs(value) >= 2**53:
            return None
        value = int(value)
    text = str(value).strip()
    text = re.sub(r"^Gaia\s+DR3\s+", "", text, flags=re.IGNORECASE)
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    return (text.lstrip("0") or "0") if text.isascii() and text.isdigit() else None


def _normalise_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _validate_vizier_id(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Invalid VizieR {name}: expected a string")
    value = value.strip()
    if not value or not re.fullmatch(r"[A-Za-z0-9+_.-]+(?:/[A-Za-z0-9+_.-]+)*", value):
        raise ValueError(f"Invalid VizieR {name}: {value!r}")
    return value


def _json_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        {str(key): _json_value(value) for key, value in row.items()}
        for row in frame.to_dict(orient="records")
    ]


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        missing = pd.isna(value)
        if isinstance(missing, bool) and missing:
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except (TypeError, ValueError):
            pass
    return value


def resolution_metadata(resolution: MembershipResolution) -> dict[str, Any]:
    """Serialize non-tabular resolution metadata for manifests/tests."""
    return {
        name: getattr(resolution, name)
        for name in (
            "catalog_id", "table_id", "table_description", "catalog_title",
            "match_method", "match_radius_arcsec", "table_schema",
            "coordinate_descriptor",
        )
    }
