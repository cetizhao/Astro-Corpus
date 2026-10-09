#!/usr/bin/env python3
"""Collect the fixed CrA and Upper Sco published-member samples.

The runner is intentionally thin: it calls ``build_membership_corpus`` with
verified catalogue/table presets, keeps smoke and full labels separate, and
relies on the existing corpus/library checkpoints for ordinary resume.  It
never enables refresh and never performs a geometric/full-sky Gaia download.

Progress and final reports are emitted as JSON Lines on standard output.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Sequence

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astro_pipeline.library.refs import (
    ProductReferenceError,
    read_reference_documents,
    resolve_product,
)


@dataclass(frozen=True)
class RegionPreset:
    key: str
    catalog_id: str
    table_id: str
    expected_rows: int
    smoke_label: str
    full_label: str


REGIONS: dict[str, RegionPreset] = {
    "cra": RegionPreset(
        key="cra",
        catalog_id="J/AJ/163/64",
        table_id="J/AJ/163/64/table1",
        expected_rows=393,
        smoke_label="cra_esplin2022_table1_smoke",
        full_label="cra_esplin2022_table1_full",
    ),
    "uppersco": RegionPreset(
        key="uppersco",
        catalog_id="J/AJ/160/44",
        table_id="J/AJ/160/44/table5",
        expected_rows=1761,
        smoke_label="uppersco_luhman2020_table5_smoke",
        full_label="uppersco_luhman2020_table5_full",
    ),
}

_STAGE_ORDER = {
    "members": 0,
    "sampled": 1,
    "continuous": 2,
    "papers": 3,
    "enrich": 4,
    "all": 5,
}
_PRODUCT_TYPES = {
    "sampled": ("XP_SAMPLED", "has_xp_sampled"),
    "continuous": ("XP_CONTINUOUS", "has_xp_continuous"),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _retry_count(value: str) -> int:
    parsed = int(value)
    if parsed < 0 or parsed > 3:
        raise argparse.ArgumentTypeError("retry passes must be between 0 and 3")
    return parsed


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Collect fixed published-member tables for Corona Australis and "
            "Upper Sco using resumable source-ID DataLink products."
        )
    )
    parser.add_argument(
        "--target",
        choices=("cra", "uppersco", "all"),
        default="all",
        help="fixed region preset to run (default: all, sequentially)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="use the full-table label and limit=None instead of a smoke sample",
    )
    parser.add_argument(
        "--smoke-limit",
        type=_positive_int,
        default=10,
        help="catalogue rows for each smoke label (default: 10; ignored by --full)",
    )
    parser.add_argument(
        "--stage",
        choices=tuple(_STAGE_ORDER),
        default="members",
        help="cumulative stage target (default: members)",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path("data"),
        help="corpus parent directory (default: data)",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=_nonnegative_float,
        default=0.3,
        help="polite SIMBAD pause inherited by the existing builder",
    )
    parser.add_argument(
        "--retry-passes",
        type=_retry_count,
        default=1,
        help="bounded ordinary-resume passes after the first attempt (default: 1)",
    )
    parser.add_argument(
        "--attempt-timeout-seconds", type=_positive_int, default=3600,
        help="hard process deadline per attempt; checkpoints survive termination (default: 3600)",
    )
    parser.add_argument(
        "--static-viz-mode",
        choices=("status_only", "embedded", "off"),
        default="status_only",
        help="static corpus HTML mode (default: status_only)",
    )
    parser.add_argument(
        "--resume-selection-run-id",
        help=("reuse a verified, published membership selection from this corpus "
              "without VizieR/Gaia positional queries; requires one --target"),
    )
    return parser


def _stage_options(stage: str) -> dict[str, bool]:
    rank = _STAGE_ORDER[stage]
    papers = rank >= _STAGE_ORDER["papers"]
    return {
        "xp_sampled": rank >= _STAGE_ORDER["sampled"],
        "xp_continuous": rank >= _STAGE_ORDER["continuous"],
        "skip_simbad": not papers,
        "enrich": rank >= _STAGE_ORDER["enrich"],
    }


def _selected_presets(target: str) -> list[RegionPreset]:
    if target == "all":
        return [REGIONS["cra"], REGIONS["uppersco"]]
    return [REGIONS[target]]


def _job_identity(
    preset: RegionPreset, *, full: bool, smoke_limit: int
) -> tuple[str, int | None]:
    if full:
        return preset.full_label, None
    return preset.smoke_label, smoke_limit


def _emit(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str), flush=True)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _read_frame(path: Path | None) -> pd.DataFrame | None:
    if path is None or not path.is_file():
        return None
    try:
        if path.suffix.lower() == ".parquet":
            return pd.read_parquet(path)
        if path.suffix.lower() == ".csv":
            return pd.read_csv(path)
    except Exception:  # report unreadable/corrupt artifacts as unknown
        return None
    return None


def _result_path(
    result: Mapping[str, Any], key: str, fallback: Path
) -> Path | None:
    value = result.get(key)
    if isinstance(value, str) and value.strip():
        return Path(value)
    return fallback if fallback.is_file() else None


@dataclass(frozen=True)
class _ReportSnapshot:
    directory: Path | None
    manifest_path: Path | None
    manifest: dict[str, Any] | None
    run_id: str
    active_run_id: str
    shared: bool


def _report_snapshot(
    corpus_dir: Path,
    result: Mapping[str, Any],
    *,
    error: str | None,
    previous_run_id: str | None,
) -> _ReportSnapshot:
    """Pin every report artifact to the attempted run, not the published view."""
    manifest_path = _result_path(result, "manifest_json", corpus_dir / "manifest.json")
    manifest = _read_json(manifest_path) if manifest_path is not None else None
    values = manifest or {}
    storage = values.get("storage", {})
    shared = (
        isinstance(storage, Mapping) and storage.get("kind") == "shared_library"
    ) or result.get("storage_mode") == "shared_library" or (corpus_dir / "runs").is_dir()
    active_run_id = str(values.get("active_run_id") or "")
    run_id = result.get("run_id") or values.get("snapshot_run_id") or values.get("current_run_id")
    if not shared:
        return _ReportSnapshot(
            corpus_dir, manifest_path, manifest,
            str(run_id or active_run_id), active_run_id, False,
        )
    # A child can fail before starting a run (preflight, lease acquisition, etc.).
    # In that case the unchanged previous pointer is not this attempt's identity.
    if error and not result.get("run_id") and run_id == previous_run_id:
        run_id = None
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
        return _ReportSnapshot(None, None, None, "", active_run_id, True)
    root = corpus_dir.resolve()
    run_root = (root / "runs").resolve()
    directory = (run_root / run_id).resolve()
    if run_root.parent != root or directory.parent != run_root or not directory.is_dir():
        return _ReportSnapshot(None, None, None, run_id, active_run_id, True)
    snapshot_path = directory / "manifest.json"
    if snapshot_path.is_file():
        snapshot = _read_json(snapshot_path)
        if snapshot is None or snapshot.get("snapshot_run_id") != run_id:
            return _ReportSnapshot(None, snapshot_path, None, run_id, active_run_id, True)
        manifest_path, manifest = snapshot_path, snapshot
    elif values.get("current_run_id") != run_id:
        # Unfinished runs have no snapshot manifest yet.  Only their own root
        # working manifest may supply stage status; a successor's may not.
        manifest_path, manifest = None, None
    return _ReportSnapshot(directory, manifest_path, manifest, run_id, active_run_id, True)


def _string_ids(values: pd.Series) -> list[str]:
    return sorted(
        {
            str(value).strip()
            for value in values.dropna().tolist()
            if str(value).strip()
        }
    )


def _availability_ids(
    availability: pd.DataFrame | None, flag_column: str
) -> list[str] | None:
    if availability is None:
        return None
    if "source_id" not in availability or flag_column not in availability:
        return None
    mask = (
        availability[flag_column]
        .astype(str)
        .str.strip()
        .str.casefold()
        .isin({"true", "1"})
    )
    return _string_ids(availability.loc[mask, "source_id"])


def _registered_product_ids(
    corpus_dir: Path | None, retrieval_type: str
) -> tuple[list[str], list[str]]:
    if corpus_dir is None:
        return [], []
    ready: set[str] = set()
    non_ready: set[str] = set()
    try:
        documents = read_reference_documents(corpus_dir)
    except (OSError, ProductReferenceError, ValueError):
        return [], []
    for document in documents:
        contract = document.get("contract", {})
        if not isinstance(contract, Mapping):
            continue
        if str(contract.get("retrieval_type", "")).strip().upper() != retrieval_type:
            continue
        for item in document.get("items", []):
            if not isinstance(item, Mapping):
                continue
            identifier = item.get("identifier")
            if not isinstance(identifier, (str, int)):
                continue
            identifier_text = str(identifier).strip()
            if not identifier_text:
                continue
            if str(item.get("status", "")).strip().casefold() == "ready":
                ready.add(identifier_text)
            else:
                non_ready.add(identifier_text)
    return sorted(ready), sorted(non_ready)


def _product_report(
    *,
    corpus_dir: Path | None,
    availability: pd.DataFrame | None,
    requested: bool,
    product: str,
) -> dict[str, Any]:
    retrieval_type, flag_column = _PRODUCT_TYPES[product]
    available_ids = _availability_ids(availability, flag_column)
    registered_ready, registered_non_ready = _registered_product_ids(
        corpus_dir, retrieval_type
    )
    candidates = available_ids if available_ids is not None else registered_ready
    ready_ids: list[str] = []
    verification_errors: dict[str, str] = {}
    for identifier in candidates:
        try:
            resolve_product(
                corpus_dir,
                identifier,
                retrieval_type,
                release="dr3",
            )
        except Exception as exc:  # report corrupt/missing artifacts without aborting
            verification_errors[identifier] = str(exc)
        else:
            ready_ids.append(identifier)
    ready_set = set(ready_ids)
    pending_ids = (
        sorted(set(available_ids) - ready_set)
        if available_ids is not None
        else None
    )
    return {
        "requested": requested,
        "retrieval_type": retrieval_type,
        "availability_known": available_ids is not None,
        "available_ids": available_ids,
        "available_count": len(available_ids) if available_ids is not None else None,
        "ready_ids": sorted(ready_ids),
        "ready_count": len(ready_ids),
        "pending_ids": pending_ids,
        "pending_count": len(pending_ids) if pending_ids is not None else None,
        "registered_non_ready_ids": registered_non_ready,
        "verification_errors": verification_errors,
    }


def _simbad_report(
    *, corpus_dir: Path | None, source_ids: list[str], requested: bool
) -> tuple[dict[str, Any], pd.DataFrame | None]:
    checkpoint = (
        _read_json(corpus_dir / "simbad_refs.checkpoint.json") if corpus_dir is not None else None
    )
    refs = _read_frame(corpus_dir / "simbad_refs.parquet" if corpus_dir is not None else None)
    target = set(source_ids)
    if checkpoint is None:
        completed: set[str] | None = None
        failures: dict[str, str] | None = None
        pending: list[str] | None = None
    else:
        completed = {
            str(value)
            for value in checkpoint.get("completed", [])
            if str(value) in target
        }
        raw_failures = checkpoint.get("failed", {})
        failures = (
            {
                str(key): str(value)
                for key, value in raw_failures.items()
                if str(key) in target
            }
            if isinstance(raw_failures, Mapping)
            else {}
        )
        pending = sorted(target - completed)
    sources_with_refs = (
        set(_string_ids(refs["gaia_id"]))
        if refs is not None and "gaia_id" in refs
        else set()
    )
    zero_reference_ids = (
        sorted(completed - sources_with_refs) if completed is not None else None
    )
    report = {
        "requested": requested,
        "checkpoint_known": checkpoint is not None,
        "target_source_count": len(target),
        "completed_ids": sorted(completed) if completed is not None else None,
        "completed_count": len(completed) if completed is not None else None,
        "failed": failures,
        "failed_count": len(failures) if failures is not None else None,
        "pending_ids": pending,
        "pending_count": len(pending) if pending is not None else None,
        "sources_with_references_ids": sorted(sources_with_refs),
        "sources_with_references_count": len(sources_with_refs),
        "zero_reference_ids": zero_reference_ids,
        "zero_reference_count": (
            len(zero_reference_ids) if zero_reference_ids is not None else None
        ),
    }
    return report, refs


def _enrichment_report(
    refs: pd.DataFrame | None, *, requested: bool
) -> dict[str, Any]:
    if refs is None or "bibcode" not in refs:
        return {
            "requested": requested,
            "metadata_known": False,
            "unique_bibcodes": None,
            "with_title": None,
            "with_doi": None,
            "with_arxiv": None,
            "with_year": None,
        }
    unique = refs.drop_duplicates(subset=["bibcode"]).copy()

    def populated(column: str) -> int:
        if column not in unique:
            return 0
        return int(
            unique[column]
            .fillna("")
            .astype(str)
            .str.strip()
            .ne("")
            .sum()
        )

    total = int(unique["bibcode"].nunique())
    return {
        "requested": requested,
        "metadata_known": True,
        "unique_bibcodes": total,
        "with_title": populated("title"),
        "with_doi": populated("doi"),
        "with_arxiv": populated("arxiv_id"),
        "with_year": populated("year"),
    }


def _enrichment_status_report(
    directory: Path | None, *, run_id: str | None, requested: bool,
) -> dict[str, Any]:
    if not requested:
        return {"known": True, "status": "not_requested"}
    path = directory / "simbad_refs.enrichment.json" if directory is not None else None
    observation = _read_json(path) if path is not None else None
    if observation is None or observation.get("schema_version") != 1:
        return {"known": False, "status": "unknown", "error": "No valid enrichment observation"}
    if run_id is not None and observation.get("run_id") != run_id:
        return {"known": False, "status": "unknown", "error": "Enrichment observation belongs to another run"}
    return {**observation, "known": True, "path": str(path)}


def _enrichment_complete(
    coverage: Mapping[str, Any], observation: Mapping[str, Any] | None,
) -> bool:
    if not coverage.get("metadata_known"):
        return False
    total = coverage.get("unique_bibcodes")
    if coverage.get("with_title") != total:
        return False
    if total == 0:
        return True
    if not observation or not observation.get("known") or observation.get("status") != "completed":
        return False
    metadata, doi = observation.get("metadata"), observation.get("doi")
    if not isinstance(metadata, Mapping) or not isinstance(doi, Mapping):
        return False
    if (
        metadata.get("status") != "completed"
        or metadata.get("total") != total
        or metadata.get("completed") != total
        or metadata.get("failed")
    ):
        return False
    return (
        doi.get("status") == "completed"
        and isinstance(doi.get("total"), int)
        and doi["total"] >= 0
        and doi.get("completed") == doi["total"]
        and not doi.get("failed")
        and not doi.get("deferred")
        and not doi.get("rate_limited")
    )


def _requested_stages_complete(
    *,
    stage: str,
    manifest: Mapping[str, Any] | None,
    products: Mapping[str, Mapping[str, Any]],
    simbad: Mapping[str, Any],
    enrichment: Mapping[str, Any],
    artifacts_known: bool,
    enrichment_observation: Mapping[str, Any] | None = None,
) -> bool:
    if manifest is None or not artifacts_known:
        return False
    stages = manifest.get("stages", {})
    if not isinstance(stages, Mapping):
        return False
    for required in ("resolve_members", "save_members"):
        if stages.get(required, {}).get("status") != "completed":
            return False
    rank = _STAGE_ORDER[stage]
    for product, required_rank in (("sampled", 1), ("continuous", 2)):
        if rank < required_rank:
            continue
        product_report = products[product]
        if not product_report.get("availability_known"):
            return False
        if product_report.get("pending_count") != 0:
            return False
    if rank >= _STAGE_ORDER["papers"]:
        if not simbad.get("checkpoint_known"):
            return False
        if simbad.get("pending_count") != 0 or simbad.get("failed_count") != 0:
            return False
        if stages.get("simbad_papers", {}).get("status") != "completed":
            return False
    if rank >= _STAGE_ORDER["enrich"] and not _enrichment_complete(enrichment, enrichment_observation):
        return False
    return True


def _match_audit(
    members: pd.DataFrame | None, unmatched: pd.DataFrame | None,
) -> dict[str, Any]:
    if members is None:
        return {"known": False}
    separations = pd.to_numeric(
        members.get("membership_match_sep_arcsec", pd.Series(dtype=float)),
        errors="coerce",
    )
    candidates = pd.to_numeric(
        members.get("membership_match_candidate_count", pd.Series(dtype=float)),
        errors="coerce",
    )
    duplicate_ids = (
        members["Gaia"].astype(str).value_counts()
        if "Gaia" in members else pd.Series(dtype=int)
    )
    reasons = (
        unmatched["membership_match_status"].value_counts().to_dict()
        if unmatched is not None and "membership_match_status" in unmatched else {}
    )
    return {
        "known": True,
        "multiple_candidate_rows": int((candidates > 1).sum()),
        "separation_over_1_arcsec_rows": int((separations > 1).sum()),
        "maximum_separation_arcsec": (
            float(separations.max()) if separations.notna().any() else None
        ),
        "duplicate_source_occurrences": {
            str(key): int(value) for key, value in duplicate_ids.items() if value > 1
        },
        "unmatched_reasons": {str(key): int(value) for key, value in reasons.items()},
        "note": (
            "Nearest positional candidate within 3 arcsec; no membership filtering "
            "or proper-motion epoch propagation. Inspect ambiguous and wide matches."
        ),
    }


def _build_report(
    *,
    preset: RegionPreset,
    label: str,
    full: bool,
    limit: int | None,
    stage: str,
    corpus_dir: Path,
    result: Mapping[str, Any] | None,
    error: str | None,
    previous_run_id: str | None = None,
) -> dict[str, Any]:
    result = result or {}
    result_error = result.get("error")
    effective_error = error or (str(result_error) if result_error else None)
    snapshot = _report_snapshot(
        corpus_dir, result, error=effective_error, previous_run_id=previous_run_id,
    )
    report_dir = snapshot.directory
    manifest_path, manifest = snapshot.manifest_path, snapshot.manifest

    def artifact_path(key: str, relative: str) -> Path | None:
        if report_dir is None:
            return None
        fallback = report_dir / relative
        # Returned compatibility-root paths are not immutable run pins.
        return fallback if snapshot.shared else _result_path(result, key, fallback)

    original = _read_frame(artifact_path("membership_catalog_parquet", "membership_catalog.parquet"))
    members = _read_frame(artifact_path("members_parquet", "members.parquet"))
    unmatched = _read_frame(artifact_path("unmatched_members_csv", "unmatched_members.csv"))
    original_rows = len(original) if original is not None else result.get("n_catalog_rows")
    matched_rows = len(members) if members is not None else result.get("n_gaia_matched")
    unmatched_rows = len(unmatched) if unmatched is not None else result.get("n_unmatched")
    source_ids = (
        _string_ids(members["Gaia"])
        if members is not None and "Gaia" in members
        else []
    )
    unique_gaia_rows = len(source_ids) if members is not None else result.get("n_unique_sources")

    xp_summary = result.get("xp_products", {})
    availability_path: Path | None = None
    if not snapshot.shared and isinstance(xp_summary, Mapping):
        raw_path = xp_summary.get("availability_path")
        if isinstance(raw_path, str) and raw_path.strip():
            availability_path = Path(raw_path)
    if availability_path is None and report_dir is not None:
        availability_path = report_dir / "catalogs" / "gaia_xp_availability.parquet"
    availability = _read_frame(availability_path)
    options = _stage_options(stage)
    products = {
        product: _product_report(
            corpus_dir=report_dir,
            availability=availability,
            requested=options[f"xp_{product}"],
            product=product,
        )
        for product in ("sampled", "continuous")
    }
    simbad, refs = _simbad_report(
        corpus_dir=report_dir,
        source_ids=source_ids,
        requested=not options["skip_simbad"],
    )
    enrichment = _enrichment_report(refs, requested=options["enrich"])
    enrichment_observation = _enrichment_status_report(
        report_dir, run_id=snapshot.run_id if snapshot.shared else None,
        requested=options["enrich"],
    )

    manifest_status = manifest.get("status") if manifest is not None else None
    run_id = snapshot.run_id
    row_reconciliation_known = all(
        isinstance(value, int)
        for value in (original_rows, matched_rows, unmatched_rows)
    )
    row_reconciled = (
        matched_rows + unmatched_rows == original_rows
        if row_reconciliation_known
        else None
    )
    full_table_complete = (
        original_rows == preset.expected_rows if full and original_rows is not None else None
    )
    stages_complete = _requested_stages_complete(
        stage=stage,
        manifest=manifest,
        products=products,
        simbad=simbad,
        enrichment=enrichment,
        enrichment_observation=enrichment_observation,
        artifacts_known=all(
            frame is not None for frame in (original, members, unmatched)
        ),
    )
    if effective_error:
        lowered = effective_error.casefold()
        completion = (
            "blocked"
            if any(
                token in lowered
                for token in ("credential", "authentication", "login", "unauthorized")
            )
            else "failed"
        )
    elif (
        manifest_status == "completed"
        and stages_complete
        and row_reconciled is True
        and full_table_complete is not False
    ):
        completion = "completed"
    else:
        completion = "partial"

    return {
        "target": preset.key,
        "label": label,
        "run_id": run_id or "",
        "active_run_id": snapshot.active_run_id,
        "collection_scope": "full_published_table" if full else "smoke_sample",
        "data_scope": "published_membership_table_not_full_sky",
        "gaia_product_scope": "explicit_matched_source_ids_via_datalink",
        "catalog_id": preset.catalog_id,
        "table_id": preset.table_id,
        "expected_full_table_rows": preset.expected_rows,
        "requested_limit": limit,
        "requested_stage": stage,
        "completion": completion,
        "manifest_status": manifest_status,
        "error": effective_error,
        "counts": {
            "original_rows": original_rows,
            "matched_rows": matched_rows,
            "unmatched_rows": unmatched_rows,
            "unique_gaia_rows": unique_gaia_rows,
            "duplicate_matched_rows": (
                matched_rows - unique_gaia_rows
                if isinstance(matched_rows, int)
                and isinstance(unique_gaia_rows, int)
                else None
            ),
            "row_reconciled": row_reconciled,
            "full_table_complete": full_table_complete,
        },
        "matching": _match_audit(members, unmatched),
        "xp": products,
        "simbad": simbad,
        "enrichment": enrichment,
        "enrichment_observation": enrichment_observation,
        "manifest_path": str(manifest_path) if manifest_path is not None else "",
        "reported_at": _utc_now(),
    }


def _bounded_build(*, timeout_seconds: int, **kwargs) -> dict[str, Any]:
    code = (
        "import json,sys\n"
        "from pathlib import Path\n"
        "from astro_pipeline.membership import build_membership_corpus\n"
        "request=json.load(sys.stdin)\n"
        "result=build_membership_corpus(**request['kwargs'])\n"
        "Path(request['result_path']).write_text(json.dumps(result,default=str))\n"
    )
    with tempfile.TemporaryDirectory(prefix="astro-collection-") as directory:
        result_path = Path(directory) / "result.json"
        subprocess.run(
            [sys.executable, "-c", code],
            input=json.dumps({"kwargs": kwargs, "result_path": str(result_path)}, default=str),
            text=True, check=True, timeout=timeout_seconds,
        )
        return json.loads(result_path.read_text())


def _run_job(
    *,
    preset: RegionPreset,
    args: argparse.Namespace,
) -> dict[str, Any]:
    label, limit = _job_identity(
        preset,
        full=bool(args.full),
        smoke_limit=int(args.smoke_limit),
    )
    corpus_dir = args.data_root / label
    options = _stage_options(args.stage)
    total_passes = 1 + int(args.retry_passes)
    last_report: dict[str, Any] | None = None
    for attempt in range(1, total_passes + 1):
        _emit(
            {
                "event": "collection_attempt_started",
                "target": preset.key,
                "label": label,
                "attempt": attempt,
                "max_attempts": total_passes,
                "stage": args.stage,
                "refresh": False,
                "started_at": _utc_now(),
            }
        )
        result: Mapping[str, Any] | None = None
        error: str | None = None
        previous_manifest = _read_json(corpus_dir / "manifest.json") or {}
        previous_run_id = previous_manifest.get("current_run_id")
        try:
            result = _bounded_build(
                timeout_seconds=args.attempt_timeout_seconds,
                label=label,
                out_dir=corpus_dir,
                catalog_id=preset.catalog_id,
                table_id=preset.table_id,
                limit=limit,
                match_radius_arcsec=3.0,
                skip_simbad=options["skip_simbad"],
                sleep_seconds=args.sleep_seconds,
                enrich=options["enrich"],
                xp_sampled=options["xp_sampled"],
                xp_continuous=options["xp_continuous"],
                refresh=False,
                resume_selection_run_id=args.resume_selection_run_id,
                static_viz_mode=args.static_viz_mode,
            )
        except Exception as exc:  # bounded retry/report boundary
            error = str(exc)
        last_report = _build_report(
            preset=preset,
            label=label,
            full=bool(args.full),
            limit=limit,
            stage=args.stage,
            corpus_dir=corpus_dir,
            result=result,
            error=error,
            previous_run_id=previous_run_id,
        )
        _emit(
            {
                "event": "collection_attempt_finished",
                "attempt": attempt,
                "max_attempts": total_passes,
                "report": last_report,
            }
        )
        if last_report["completion"] == "completed":
            break
    assert last_report is not None
    return last_report


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.resume_selection_run_id and args.target == "all":
        _parser().error("--resume-selection-run-id requires one --target (cra or uppersco)")
    reports = [
        _run_job(preset=preset, args=args)
        for preset in _selected_presets(args.target)
    ]
    if reports and all(report["completion"] == "completed" for report in reports):
        overall = "completed"
    elif any(report["completion"] == "blocked" for report in reports):
        overall = "blocked"
    elif any(report["completion"] == "failed" for report in reports):
        overall = "failed"
    else:
        overall = "partial"
    _emit(
        {
            "event": "collection_report",
            "overall": overall,
            "reports": reports,
            "finished_at": _utc_now(),
        }
    )
    return 0 if overall == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
