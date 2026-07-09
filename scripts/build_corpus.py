#!/usr/bin/env python3
"""Build a complete corpus snapshot from any input mode.

Resolves members via :mod:`astro_pipeline.intake` (cone / catalog / name /
region), runs SIMBAD paper-edge construction, and emits the artifacts the
existing TWA pipeline produces (members catalog, paper_edges parquet,
viz HTML).

Output layout under ``--out-dir``::

    members.csv             # input to corpus pipeline (Gaia/RAICRS/DEICRS/...)
    members.parquet         # same, parquet
    simbad_refs.parquet     # SIMBAD bibliography cache (resumable)
    paper_edges.parquet     # source<->paper edge table
    source_paper_viz.html   # interactive overview

For JSONL export, run after this script::

    python scripts/export_jsonl.py \\
        --catalog <out_dir>/members.csv \\
        --data-dir <out_dir> \\
        --output <out_dir>/corpus.jsonl

Examples
--------
  # Single source by name
  python scripts/build_corpus.py --name "TW Hya" --out-dir data/demo/tw_hya

  # Cone around a position, capped at 50 sources
  python scripts/build_corpus.py --cone 165.466 -34.705 0.5 \\
      --out-dir data/demo/tw_hya_region --limit 50

  # User-provided catalog (must have Gaia ids OR RA/Dec columns)
  python scripts/build_corpus.py --catalog my_sources.csv --out-dir data/demo/mine

  # Region by name + radius
  python scripts/build_corpus.py --region "Pleiades" 5.0 --out-dir data/demo/pleiades
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from astro_pipeline.intake import resolve_members
from astro_pipeline.sources.simbad_refs import build_paper_edges
from astro_pipeline.io.writer import save_catalog_parquet

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("build_corpus")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--cone", nargs=3, metavar=("RA", "DEC", "RADIUS_DEG"), type=float,
                   help="Gaia DR3 cone search: RA Dec radius (degrees)")
    g.add_argument("--catalog", type=Path,
                   help="User CSV with a Gaia source_id column OR RA/Dec columns")
    g.add_argument("--name", help="SIMBAD-resolvable source name (e.g. 'TW Hya')")
    g.add_argument("--region", nargs=2, metavar=("CENTER", "RADIUS_DEG"),
                   help="Region: SIMBAD-resolvable center name + cone radius (degrees)")

    ap.add_argument("--out-dir", type=Path, required=True,
                    help="Output directory (created if missing)")
    ap.add_argument("--label", help="Human-readable corpus name (default: derived)")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap on members (advisory; cone queries may exceed)")
    ap.add_argument("--skip-simbad", action="store_true",
                    help="Stop after members.* are written (faster; for testing)")
    args = ap.parse_args()

    # --- 1. Resolve members ---
    logger.info("Resolving members...")
    if args.cone:
        ra, dec, radius = args.cone
        members = resolve_members(cone=(ra, dec, radius), limit=args.limit)
        label = args.label or f"cone_{ra:.2f}_{dec:.2f}_r{radius}"
    elif args.catalog:
        members = resolve_members(catalog=args.catalog, limit=args.limit)
        label = args.label or args.catalog.stem
    elif args.name:
        members = resolve_members(name=args.name)
        label = args.label or args.name.replace(" ", "_")
    else:  # region
        center, radius_str = args.region
        members = resolve_members(region=(center, float(radius_str)), limit=args.limit)
        label = args.label or center.replace(" ", "_")

    logger.info("Resolved %d members (label=%r)", len(members), label)
    if members.empty:
        logger.error("No members resolved; aborting")
        sys.exit(1)

    # --- 2. Save members ---
    args.out_dir.mkdir(parents=True, exist_ok=True)
    members_csv = args.out_dir / "members.csv"
    members.to_csv(members_csv, index=False)
    save_catalog_parquet(members, args.out_dir / "members.parquet")
    logger.info("Wrote %s + members.parquet", members_csv.name)

    if args.skip_simbad:
        logger.info("--skip-simbad; stopping after members.*")
        return

    # --- 3. SIMBAD paper edges ---
    logger.info("Querying SIMBAD for paper bibliography per source (this can take a while)...")
    edges, refs = build_paper_edges(
        members,
        id_col="Gaia",
        cache_path=args.out_dir / "simbad_refs.parquet",
    )
    edges_path = args.out_dir / "paper_edges.parquet"
    edges.save(edges_path)
    logger.info("Built %d paper edges -> %s", len(edges), edges_path.name)

    # --- 4. Viz HTML ---
    from astro_pipeline.viz import build_overview_html
    viz_path = args.out_dir / "source_paper_viz.html"
    build_overview_html(
        output_path=viz_path,
        catalog_path=members_csv,
        edges_path=edges_path,
    )
    logger.info("Wrote %s", viz_path.name)

    # --- 5. Next-step pointer for JSONL ---
    jsonl_hint = (
        f"  python scripts/export_jsonl.py "
        f"--catalog {members_csv} --data-dir {args.out_dir} "
        f"--output {args.out_dir / 'corpus.jsonl'}"
    )
    logger.info("Done. For JSONL export run:\n%s", jsonl_hint)


if __name__ == "__main__":
    main()
