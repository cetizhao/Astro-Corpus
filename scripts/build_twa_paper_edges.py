#!/usr/bin/env python3
"""Build the authoritative TWA source↔paper edge table via SIMBAD.

Run once (or whenever the catalog changes) to (re)generate:
  data/twa/paper_edges.parquet   -- many-to-many edges with metadata
  data/twa/simbad_refs.parquet   -- cached per-source bibcode lists (offline reuse)

Two phases, both resumable via the cache:
  Phase 1 (default): SIMBAD source->bibcodes + SIMBAD bibcode metadata
    (title/DOI/year/journal). Reliable and reasonably fast. This alone gives
    authoritative links with paper id (bibcode), title, and a DOI/adstract URL.
  Phase 2 (optional, --resolve-arxiv): resolve arXiv ids for journal papers via
    the arXiv DOI API. arXiv rate-limits to ~1 req/3s, so this is slow and may
    need multiple runs; it is checkpointed so no work is lost.

Usage:
    python scripts/build_twa_paper_edges.py                 # phase 1 only
    python scripts/build_twa_paper_edges.py --resolve-arxiv # phase 1 + 2
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astro_pipeline.sources.simbad_refs import build_paper_edges

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("build_edges")

DATA = Path("data/twa")
CATALOG = Path("TWAxgdr3.csv")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--resolve-arxiv", action="store_true",
                    help="Also resolve arXiv ids via the rate-limited arXiv DOI API (slow).")
    ap.add_argument("--sleep", type=float, default=0.3, help="Sleep between SIMBAD requests (s).")
    args = ap.parse_args()

    if not CATALOG.exists():
        logger.error("Catalog not found: %s", CATALOG)
        sys.exit(1)
    DATA.mkdir(parents=True, exist_ok=True)

    cat = pd.read_csv(CATALOG)
    logger.info("Loaded %d TWA members from %s", len(cat), CATALOG)

    edges, refs = build_paper_edges(
        cat,
        id_col="Gaia",
        sleep_seconds=args.sleep,
        progress_every=10,
        cache_path=DATA / "simbad_refs.parquet",
        enrich=True,
        resolve_doi=args.resolve_arxiv,
    )

    edges.save(DATA / "paper_edges.parquet")
    refs.to_parquet(DATA / "twa_refs_full.parquet", index=False)
    n_arxiv = int((refs["arxiv_id"].astype(str) != "").sum()) if not refs.empty else 0
    logger.info(
        "Done. %d edges, %d unique papers (%d with arXiv id), %d/%d sources resolved.",
        len(edges),
        refs["bibcode"].nunique() if not refs.empty else 0,
        n_arxiv,
        refs["gaia_id"].nunique() if not refs.empty else 0,
        len(cat),
    )
    if not args.resolve_arxiv:
        logger.info("Tip: re-run with --resolve-arxiv to fill in arXiv ids for journal papers.")


if __name__ == "__main__":
    main()
