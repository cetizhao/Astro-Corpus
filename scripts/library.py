#!/usr/bin/env python3
"""Read-only status/verification and explicit library import/export operations."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from astro_pipeline.library.import_export import (
    export_corpus,
    import_corpus,
    reconcile_library,
    verify_library,
)
from astro_pipeline.library.query import Library
from astro_pipeline.library.store import LibraryStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect and administer the local astronomical library"
    )
    parser.add_argument(
        "--library-root",
        type=Path,
        help="explicit library root (otherwise use configured resolution)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("status", help="show read-only global library counts")

    import_parser = commands.add_parser(
        "import", help="plan or explicitly apply a non-destructive legacy import"
    )
    import_parser.add_argument("source", type=Path)
    import_parser.add_argument("--release")
    import_parser.add_argument("--catalog", type=Path)
    import_parser.add_argument("--label")
    import_parser.add_argument(
        "--apply",
        action="store_true",
        help="write managed copies/index rows; default is dry-run",
    )

    verify_parser = commands.add_parser(
        "verify", help="verify SQLite, registered blobs, orphans, and staging"
    )
    verify_parser.add_argument(
        "--reconcile",
        action="store_true",
        help="also plan safe regeneration of missing compatibility views",
    )
    verify_parser.add_argument(
        "--apply-reconcile",
        action="store_true",
        help="explicitly apply safe missing-view reconciliation",
    )

    export_parser = commands.add_parser(
        "export", help="export a corpus to a new reference or standalone directory"
    )
    export_parser.add_argument("source", help="corpus path, UUID, or unambiguous label")
    export_parser.add_argument("destination", type=Path)
    export_parser.add_argument("--run-id")
    export_parser.add_argument(
        "--standalone",
        action="store_true",
        help="copy pinned product bytes and remove shared-library dependency",
    )
    return parser


def _configured_store(root: Path | None) -> LibraryStore | None:
    if root is not None:
        return LibraryStore(root)
    from astro_pipeline.library import get_library

    return get_library(create=False)


def _emit(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    store = _configured_store(args.library_root)
    try:
        if args.command == "status":
            result = Library(store).status()
        elif args.command == "import":
            if args.apply and store is None:
                raise RuntimeError(
                    "library is disabled; enable it or pass --library-root before import"
                )
            result = import_corpus(
                store or LibraryStore(Path("data") / "_library"),
                args.source,
                apply=args.apply,
                release=args.release,
                catalog=args.catalog,
                label=args.label,
            )
        elif args.command == "verify":
            if args.apply_reconcile:
                args.reconcile = True
            if store is None:
                result = {
                    "verify": {
                        "exists": False,
                        "scope": "global_library",
                        "ok": True,
                        "clean": True,
                    }
                }
                if args.reconcile:
                    result["reconcile"] = {
                        "exists": False,
                        "apply": bool(args.apply_reconcile),
                        "actions": [],
                    }
            else:
                verification = verify_library(store)
                if args.reconcile:
                    result = {
                        "verify": verification,
                        "reconcile": reconcile_library(
                            store, apply=bool(args.apply_reconcile)
                        ),
                    }
                else:
                    result = verification
        elif args.command == "export":
            if store is None:
                raise RuntimeError(
                    "library is disabled; enable it or pass --library-root before export"
                )
            result = export_corpus(
                store,
                args.source,
                args.destination,
                run_id=args.run_id,
                standalone=args.standalone,
            )
        else:  # pragma: no cover - argparse enforces a known command
            parser.error(f"unsupported command: {args.command}")
            return 2
    except (OSError, RuntimeError, ValueError) as exc:
        _emit({"error": str(exc), "command": args.command})
        return 2
    _emit(result)
    if args.command == "verify":
        verification = result.get("verify", result)
        return 0 if verification.get("ok", False) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
