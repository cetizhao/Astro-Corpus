#!/usr/bin/env python3
"""Build a complete corpus snapshot from any input mode (CLI).

Thin wrapper around astro_pipeline.corpus_builder.build_corpus_snapshot.
The same orchestrator is used by the MCP ``download_corpus`` tool, so CLI
and agent-driven builds stay in sync.

Examples
--------
  # Single source by name
  python scripts/build_corpus.py --name "TW Hya" --out-dir data/demo/tw_hya

  # Cone around a position, capped at 50 sources
  python scripts/build_corpus.py --cone 165.466 -34.705 0.5 \\
      --out-dir data/demo/tw_hya_region --limit 50

  # User-provided catalog (must have Gaia ids OR RA/Dec columns)
  python scripts/build_corpus.py --catalog my_sources.csv --out-dir data/demo/mine

  # Named sky center + radius (field sources, not physical members)
  python scripts/build_corpus.py --region "Pleiades" 5.0 --out-dir data/demo/pleiades

  # Skip SIMBAD (just resolve members -- much faster)
  python scripts/build_corpus.py --cone 84 -5 3 --out-dir data/demo/test --skip-simbad
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astro_pipeline.corpus_builder import build_corpus_snapshot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("build_corpus")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--cone", nargs=3, metavar=("RA", "DEC", "RADIUS_DEG"), type=float,
                   help="Gaia DR3 cone search: RA Dec radius (degrees)")
    g.add_argument("--catalog", type=Path,
                   help="User CSV with a Gaia source_id column OR RA/Dec columns")
    g.add_argument("--name", help="SIMBAD-resolvable source name (e.g. 'TW Hya')")
    g.add_argument("--region", nargs=2, metavar=("CENTER", "RADIUS_DEG"),
                   help="Geometric field region: SIMBAD center + cone radius; "
                        "does not select physical members")

    ap.add_argument("--out-dir", type=Path, required=True,
                    help="Output directory (created if missing)")
    ap.add_argument("--label", help="Human-readable corpus name (default: derived)")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap on members (advisory for cone; hard for catalog id fetch)")
    ap.add_argument("--skip-simbad", action="store_true",
                    help="Stop after members.* are written (faster; for testing)")
    ap.add_argument("--sleep", type=float, default=0.3,
                    help="Pause between SIMBAD requests in seconds (default 0.3; "
                         "lower to 0.1 for ~3x speedup)")
    ap.add_argument("--no-enrich", action="store_true",
                    help="Skip per-bibcode metadata fetch (title/DOI/year). "
                         "Source<->paper links still built; just no paper titles. "
                         "5-10x faster for dense fields.")
    ap.add_argument("--xp-sampled", action="store_true",
                    help="Download available Gaia XP_SAMPLED FITS (resumable)")
    ap.add_argument("--xp-continuous", action="store_true",
                    help="Download available Gaia XP_CONTINUOUS FITS (resumable)")
    args = ap.parse_args()

    # Derive a label if not given
    if args.label:
        label = args.label
    elif args.cone:
        ra, dec, radius = args.cone
        label = f"cone_{ra:.2f}_{dec:.2f}_r{radius}"
    elif args.catalog:
        label = args.catalog.stem
    elif args.name:
        label = args.name.replace(" ", "_")
    else:  # region
        center, _ = args.region
        label = center.replace(" ", "_")

    result = build_corpus_snapshot(
        label=label,
        out_dir=args.out_dir,
        cone=tuple(args.cone) if args.cone else None,
        name=args.name,
        region=(args.region[0], float(args.region[1])) if args.region else None,
        catalog=args.catalog,
        limit=args.limit,
        skip_simbad=args.skip_simbad,
        sleep_seconds=args.sleep,
        enrich=not args.no_enrich,
        xp_sampled=args.xp_sampled,
        xp_continuous=args.xp_continuous,
    )

    if result.error:
        logger.error("Build failed: %s", result.error)
        sys.exit(1)

    logger.info(
        "Done. %d members, %d edges, %d papers. Viz: %s",
        result.n_members, result.n_paper_edges, result.n_unique_papers,
        result.viz_html or "(skipped)",
    )
    if not args.skip_simbad and result.members_csv:
        jsonl_path = Path(result.out_dir) / "corpus.jsonl"
        logger.info("For JSONL export run:")
        logger.info(
            "  python scripts/export_jsonl.py --catalog %s --data-dir %s --output %s",
            result.members_csv, result.out_dir, jsonl_path,
        )


if __name__ == "__main__":
    main()
