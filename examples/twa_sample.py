#!/usr/bin/env python3
"""End-to-end TWA small-sample pipeline.

Queries Gaia XP spectra, LAMOST LRS, and SDSS for the TW Hya association
members based on a pre-existing Gaia DR3 cross-matched catalog.

Usage:
    python examples/twa_sample.py [--skip-download] [--fast]

The TWAxgdr3.csv catalog must be in the project root directory.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astro_pipeline.config import PipelineConfig
from astro_pipeline.crossmatch import (
    compute_coverage_center_radius,
    crossmatch_by_gaia_id,
    crossmatch_by_position,
)
from astro_pipeline.io.exporter import build_knowledge_table, export_jsonl
from astro_pipeline.io.writer import save_catalog_parquet
from astro_pipeline.models.query import ConeSearchQuery
from astro_pipeline.sources.external import ExternalCatalog
from astro_pipeline.sources.gaia import GaiaSource
from astro_pipeline.sources.lamost import LamostSource
from astro_pipeline.sources.sdss import SdssSource
from astro_pipeline.sources.simbad_refs import build_paper_edges

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("twa_sample")

DATA_DIR = Path("data/twa")
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description="TWA small-sample pipeline")
    parser.add_argument("--skip-download", action="store_true", help="Skip spectrum downloads")
    parser.add_argument("--fast", action="store_true", help="Only download 3 sources per survey")
    parser.add_argument(
        "--catalog", type=str, default="TWAxgdr3.csv",
        help="Path to TWA catalog CSV",
    )
    args = parser.parse_args()

    config = PipelineConfig()
    config.output.base_dir = config.output.base_dir.resolve()
    config.output.ensure_dirs()

    gaia = GaiaSource(config.gaia, config.network)
    lamost = LamostSource(config.lamost, config.network)
    sdss = SdssSource(config.sdss, config.network)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    catalog_path = PROJECT_ROOT / args.catalog
    if not catalog_path.exists():
        logger.error("Catalog not found: %s", catalog_path)
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("Step 1: Load TWA external catalog")
    logger.info("=" * 60)
    catalog = ExternalCatalog(catalog_path)
    catalog.filter_quality(max_ruwe=1.4, min_parallax_over_error=5.0)
    logger.info("After quality filter: %d sources", len(catalog.df))
    if len(catalog.df) == 0:
        logger.error("No sources survived quality filtering. Trying without filters...")
        catalog = ExternalCatalog(catalog_path)
        logger.info("Unfiltered: %d sources", len(catalog.df))

    source_ids = catalog.source_ids
    ra_center, dec_center, radius_deg = catalog.compute_coverage()
    cone_query = ConeSearchQuery(ra=ra_center, dec=dec_center, radius_deg=radius_deg)

    logger.info("=" * 60)
    logger.info("Step 2: Query Gaia for XP spectra availability")
    logger.info("=" * 60)
    xp_query = catalog.to_source_id_query()
    xp_result = gaia.query_xp_sources(xp_query, output_path=DATA_DIR / "catalogs" / "twa_gaia_xp.parquet")
    xp_df = xp_result.data

    if args.fast and not xp_df.empty:
        xp_df = xp_df.head(3)
    if not xp_df.empty and "source_id" in xp_df.columns:
        logger.info("  %d sources have XP spectra", len(xp_df))
        if not args.skip_download:
            logger.info("  Downloading Gaia XP spectra...")
            gaia.download_spectra(
                xp_df["source_id"].tolist(),
                DATA_DIR / "spectra" / "gaia_xp",
            )
        else:
            logger.info("  Skipping download (--skip-download)")
    else:
        logger.info("  No XP spectra available for TWA members")

    logger.info("=" * 60)
    logger.info("Step 3: Query LAMOST + cross-match")
    logger.info("=" * 60)
    lamost_twa = pd.DataFrame()
    try:
        lamost_result = lamost.query_catalog(cone_query, output_path=DATA_DIR / "catalogs" / "twa_lamost.parquet")
        lamost_df = lamost_result.data
        if not lamost_df.empty:
            try:
                lamost_twa = crossmatch_by_gaia_id(catalog.df, lamost_df, left_id_col="Gaia")
            except Exception as e:
                logger.warning("  LAMOST crossmatch failed: %s", e)
            logger.info("  TWA members with LAMOST spectra: %d", len(lamost_twa))
            if args.fast and len(lamost_twa) > 3:
                lamost_twa = lamost_twa.head(3)
            if len(lamost_twa) > 0 and not args.skip_download:
                logger.info("  Downloading LAMOST spectra...")
                lamost.download_spectra(lamost_twa, DATA_DIR / "spectra" / "lamost_lrs")
            elif args.skip_download:
                logger.info("  Skipping download (--skip-download)")
        else:
            logger.info("  No LAMOST data in TWA region")
    except Exception as e:
        logger.warning("  LAMOST query failed: %s", e)

    logger.info("=" * 60)
    logger.info("Step 4: Query SDSS + cross-match")
    logger.info("=" * 60)
    sdss_twa = pd.DataFrame()
    try:
        sdss_result = sdss.query_catalog(cone_query, output_path=DATA_DIR / "catalogs" / "twa_sdss.parquet")
        sdss_df = sdss_result.data
        if not sdss_df.empty:
            try:
                sdss_twa = crossmatch_by_position(
                    catalog.df, sdss_df,
                    left_ra_col="RAICRS", left_dec_col="DEICRS",
                    right_ra_col="ra", right_dec_col="dec",
                    max_sep_arcsec=2.0,
                )
            except Exception as e:
                logger.warning("  SDSS crossmatch failed: %s", e)
            logger.info("  TWA members with SDSS spectra: %d", len(sdss_twa))
            if args.fast and len(sdss_twa) > 3:
                sdss_twa = sdss_twa.head(3)
            if len(sdss_twa) > 0 and not args.skip_download:
                logger.info("  Downloading SDSS spectra...")
                sdss.download_spectra(sdss_twa, DATA_DIR / "spectra" / "sdss")
            elif args.skip_download:
                logger.info("  Skipping download (--skip-download)")
        else:
            logger.info("  No SDSS data in TWA region")
    except Exception as e:
        logger.warning("  SDSS query failed: %s", e)

    logger.info("=" * 60)
    logger.info("Step 5: Paper linking via SIMBAD (authoritative bibliography)")
    logger.info("=" * 60)
    # SIMBAD maintains a curated reference list per object: every bibcode is a
    # paper that genuinely studies that source. This replaces the old substring
    # matcher (which produced garbage: 94 papers x all sources). The result is
    # cached to data/twa/simbad_refs.parquet so re-runs are fast/offline.
    simbad_cache = DATA_DIR / "simbad_refs.parquet"
    paper_edges, refs = build_paper_edges(
        catalog.df,
        id_col="Gaia",
        sleep_seconds=0.25,
        cache_path=simbad_cache,
        enrich=True,
        resolve_doi=False,  # fast first pass; arXiv DOI resolution is optional/separate
    )
    paper_edges.save(DATA_DIR / "paper_edges.parquet")
    logger.info("  Paper edges: %d links across %d sources",
                len(paper_edges),
                refs["gaia_id"].nunique() if not refs.empty else 0)

    logger.info("=" * 60)
    logger.info("Step 6: Build unified knowledge table")
    logger.info("=" * 60)
    unified = build_knowledge_table(
        catalog.df,
        gaia_spectra_info=xp_df if not xp_df.empty else None,
        lamost_spectra_info=lamost_twa if not lamost_twa.empty else None,
        sdss_spectra_info=sdss_twa if not sdss_twa.empty else None,
        id_col="Gaia",
        gaia_id_col="source_id",
        lamost_id_col="gaiadr3",
        paper_edges=paper_edges,
    )

    unified_path = DATA_DIR / "twa_knowledge_table.parquet"
    save_catalog_parquet(unified, unified_path)
    logger.info("  Knowledge table: %s (%d rows, %d cols)", unified_path, len(unified), len(unified.columns))

    jsonl_path = DATA_DIR / "twa_knowledge.jsonl"
    export_jsonl(unified, jsonl_path)

    logger.info("=" * 60)
    logger.info("Summary")
    logger.info("=" * 60)
    has_xp = unified.get("has_gaia_xp", pd.Series([False] * len(unified)))
    has_lam = unified.get("has_lamost_spec", pd.Series([False] * len(unified)))
    has_sdss = unified.get("has_sdss_spec", pd.Series([False] * len(unified)))
    has_paper = unified.get("num_papers", pd.Series([0] * len(unified)))
    has_bibcodes = unified.get("bibcodes", pd.Series([[] for _ in range(len(unified))]))
    logger.info("  Total TWA members: %d", len(unified))
    logger.info("  With Gaia XP spectra: %d", has_xp.sum())
    logger.info("  With LAMOST spectra: %d", has_lam.sum())
    logger.info("  With SDSS spectra: %d", has_sdss.sum())
    logger.info("  With linked papers (arXiv id): %d", int((has_paper > 0).sum()))
    logger.info("  With SIMBAD bibcodes: %d", int(has_bibcodes.apply(len) > 0).sum())
    logger.info("  Total paper links: %d", int(has_paper.sum()))
    logger.info("  Output: %s", jsonl_path)


if __name__ == "__main__":
    main()
