#!/usr/bin/env python3
"""Regenerate the TWA source-paper corpus HTML.

Reads the corpus parquets (catalog + paper_edges) and writes
``data/twa/source_paper_viz.html``. Backs up the previous HTML once.

Usage:
    python scripts/build_viz.py
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astro_pipeline.viz import build_overview_html

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("build_viz")

DATA = Path("data/twa")
CATALOG = Path("TWAxgdr3.csv")
OUTPUT = DATA / "source_paper_viz.html"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, default=OUTPUT)
    ap.add_argument("--catalog", type=Path, default=CATALOG)
    ap.add_argument("--edges", type=Path, default=DATA / "paper_edges.parquet")
    args = ap.parse_args()

    if not args.catalog.exists():
        logger.error("Catalog not found: %s", args.catalog)
        sys.exit(1)
    if not args.edges.exists():
        logger.error("Edges not found: %s", args.edges)
        sys.exit(1)

    bak = args.output.with_suffix(args.output.suffix + ".bak")
    if not bak.exists() and args.output.exists():
        bak.write_bytes(args.output.read_bytes())
        logger.info("Backed up existing HTML to %s", bak.name)

    out = build_overview_html(
        output_path=args.output,
        catalog_path=args.catalog,
        edges_path=args.edges,
    )
    logger.info("Wrote %s (%.1f KB)", out, out.stat().st_size / 1024)


if __name__ == "__main__":
    main()
