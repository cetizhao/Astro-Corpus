#!/usr/bin/env python3
"""Plan, run, and inspect resumable Gaia DR3 main-table HEALPix downloads."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from astro_pipeline.bulk_gaia import bulk_status, create_bulk_plan, run_bulk_plan
from astro_pipeline.manifest import validate_label


def _root(label: str) -> Path:
    return Path("data") / "gaia_bulk" / validate_label(label)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan", help="create a plan; downloads no Gaia rows")
    plan.add_argument("label")
    plan.add_argument("--level", type=int, default=5)
    plan.add_argument("--pixel-start", type=int, default=0)
    plan.add_argument("--pixel-stop", type=int, default=0, help="0 = end of sky")
    plan.add_argument("--columns", choices=("default", "all"), default="default")
    plan.add_argument("--page-size", type=int, default=100_000)
    plan.add_argument("--release", default="dr3", help="Gaia release profile (default: dr3)")
    plan.add_argument("--table", default="gaia_source", help="Unqualified TAP table name")
    plan.add_argument("--schema", default=None, help="Confirmed TAP schema override")
    plan.add_argument(
        "--partition-strategy",
        default=None,
        help="Defaults to the release profile; DR4 remains disabled until confirmed",
    )

    run = commands.add_parser("run", help="download bounded chunks from a saved plan")
    run.add_argument("label")
    run.add_argument(
        "--max-chunks",
        type=int,
        default=1,
        help="default 1; 0 explicitly means all remaining chunks",
    )

    status = commands.add_parser("status", help="read local progress only")
    status.add_argument("label")
    args = parser.parse_args()

    if args.command == "plan":
        result = create_bulk_plan(
            label=args.label,
            root=_root(args.label),
            healpix_level=args.level,
            pixel_start=args.pixel_start,
            pixel_stop=args.pixel_stop or None,
            columns_mode=args.columns,
            page_size=args.page_size,
            release=args.release,
            table=args.table,
            schema=args.schema,
            partition_strategy=args.partition_strategy,
        )
    elif args.command == "run":
        result = run_bulk_plan(
            _root(args.label) / "bulk_plan.json", max_chunks=args.max_chunks
        )
    else:
        result = bulk_status(_root(args.label) / "bulk_plan.json")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
