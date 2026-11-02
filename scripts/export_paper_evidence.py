"""Export existing object-paper evidence as one pinned, offline HTML file."""
from __future__ import annotations

import argparse
from pathlib import Path

from astro_pipeline.library import get_library
from astro_pipeline.library.evidence import _has_table
from astro_pipeline.library.identity import source_identity
from astro_pipeline.viz.evidence import export_evidence_html


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--source", action="append", help="Release-qualified ID, e.g. dr3:5401795662560500352; repeatable")
    selection.add_argument("--all-annotated", action="store_true", help="All currently annotated identities in the shared library")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--title", default="Astro-Corpus · 文献与证据")
    args = parser.parse_args()
    store = get_library(create=False)
    if args.all_annotated:
        targets = [(r["release"], r["source_id"]) for r in store.read(
            "SELECT DISTINCT release,source_id FROM paper_annotations ORDER BY release,source_id"
        )] if _has_table(store, "paper_annotations") else []
    else:
        try:
            targets = [source_identity(*value.split(":")) for value in args.source]
        except (ValueError, TypeError) as exc:
            parser.error(str(exc))
    print(export_evidence_html(store, targets, args.output, title=args.title).resolve())


if __name__ == "__main__":
    main()
