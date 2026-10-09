#!/usr/bin/env python3
"""Select a reproducible paper pilot from pinned bibliography snapshots, then build it."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Sequence

import pandas as pd

from astro_pipeline.library.files import sha256_file
from astro_pipeline.sources.paper_fulltext import normalize_arxiv_id, split_arxiv_id


def _era(year: int) -> str:
    return "pre-2010" if year < 2010 else "2010-2019" if year < 2020 else "2020+"


def _topic(title: str) -> str:
    for name, pattern in (
        ("membership", r"census|membership|association|moving group|kinematic|population"),
        ("disks-accretion", r"dis[ck]|accreti|protoplanet|planet formation"),
        ("spectroscopy", r"spectro|spectral|abundance|lithium|emission.line"),
        ("methods-models", r"model|simulat|method|calibrat|evolution|theor"),
    ):
        if re.search(pattern, title, re.IGNORECASE):
            return name
    return "other-observations"


def select_papers(sources: Sequence[Path], count: int = 30, *, include_twa: bool = True) -> dict[str, Any]:
    """Round-robin era/topic strata; metadata labels are not content verification."""
    if not 20 <= count <= 50:
        raise ValueError("pilot count must be between 20 and 50")
    if not sources:
        raise ValueError("at least one pinned bibliography snapshot is required")
    candidates: dict[str, dict] = {}
    snapshots = []
    for source in sources:
        source = source.expanduser().resolve()
        if source.parent.parent.name != "runs":
            raise ValueError(f"select from a pinned runs/<run_id>/ snapshot, not a mutable root: {source}")
        frame = pd.read_parquet(source)
        required = {"arxiv_id", "bibcode", "title", "year"}
        if not required.issubset(frame.columns):
            raise ValueError(f"bibliography lacks required columns: {source}")
        provenance = {"path": str(source), "sha256": sha256_file(source),
                      "run_id": source.parent.name, "corpus_label": source.parents[2].name}
        snapshots.append(provenance)
        occurrences: dict[str, list[int]] = defaultdict(list)
        for ordinal, row in enumerate(frame.to_dict("records")):
            try:
                identifier = normalize_arxiv_id(str(row.get("arxiv_id", "")).strip())
                work, _ = split_arxiv_id(identifier)
            except ValueError:
                continue
            occurrences[work].append(ordinal)
            if work not in candidates:
                year = int(row["year"]) if pd.notna(row["year"]) else 0
                title = str(row["title"] or "").strip()
                candidates[work] = {
                    "arxiv_id": work, "bibcode": str(row["bibcode"]),
                    "doi": str(row["doi"]) if pd.notna(row.get("doi")) else None,
                    "title": title, "year": year,
                    "selection_stratum": {"era": _era(year), "topic_hint": _topic(title)},
                    "aliases": [], "provenance": [],
                }
            alias = {"bibcode": str(row["bibcode"]), "arxiv_id": identifier}
            if alias not in candidates[work]["aliases"]:
                candidates[work]["aliases"].append(alias)
        for work, ordinals in occurrences.items():
            candidates[work]["provenance"].append({**provenance, "row_ordinals": ordinals})
    selected = []
    if include_twa:
        selected.append(candidates.pop("2305.03557", {
            "arxiv_id": "2305.03557", "bibcode": "2023AJ....165..269L",
            "title": "A Census of the TW Hya Association with Gaia", "year": 2023,
            "selection_stratum": {"era": "2020+", "topic_hint": "membership"},
            "provenance": [{"selection_reason": "explicit TWA anchor for the project's original member sample"}],
        }))
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for paper in candidates.values():
        stratum = paper["selection_stratum"]
        buckets[(stratum["era"], stratum["topic_hint"])].append(paper)
    queues = {key: deque(sorted(values, key=lambda p: hashlib.sha256(p["arxiv_id"].encode()).hexdigest()))
              for key, values in sorted(buckets.items())}
    while len(selected) < count and any(queues.values()):
        for queue in queues.values():
            if queue and len(selected) < count:
                selected.append(queue.popleft())
    if len(selected) != count:
        raise ValueError(f"only {len(selected)} unique usable candidates, fewer than requested {count}")
    return {"schema_version": "1.0", "created_at": datetime.now(timezone.utc).isoformat(),
            "selection_method": "TWA anchor, then deterministic round-robin era/topic strata; SHA-256 ID ordering within strata",
            "topic_labels": "metadata heuristics only; confirm content characteristics after acquisition",
            "source_snapshots": snapshots, "n_candidates": len(candidates) + int(include_twa),
            "n_selected": len(selected), "papers": selected}


def _summary(manifest: dict) -> dict:
    return {key: manifest.get(key) for key in (
        "label", "run_id", "status", "n_inputs", "n_unique_papers", "n_downloaded", "n_fulltext", "counts", "manifest_path"
    )}


def audit_collection(manifest: dict, *, check_pdf: int = 0) -> dict:
    """Verify artifact bytes and report structure, separately from human fidelity checks."""
    if not 0 <= check_pdf <= 50:
        raise ValueError("check_pdf must be between 0 and 50")
    from astro_pipeline.library.paper_corpus import _parse_document

    papers = []
    checked_pdfs = set()
    for entry in manifest["papers"]:
        item = {key: entry.get(key) for key in ("ordinal", "work_id", "versioned_id", "status", "title", "license", "error")}
        artifacts = {a["kind"]: a for a in entry.get("artifacts", [])}
        item["artifacts"] = {kind: {"sha256": a["sha256"], "path": a["path"]} for kind, a in artifacts.items()}
        errors = []
        for kind, artifact in artifacts.items():
            try:
                if sha256_file(artifact["path"]) != artifact["sha256"]:
                    errors.append(f"{kind}: checksum mismatch")
            except OSError as exc:
                errors.append(f"{kind}: {exc}")
        item["integrity_errors"] = errors
        if "json" in artifacts and not errors:
            document = json.loads(Path(artifacts["json"]["path"]).read_text(encoding="utf-8"))
            item["parser"] = document["parser"]
            item["quality"] = document["quality"]
            item["replacement_characters"] = sum(b.get("text", "").count("�") for b in document["blocks"])
            item["has_reference_pdf"] = "pdf" in artifacts
            if "pdf" in artifacts and len(checked_pdfs) < check_pdf and entry["work_id"] not in checked_pdfs:
                checked_pdfs.add(entry["work_id"])
                try:
                    fallback = _parse_document("pdf", Path(artifacts["pdf"]["path"]).read_bytes(), document["paper"])
                    item["pdf_fallback_check"] = {"raw_sha256": artifacts["pdf"]["sha256"],
                                                   "parser": fallback["parser"], "quality": fallback["quality"]}
                except (ValueError, OSError) as exc:
                    item["pdf_fallback_check"] = {"error": str(exc)}
        papers.append(item)
    return {"schema_version": "1.0", "created_at": datetime.now(timezone.utc).isoformat(),
            "manifest_path": manifest.get("manifest_path"), "run_id": manifest["run_id"],
            "summary": _summary(manifest),
            "parser_formats": dict(Counter(p["parser"]["format"] for p in papers if "parser" in p)),
            "known_licenses": sum(bool(p.get("license")) for p in papers),
            "verified_artifacts": sum(len(p["artifacts"]) for p in papers if not p["integrity_errors"]),
            "integrity_failures": sum(bool(p["integrity_errors"]) for p in papers),
            "pdf_fallback_checks": len(checked_pdfs),
            "pdf_fallback_failures": sum("error" in p.get("pdf_fallback_check", {}) for p in papers),
            "scientific_fidelity": "not established by automatic checks; inspect original and derived content",
            "papers": papers}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    select = commands.add_parser("select", help="write a new bounded selection; performs no network requests")
    select.add_argument("--source", type=Path, action="append", required=True)
    select.add_argument("--count", type=int, default=30)
    select.add_argument("--selection", type=Path, required=True)
    build = commands.add_parser("build", help="download or resume the fixed local selection")
    build.add_argument("--selection", type=Path, required=True)
    build.add_argument("--label", required=True)
    build.add_argument("--output-dir", type=Path)
    build.add_argument("--refresh", action="store_true")
    build.add_argument("--no-pdf", action="store_true")
    report = commands.add_parser("report", help="verify local artifact bytes and report parser coverage offline")
    report.add_argument("--label", required=True)
    report.add_argument("--output-dir", type=Path)
    report.add_argument("--run-id")
    report.add_argument("--output", type=Path, required=True)
    report.add_argument("--check-pdf", type=int, default=0, help="also exercise PDF fallback on up to N cached PDFs")
    args = parser.parse_args(argv)
    try:
        if args.command == "report":
            from astro_pipeline.library.paper_corpus import paper_corpus_status

            if args.output.exists():
                raise FileExistsError(f"report already exists: {args.output}")
            manifest = paper_corpus_status(args.label, output_dir=args.output_dir, run_id=args.run_id)
            if not manifest["exists"]:
                raise ValueError("paper collection does not exist")
            result = audit_collection(manifest, check_pdf=args.check_pdf)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            print(json.dumps({key: value for key, value in result.items() if key != "papers"}, indent=2))
            return 1 if result["integrity_failures"] or result["pdf_fallback_failures"] else 0
        if args.command == "select":
            if args.selection.exists():
                raise FileExistsError(f"selection already exists; it will not be overwritten: {args.selection}")
            selection = select_papers(args.source, args.count)
            args.selection.parent.mkdir(parents=True, exist_ok=True)
            with args.selection.open("x", encoding="utf-8") as stream:
                json.dump(selection, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            print(json.dumps({"selection": str(args.selection), "n_selected": selection["n_selected"],
                              "eras": dict(Counter(p["selection_stratum"]["era"] for p in selection["papers"])),
                              "topics": dict(Counter(p["selection_stratum"]["topic_hint"] for p in selection["papers"]))}, indent=2))
            return 0
        from astro_pipeline.library.paper_corpus import build_paper_corpus

        if args.selection.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("selection exceeds 2 MiB")
        selection = json.loads(args.selection.read_text(encoding="utf-8"))
        papers = selection["papers"] if isinstance(selection, dict) else selection
        result = build_paper_corpus(args.label, papers, output_dir=args.output_dir,
                                    include_pdf=not args.no_pdf, refresh=args.refresh)
        print(json.dumps(_summary(result), ensure_ascii=False, indent=2))
        if result["counts"].get("failed") or result["counts"].get("unavailable"):
            return 1
        return 3 if result["status"] == "partial" else 0
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
