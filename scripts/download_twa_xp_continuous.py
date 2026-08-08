#!/usr/bin/env python3
"""Download Gaia DR3 XP_CONTINUOUS spectra for adopted TWA members."""
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
logger = logging.getLogger("download_twa_xp_continuous")


def main() -> None:
    catalog_path = Path("TWAxgdr3.csv")
    if not catalog_path.exists():
        raise FileNotFoundError(f"TWA catalog not found: {catalog_path}")

    catalog = pd.read_csv(catalog_path, dtype={"Gaia": str})
    source_ids = catalog["Gaia"].astype(str).tolist()
    config = PipelineConfig.load()
    gaia = GaiaSource(config.gaia, config.network)

    result = gaia._query_by_ids(
        SourceIdQuery(source_ids=source_ids),
        extra_columns=["has_xp_sampled", "has_xp_continuous"],
    )
    xp_df = result.data
    availability_path = Path("data/twa/catalogs/twa_gaia_xp_availability.parquet")
    availability_path.parent.mkdir(parents=True, exist_ok=True)
    xp_df.to_parquet(availability_path, index=False)

    has_cont = xp_df["has_xp_continuous"].astype(str).str.lower().isin({"true", "1"})
    cont_ids = xp_df.loc[has_cont, "source_id"].astype(str).tolist()
    logger.info("XP_CONTINUOUS available for %d/%d TWA members", len(cont_ids), len(source_ids))

    output_dir = Path("data/twa/gaia_xp_continuous")
    downloads = gaia.download_spectra(
        cont_ids,
        output_dir,
        resume=True,
        retrieval_type="XP_CONTINUOUS",
        batch_size=5,
    )
    logger.info(
        "Download complete: %d success, %d failed",
        sum(item.success for item in downloads),
        sum(not item.success for item in downloads),
    )


if __name__ == "__main__":
    main()
