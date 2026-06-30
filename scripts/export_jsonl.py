#!/usr/bin/env python3
"""Export the TWA corpus to a simplified JSONL schema (one record per source).

This is the **demonstrable data storage** artifact: a single self-describing
JSONL file showing what the corpus currently knows about each Gaia source.
Schema is deliberately minimal and derived from what the data actually
contains -- it is meant to evolve once AISI gives format guidance.

Schema (one JSON object per line, per Gaia source):

    {
      "gaia_id":            str,   # Gaia DR3 source_id
      "ra_deg":             float, # ICRS RA from catalog
      "dec_deg":            float, # ICRS Dec
      "parallax_mas":       float, # Gaia DR3 parallax (mas)
      "parallax_error_mas": float,
      "ruwe":               float,
      "g_mag":              float, # Gaia G-band magnitude
      "pm_ra_masyr":        float, # proper motion (mas/yr)
      "pm_dec_masyr":       float,
      "multimodal": {
        "gaia_xp": bool,           # whether Gaia XP spectrum is available
        "lamost":  bool,           # LAMOST spectrum available
        "sdss":    bool            # SDSS spectrum available
      },
      "papers": [
        {"arxiv_id": str, "bibcode": str, "title": str, "year": int|null,
         "url": str},
        ...
      ],
      "n_papers":  int,
      "provenance": {"pipeline_version": str}
    }

Usage:
    python scripts/export_jsonl.py
    python scripts/export_jsonl.py --output path/to/out.jsonl --offline
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astro_pipeline.corpus import Corpus

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("export_jsonl")

PIPELINE_VERSION = "0.2.1"
DATA = Path("data/twa")
CATALOG = Path("TWAxgdr3.csv")
OUTPUT = DATA / "twa_corpus.jsonl"


def to_record(lookup: dict, catalog_extras: dict) -> dict:
    """Flatten a corpus.lookup_source result into the simplified export schema."""
    ast = lookup.get("astrometry", {})
    mm = lookup.get("multimodal", {})
    papers = lookup.get("papers", [])
    extras = catalog_extras or {}

    def _f(d, key, default=None):
        v = d.get(key, default)
        if v is None:
            return None
        try:
            if pd.isna(v):
                return None
        except (TypeError, ValueError):
            pass
        return v

    return {
        "gaia_id": lookup.get("gaia_id"),
        "ra_deg": _f(ast, "RAICRS"),
        "dec_deg": _f(ast, "DEICRS"),
        "parallax_mas": _f(ast, "Plx_gdr3"),
        "parallax_error_mas": _f(ast, "e_Plx_gdr3"),
        "ruwe": _f(ast, "RUWE_gdr3"),
        "g_mag": _f(ast, "Gmag_gdr3"),
        "pm_ra_masyr": _f(extras, "pmRA_gdr3") if "pmRA_gdr3" in extras else _f(extras, "pmRA"),
        "pm_dec_masyr": _f(extras, "pmDE_gdr3") if "pmDE_gdr3" in extras else _f(extras, "pmDE"),
        "multimodal": {
            "gaia_xp": bool(mm.get("gaia_xp", {}).get("available", False)),
            "lamost": bool(mm.get("lamost", {}).get("available", False)),
            "sdss": bool(mm.get("sdss", {}).get("available", False)),
        },
        "papers": [
            {
                "arxiv_id": p.get("arxiv_id") or "",
                "bibcode": p.get("bibcode") or "",
                "title": p.get("title") or "",
                "year": p.get("year"),
                "url": p.get("url") or "",
            }
            for p in papers
        ],
        "n_papers": len(papers),
        "provenance": {"pipeline_version": PIPELINE_VERSION},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, default=OUTPUT)
    ap.add_argument("--catalog", type=Path, default=CATALOG)
    ap.add_argument("--data-dir", type=Path, default=DATA)
    ap.add_argument("--offline", action="store_true", default=True,
                    help="Use cached parquets only (default; pass --no-offline to allow live queries).")
    ap.add_argument("--no-offline", dest="offline", action="store_false")
    ap.add_argument("--limit", type=int, default=0,
                    help="Export only first N sources (0 = all, for debugging).")
    args = ap.parse_args()

    if not args.catalog.exists():
        logger.error("Catalog not found: %s", args.catalog)
        sys.exit(1)

    catalog = pd.read_csv(args.catalog)
    catalog_extras = {str(r["Gaia"]): r.to_dict() for _, r in catalog.iterrows()}
    logger.info("Loaded %d catalog sources from %s", len(catalog), args.catalog)

    corpus = Corpus(data_dir=args.data_dir, catalog_path=args.catalog, offline=args.offline)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    n_written = 0
    n_with_arxiv = 0
    n_with_xp = 0
    with args.output.open("w", encoding="utf-8") as f:
        for _, row in catalog.iterrows():
            gid = str(row["Gaia"])
            if args.limit and n_written >= args.limit:
                break
            lookup = corpus.lookup_source(gid)
            if not lookup.get("resolved"):
                logger.warning("  skipping unresolved source %s", gid)
                continue
            rec = to_record(lookup, catalog_extras.get(gid, {}))
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n_written += 1
            if rec["papers"] and any(p["arxiv_id"] for p in rec["papers"]):
                n_with_arxiv += 1
            if rec["multimodal"]["gaia_xp"]:
                n_with_xp += 1

    size_kb = args.output.stat().st_size / 1024
    logger.info("Wrote %d records to %s (%.1f KB)", n_written, args.output, size_kb)
    logger.info("  sources with >=1 arxiv paper: %d/%d", n_with_arxiv, n_written)
    logger.info("  sources with Gaia XP spectrum: %d/%d", n_with_xp, n_written)


if __name__ == "__main__":
    main()
