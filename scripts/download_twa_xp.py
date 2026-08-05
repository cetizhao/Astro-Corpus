#!/usr/bin/env python3
"""Download Gaia DR3 XP_SAMPLED spectra for all adopted TWA members."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astro_pipeline.config import PipelineConfig
from astro_pipeline.models.query import SourceIdQuery
from astro_pipeline.sources.gaia import GaiaSource


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("download_twa_xp")


def main() -> None:
    catalog_path = Path("TWAxgdr3.csv")
    if not catalog_path.exists():
        raise FileNotFoundError(f"TWA catalog not found: {catalog_path}")

    catalog = pd.read_csv(catalog_path, dtype={"Gaia": str})
    source_ids = catalog["Gaia"].astype(str).tolist()
    config = PipelineConfig.load()
    gaia = GaiaSource(config.gaia, config.network)

    xp_catalog_path = Path("data/twa/catalogs/twa_gaia_xp.parquet")
    result = gaia.query_xp_sources(
        SourceIdQuery(source_ids=source_ids), output_path=xp_catalog_path,
    )
    xp_ids = result.data["source_id"].astype(str).tolist()
    logger.info("XP_SAMPLED available for %d/%d TWA members", len(xp_ids), len(source_ids))

    output_dir = Path("data/twa/gaia_xp_sampled")
    downloads = gaia.download_spectra(xp_ids, output_dir, resume=True)
    logger.info(
        "Download complete: %d success, %d failed",
        sum(item.success for item in downloads),
        sum(not item.success for item in downloads),
    )


if __name__ == "__main__":
    main()
