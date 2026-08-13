"""Resumable HEALPix/keyset downloader for the Gaia DR3 main source table.

This module preserves the ability to materialise all rows of
``gaiadr3.gaia_source`` without pretending that a laptop should do so in one
query. Gaia ``source_id`` encodes a level-12 nested HEALPix in its high bits,
so every lower-level HEALPix maps to a deterministic, non-overlapping numeric
ID interval. Each interval is then keyset-paginated by ``source_id`` and saved
as Parquet parts.

Important scope boundary: "all" here means all rows/selected columns in the
Gaia DR3 *main source table*. Auxiliary tables, epoch photometry, XP/RVS data
products and other DataLink products are separate datasets. Corpus XP products
are handled by :mod:`astro_pipeline.corpus_products`.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

from astro_pipeline import __version__
from astro_pipeline.config import PipelineConfig
from astro_pipeline.intake import QC_EXTRA_COLUMNS
from astro_pipeline.manifest import validate_label
from astro_pipeline.sources.gaia import GaiaSource

logger = logging.getLogger(__name__)

PLAN_SCHEMA_VERSION = "1.0"
SOURCE_ID_LEVEL = 12
SOURCE_ID_LEVEL12_DIVISOR = 2**35
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def healpix_count(level: int) -> int:
    """Number of nested HEALPix pixels at a level (12 * 4**level)."""
    _validate_level(level)
    return 12 * 4**level


def source_id_divisor(level: int) -> int:
    """Integer divisor that extracts a HEALPix index from Gaia source_id."""
    _validate_level(level)
    return SOURCE_ID_LEVEL12_DIVISOR * 4 ** (SOURCE_ID_LEVEL - level)


def source_id_bounds(level: int, pixel: int) -> tuple[int, int]:
    """Inclusive lower/exclusive upper source_id bounds for one HEALPix."""
    count = healpix_count(level)
    if pixel < 0 or pixel >= count:
        raise ValueError(f"pixel must be in [0, {count}); got {pixel}")
    divisor = source_id_divisor(level)
    return pixel * divisor, (pixel + 1) * divisor


def create_bulk_plan(
    *,
    label: str,
    root: Path | str,
    healpix_level: int = 5,
    pixel_start: int = 0,
    pixel_stop: Optional[int] = None,
    columns_mode: str = "default",
    custom_columns: Optional[list[str]] = None,
    page_size: int = 100_000,
) -> dict[str, Any]:
    """Create or reopen a non-destructive Gaia bulk-download plan.

    ``pixel_stop=None`` selects all remaining pixels. No network request or
    Gaia data download happens here. Reusing a label with different immutable
    settings is rejected to prevent mixed datasets.
    """
    label = validate_label(label)
    root = Path(root)
    if root.name != label:
        raise ValueError(
            f"bulk plan root directory name must match label {label!r}; got {root.name!r}"
        )
    level_count = healpix_count(healpix_level)
    stop = level_count if pixel_stop is None or pixel_stop == 0 else pixel_stop
    if pixel_start < 0 or stop <= pixel_start or stop > level_count:
        raise ValueError(
            f"pixel range must satisfy 0 <= start < stop <= {level_count}; "
            f"got [{pixel_start}, {stop})"
        )
    if page_size < 1 or page_size > 1_000_000:
        raise ValueError("page_size must be between 1 and 1,000,000")

    config = PipelineConfig.load()
    columns = _resolve_columns(
        columns_mode,
        custom_columns=custom_columns,
        default_columns=list(config.gaia.default_columns),
    )
    plan_path = root / "bulk_plan.json"
    now = _utc_now()
    immutable = {
        "data_release": "Gaia DR3",
        "table": f"{config.gaia.db_schema}.gaia_source",
        "healpix_level": healpix_level,
        "pixel_start": pixel_start,
        "pixel_stop": stop,
        "columns_mode": columns_mode,
        "columns": columns,
        "page_size": page_size,
        "format": "parquet",
    }

    if plan_path.exists():
        existing = json.loads(plan_path.read_text(encoding="utf-8"))
        existing_immutable = existing.get("selection", {})
        if existing_immutable != immutable:
            raise ValueError(
                f"Bulk label {label!r} already exists with different settings. "
                "Choose a new label; mixing partitions from two plans is refused."
            )
        return _plan_summary(existing, plan_path)

    total = stop - pixel_start
    plan = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "pipeline_version": __version__,
        "kind": "gaia_source_healpix_bulk",
        "label": label,
        "status": "planned",
        "created_at": now,
        "updated_at": now,
        "selection": immutable,
        "progress": {
            "next_pixel": pixel_start,
            "chunks_total": total,
            "chunks_completed": 0,
            "chunks_failed": 0,
            "rows_downloaded": 0,
            "files_written": 0,
            "failed_pixels": [],
        },
        "scope_note": (
            "Covers gaiadr3.gaia_source only. Gaia auxiliary tables and "
            "DataLink products are separate downloads."
        ),
        "safety": {
            "create_plan_downloads_data": False,
            "worker_default_chunks_per_call": 1,
            "all_remaining_requires_max_chunks_zero": True,
        },
        "provenance": {
            "partition_rule": (
                "floor(source_id / (2^35 * 4^(12-level))) = healpix_pixel"
            ),
            "pagination": "ORDER BY source_id ASC with TOP page_size and an advancing lower bound",
        },
    }
    _write_json_atomic(plan_path, plan)
    return _plan_summary(plan, plan_path)


def run_bulk_plan(
    plan_path: Path | str,
    *,
    max_chunks: int = 1,
    query_runner: Optional[Callable[[str], pd.DataFrame]] = None,
) -> dict[str, Any]:
    """Process up to ``max_chunks`` from a saved plan; 0 means all remaining.

    Stops on the first failed chunk to avoid repeatedly hammering an unhealthy
    Gaia service. Rerun the same plan to retry that chunk from its checkpoint.
    """
    plan_path = Path(plan_path)
    if not plan_path.exists():
        raise FileNotFoundError(f"Bulk plan not found: {plan_path}")
    if max_chunks < 0:
        raise ValueError("max_chunks must be >= 0 (0 explicitly means all remaining)")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    selection = plan["selection"]
    root = plan_path.parent
    progress = plan["progress"]
    if plan.get("status") == "completed":
        return _plan_summary(plan, plan_path)

    if query_runner is None:
        config = PipelineConfig.load()
        source = GaiaSource(config.gaia, config.network)
        query_runner = source.execute_adql

    plan["status"] = "running"
    _write_json_atomic(plan_path, plan)
    attempted = 0
    unlimited = max_chunks == 0

    while unlimited or attempted < max_chunks:
        pixel = _next_pixel(plan)
        if pixel is None:
            plan["status"] = "completed"
            break
        attempted += 1
        try:
            result = _download_pixel(
                root=root,
                plan=plan,
                pixel=pixel,
                query_runner=query_runner,
            )
        except Exception as exc:
            logger.exception("Gaia bulk pixel %s failed", pixel)
            failed = set(int(value) for value in progress.get("failed_pixels", []))
            failed.add(pixel)
            progress["failed_pixels"] = sorted(failed)
            progress["chunks_failed"] = len(failed)
            plan["status"] = "partial"
            plan["last_error"] = {"pixel": pixel, "message": str(exc), "at": _utc_now()}
            _write_json_atomic(plan_path, plan)
            break

        failed = set(int(value) for value in progress.get("failed_pixels", []))
        failed.discard(pixel)
        progress["failed_pixels"] = sorted(failed)
        progress["chunks_failed"] = len(failed)
        progress["chunks_completed"] = int(progress.get("chunks_completed", 0)) + 1
        progress["rows_downloaded"] = int(progress.get("rows_downloaded", 0)) + int(
            result["rows_downloaded"]
        )
        progress["files_written"] = int(progress.get("files_written", 0)) + int(
            result["files_written"]
        )
        if pixel == int(progress["next_pixel"]):
            progress["next_pixel"] = pixel + 1
        plan.pop("last_error", None)
        _write_json_atomic(plan_path, plan)

    if _next_pixel(plan) is None:
        plan["status"] = "completed"
    elif plan["status"] == "running":
        plan["status"] = "paused"
    _write_json_atomic(plan_path, plan)
    summary = _plan_summary(plan, plan_path)
    summary["chunks_attempted_this_run"] = attempted
    return summary


def bulk_status(plan_path: Path | str) -> dict[str, Any]:
    plan_path = Path(plan_path)
    if not plan_path.exists():
        return {"exists": False, "error": f"Bulk plan not found: {plan_path}"}
    return _plan_summary(json.loads(plan_path.read_text(encoding="utf-8")), plan_path)


def build_page_adql(
    *,
    table: str,
    columns: list[str],
    lower_source_id: int,
    upper_source_id: int,
    page_size: int,
) -> str:
    """Build one validated, deterministic keyset page query."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*", table):
        raise ValueError(f"Invalid Gaia table identifier: {table!r}")
    if page_size < 1 or page_size > 1_000_000:
        raise ValueError("page_size must be between 1 and 1,000,000")
    if lower_source_id < 0 or upper_source_id <= lower_source_id:
        raise ValueError("invalid source_id interval")
    select = "*" if columns == ["*"] else ", ".join(_validate_column(c) for c in columns)
    return (
        f"SELECT TOP {page_size} {select} FROM {table} "
        f"WHERE source_id >= {lower_source_id} "
        f"AND source_id < {upper_source_id} "
        "ORDER BY source_id ASC"
    )


def _download_pixel(
    *,
    root: Path,
    plan: dict[str, Any],
    pixel: int,
    query_runner: Callable[[str], pd.DataFrame],
) -> dict[str, int]:
    selection = plan["selection"]
    level = int(selection["healpix_level"])
    page_size = int(selection["page_size"])
    lower, upper = source_id_bounds(level, pixel)
    width = max(1, len(str(healpix_count(level) - 1)))
    chunk_dir = root / "chunks" / f"hp{level}_{pixel:0{width}d}"
    checkpoint_path = chunk_dir / "checkpoint.json"
    chunk_dir.mkdir(parents=True, exist_ok=True)

    if checkpoint_path.exists():
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint.get("status") == "completed":
            # A completed checkpoint is idempotent. The caller normally skips
            # it, but this guard also repairs a crash that happened after the
            # chunk checkpoint was committed and before the global plan was.
            return {
                "rows_downloaded": int(checkpoint.get("rows_downloaded", 0)),
                "files_written": int(checkpoint.get("files_written", 0)),
            }
    else:
        checkpoint = {
            "pixel": pixel,
            "healpix_level": level,
            "source_id_lower": lower,
            "source_id_upper": upper,
            "next_source_id": lower,
            "part_index": 0,
            "rows_downloaded": 0,
            "files_written": 0,
            "status": "pending",
            "created_at": _utc_now(),
        }

    checkpoint["status"] = "running"
    checkpoint.pop("error", None)
    _write_json_atomic(checkpoint_path, checkpoint)

    try:
        while True:
            cursor = int(checkpoint["next_source_id"])
            if cursor >= upper:
                checkpoint["status"] = "completed"
                checkpoint["completed_at"] = _utc_now()
                _write_json_atomic(checkpoint_path, checkpoint)
                break
            adql = build_page_adql(
                table=str(selection["table"]),
                columns=list(selection["columns"]),
                lower_source_id=cursor,
                upper_source_id=upper,
                page_size=page_size,
            )
            frame = query_runner(adql)
            if frame.empty:
                checkpoint["status"] = "completed"
                checkpoint["completed_at"] = _utc_now()
                _write_json_atomic(checkpoint_path, checkpoint)
                break
            if "source_id" not in frame.columns:
                raise ValueError("Gaia bulk page did not return required source_id column")

            frame = frame.copy()
            frame["source_id"] = pd.to_numeric(frame["source_id"], errors="raise").astype(
                "int64"
            )
            frame = frame.sort_values("source_id").drop_duplicates("source_id")
            if int(frame["source_id"].iloc[0]) < cursor:
                raise ValueError("Gaia page returned a source_id below the pagination cursor")
            last_id = int(frame["source_id"].iloc[-1])
            if last_id >= upper or last_id < cursor:
                raise ValueError("Gaia page returned a source_id outside its HEALPix interval")

            part_index = int(checkpoint["part_index"])
            part_path = chunk_dir / f"part-{part_index:05d}.parquet"
            _write_parquet_atomic(frame, part_path)
            rows = len(frame)
            checkpoint["rows_downloaded"] = int(checkpoint["rows_downloaded"]) + rows
            checkpoint["files_written"] = int(checkpoint["files_written"]) + 1
            checkpoint["part_index"] = part_index + 1
            checkpoint["next_source_id"] = last_id + 1
            checkpoint["last_part"] = part_path.name
            checkpoint["updated_at"] = _utc_now()
            _write_json_atomic(checkpoint_path, checkpoint)

            # Do not treat a short page as end-of-range. A TAP service may
            # return fewer rows than TOP because of a transient/service cap;
            # advancing the keyset cursor until an actually empty page costs
            # one extra query per pixel and prevents silent row loss.
    except Exception as exc:
        checkpoint["status"] = "failed"
        checkpoint["error"] = str(exc)
        checkpoint["updated_at"] = _utc_now()
        _write_json_atomic(checkpoint_path, checkpoint)
        raise

    # Global progress is updated only after a whole chunk succeeds, so return
    # checkpoint totals (including pages written during an earlier failed run).
    return {
        "rows_downloaded": int(checkpoint["rows_downloaded"]),
        "files_written": int(checkpoint["files_written"]),
    }


def _next_pixel(plan: dict[str, Any]) -> Optional[int]:
    progress = plan["progress"]
    failed = [int(value) for value in progress.get("failed_pixels", [])]
    if failed:
        return failed[0]
    next_pixel = int(progress["next_pixel"])
    stop = int(plan["selection"]["pixel_stop"])
    return next_pixel if next_pixel < stop else None


def _resolve_columns(
    mode: str,
    *,
    custom_columns: Optional[list[str]],
    default_columns: list[str],
) -> list[str]:
    mode = mode.strip().lower()
    if mode == "all":
        if custom_columns:
            raise ValueError("custom_columns cannot be combined with columns_mode='all'")
        return ["*"]
    if mode == "default":
        if custom_columns:
            raise ValueError("use columns_mode='custom' when passing custom_columns")
        columns = [
            *default_columns,
            *QC_EXTRA_COLUMNS,
            "has_xp_sampled",
            "has_xp_continuous",
        ]
    elif mode == "custom":
        if not custom_columns:
            raise ValueError("columns_mode='custom' requires custom_columns")
        columns = list(custom_columns)
    else:
        raise ValueError("columns_mode must be 'default', 'custom', or 'all'")
    if "source_id" not in columns:
        columns.insert(0, "source_id")
    return list(dict.fromkeys(_validate_column(column) for column in columns))


def _validate_column(column: str) -> str:
    column = str(column).strip()
    if not IDENTIFIER.fullmatch(column):
        raise ValueError(f"Invalid Gaia column name: {column!r}")
    return column


def _validate_level(level: int) -> None:
    if not isinstance(level, int) or isinstance(level, bool) or level < 0 or level > 12:
        raise ValueError("healpix_level must be an integer between 0 and 12")


def _plan_summary(plan: dict[str, Any], plan_path: Path) -> dict[str, Any]:
    progress = plan["progress"]
    total = int(progress["chunks_total"])
    completed = int(progress["chunks_completed"])
    return {
        "exists": True,
        "label": plan["label"],
        "status": plan["status"],
        "plan_path": str(plan_path.resolve()),
        "output_dir": str(plan_path.parent.resolve()),
        "selection": plan["selection"],
        "progress": progress,
        "chunks_remaining": max(0, total - completed),
        "percent_chunks_completed": round(100.0 * completed / total, 4) if total else 100.0,
        "scope_note": plan.get("scope_note"),
        "safety": plan.get("safety", {}),
        "last_error": plan.get("last_error"),
        "next_step": (
            "Call run_gaia_bulk_download(label, max_chunks=1) for one chunk; "
            "set max_chunks=0 only when you explicitly want every remaining chunk."
            if plan["status"] != "completed"
            else "All planned HEALPix chunks are complete."
        ),
    }


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = _utc_now()
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_parquet_atomic(frame: pd.DataFrame, path: Path) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_parquet(temporary, engine="pyarrow", index=False)
    temporary.replace(path)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
