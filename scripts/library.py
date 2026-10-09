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

    paper_build = commands.add_parser("paper-build", help="explicitly build or resume a bounded paper collection")
    paper_build.add_argument("label")
    paper_build.add_argument("--selection", type=Path, help="JSON list of 1–50 arXiv IDs or paper records")
    paper_build.add_argument("--output-dir", type=Path)
    paper_build.add_argument("--resume-run-id")
    paper_build.add_argument("--refresh", action="store_true")
    paper_build.add_argument("--no-pdf", action="store_true", help="do not require an additional reference PDF")

    paper_status = commands.add_parser("paper-status", help="read pinned paper-collection progress")
    paper_status.add_argument("label")
    paper_status.add_argument("--output-dir", type=Path)
    paper_status.add_argument("--run-id")

    serve_parser = commands.add_parser(
        "serve", help="serve the read-only global library viewer on loopback"
    )
    serve_parser.add_argument(
        "--host",
        default="127.0.0.1",
        choices=("127.0.0.1",),
        help="loopback bind address (only 127.0.0.1 is supported)",
    )
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--poll-seconds", type=float, default=5.0)

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
        elif args.command == "paper-build":
            from astro_pipeline.library.paper_corpus import build_paper_corpus

            if store is None:
                raise RuntimeError("paper full text requires an enabled shared library or --library-root")
            selection = None
            if args.selection is not None:
                if args.selection.stat().st_size > 2 * 1024 * 1024:
                    raise ValueError("paper selection file exceeds 2 MiB")
                selection = json.loads(args.selection.read_text(encoding="utf-8"))
                if isinstance(selection, dict):
                    selection = selection.get("papers")
                if not isinstance(selection, list):
                    raise ValueError("selection must be a JSON list or an object with a papers list")
            result = build_paper_corpus(
                args.label, selection, include_pdf=not args.no_pdf, refresh=args.refresh,
                resume_run_id=args.resume_run_id, output_dir=args.output_dir, store=store,
            )
        elif args.command == "paper-status":
            from astro_pipeline.library.paper_corpus import paper_corpus_status

            result = paper_corpus_status(args.label, output_dir=args.output_dir,
                                         run_id=args.run_id, store=store)
        elif args.command == "serve":
            from astro_pipeline.viz.library_viewer import serve_library_viewer

            print(f"Serving read-only library viewer at http://{args.host}:{args.port}/")
            try:
                serve_library_viewer(
                    store,
                    host=args.host,
                    port=args.port,
                    poll_seconds=args.poll_seconds,
                )
            except KeyboardInterrupt:
                pass
            return 0
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
    if args.command == "paper-build":
        counts = result.get("counts", {})
        if counts.get("failed", 0) or counts.get("unavailable", 0):
            return 1
        if result.get("status") == "partial":
            return 3
    if args.command == "verify":
        verification = result.get("verify", result)
        return 0 if verification.get("ok", False) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
