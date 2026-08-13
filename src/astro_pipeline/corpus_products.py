"""Optional Gaia data products attached to a resolved corpus member table."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

from astro_pipeline.config import PipelineConfig
from astro_pipeline.sources.gaia import GaiaSource

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, dict[str, Any]], None]


def download_gaia_xp_products(
    members: pd.DataFrame,
    out_dir: Path | str,
    *,
    sampled: bool = False,
    continuous: bool = False,
    sampled_batch_size: int = 500,
    continuous_batch_size: int = 5,
    progress_callback: Optional[ProgressCallback] = None,
) -> dict[str, Any]:
    """Query XP availability and download requested products with resume.

    The function is deliberately non-fatal for DataLink outages: availability
    and error details are returned so the astrometry/paper corpus remains
    usable and a later call can resume from each product's checkpoint.
    """
    out_dir = Path(out_dir)
    requested = {"sampled": sampled, "continuous": continuous}
    summary: dict[str, Any] = {
        "requested": requested,
        "availability_path": "",
        "sampled": _empty_product_summary(sampled),
        "continuous": _empty_product_summary(continuous),
        "errors": [],
    }
    if not (sampled or continuous):
        return summary
    if "Gaia" not in members.columns:
        summary["errors"].append("members table has no canonical Gaia column")
        return summary

    source_ids = list(dict.fromkeys(members["Gaia"].dropna().astype(str).tolist()))
    availability_path = out_dir / "catalogs" / "gaia_xp_availability.parquet"
    if progress_callback:
        progress_callback(
            "gaia_xp_availability",
            {"status": "running", "total": len(source_ids), "completed": 0},
        )

    config = PipelineConfig.load()
    gaia = GaiaSource(config.gaia, config.network)
    try:
        result = gaia.query_xp_availability(source_ids, availability_path)
        availability = result.data
        summary["availability_path"] = str(availability_path.resolve())
        if progress_callback:
            progress_callback(
                "gaia_xp_availability",
                {
                    "status": "completed",
                    "total": len(source_ids),
                    "completed": len(availability),
                    "artifact": str(availability_path.resolve()),
                },
            )
    except Exception as exc:
        message = f"Gaia XP availability query failed: {exc}"
        logger.exception(message)
        summary["errors"].append(message)
        if progress_callback:
            progress_callback(
                "gaia_xp_availability",
                {"status": "failed", "total": len(source_ids), "error": str(exc)},
            )
        for product in ("sampled", "continuous"):
            if requested[product]:
                summary[product]["status"] = "blocked"
                summary[product]["error"] = "availability query failed"
        return summary

    product_specs = [
        ("sampled", "has_xp_sampled", "XP_SAMPLED", sampled_batch_size),
        ("continuous", "has_xp_continuous", "XP_CONTINUOUS", continuous_batch_size),
    ]
    for product, flag_column, retrieval_type, batch_size in product_specs:
        if not requested[product]:
            continue
        mask = availability[flag_column].fillna(False).astype(bool)
        available_ids = availability.loc[mask, "source_id"].astype(str).tolist()
        product_dir = out_dir / f"gaia_xp_{product}"
        info = summary[product]
        info.update(
            {
                "status": "running",
                "available": len(available_ids),
                "output_dir": str(product_dir.resolve()),
            }
        )
        if progress_callback:
            progress_callback(
                f"gaia_xp_{product}",
                {
                    "status": "running",
                    "total": len(available_ids),
                    "completed": _count_cached(product_dir, available_ids),
                    "failed": 0,
                },
            )

        def on_batch(progress: dict, *, current_product: str = product) -> None:
            cached = _count_cached(product_dir, available_ids)
            if progress_callback:
                progress_callback(
                    f"gaia_xp_{current_product}",
                    {
                        "status": "running" if not progress.get("done") else "finalizing",
                        "total": len(available_ids),
                        "completed": cached,
                        "failed": int(progress.get("failed_this_run", 0)),
                        "processed_this_run": int(progress.get("processed", 0)),
                    },
                )

        try:
            downloads = gaia.download_spectra(
                available_ids,
                product_dir,
                resume=True,
                retrieval_type=retrieval_type,
                batch_size=batch_size,
                progress_callback=on_batch,
            )
            cached = _count_cached(product_dir, available_ids)
            failed = sum(not item.success for item in downloads)
            status = "completed" if cached == len(available_ids) else "partial"
            info.update(
                {
                    "status": status,
                    "cached": cached,
                    "downloaded_this_run": sum(item.success for item in downloads),
                    "failed_this_run": failed,
                    "missing": len(available_ids) - cached,
                }
            )
        except Exception as exc:
            message = f"Gaia {retrieval_type} download failed: {exc}"
            logger.exception(message)
            summary["errors"].append(message)
            cached = _count_cached(product_dir, available_ids)
            info.update(
                {
                    "status": "partial" if cached else "failed",
                    "cached": cached,
                    "missing": len(available_ids) - cached,
                    "error": str(exc),
                }
            )
        if progress_callback:
            progress_callback(
                f"gaia_xp_{product}",
                {
                    "status": info["status"],
                    "total": len(available_ids),
                    "completed": info["cached"],
                    "failed": info.get("failed_this_run", info["missing"]),
                    "artifact": str(product_dir.resolve()),
                    "error": info.get("error"),
                },
            )
    return summary


def _empty_product_summary(requested: bool) -> dict[str, Any]:
    return {
        "requested": requested,
        "status": "pending" if requested else "not_requested",
        "available": 0,
        "cached": 0,
        "downloaded_this_run": 0,
        "failed_this_run": 0,
        "missing": 0,
        "output_dir": "",
        "error": None,
    }


def _count_cached(directory: Path, source_ids: list[str]) -> int:
    if not directory.exists():
        return 0
    return sum((directory / f"{source_id}.fits").exists() for source_id in source_ids)
