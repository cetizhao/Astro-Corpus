#!/usr/bin/env python3
"""Acquire, prepare, export and score the CTTS development pilot. No model calls."""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from astro_pipeline.research_tasks import (
    compile_bundle, export_prompts, read_json, reference_predictions, score, write_json,
)

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "benchmarks/ctts_herczeg/tasks.json"
OUT = ROOT / "data/research_tasks/ctts_herczeg_v1"
SOURCE = ROOT / "data/ctts_herczeg_pilot"
PAPERS = ROOT / "data/paper_corpora/ctts_herczeg_pilot"


def acquire(spec: dict, output: Path) -> None:
    from astro_pipeline.config import PipelineConfig
    from astro_pipeline.corpus_builder import build_corpus_snapshot, result_to_dict
    from astro_pipeline.gaia_releases import normalize_gaia_release
    from astro_pipeline.library.paper_corpus import build_paper_corpus
    from astro_pipeline.sources.paper_fulltext import ArxivClient

    config = PipelineConfig.load()
    if normalize_gaia_release(config.gaia.release) != "dr3" or not config.library.enabled:
        raise ValueError("This pilot requires Gaia DR3 and library.enabled: true")
    output.mkdir(parents=True, exist_ok=True)
    inputs = output / "targets.csv"
    with inputs.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["name", "Gaia", "gaia_release",
                                                   "selection_type", "selection_paper", "identity_source"])
        writer.writeheader()
        for target in spec["selection"]["targets"]:
            writer.writerow({"name": target["name"], "Gaia": target["source_id"],
                             "gaia_release": target["release"],
                             "selection_type": "literature_target_list",
                             "selection_paper": target["selection_paper"],
                             "identity_source": spec["selection"]["identity_method"]})
    result = result_to_dict(build_corpus_snapshot(label="ctts_herczeg_pilot", out_dir=SOURCE,
                                                  catalog=inputs, skip_simbad=True, enrich=False))
    write_json(output / "source-acquisition.json", result)
    if result.get("error"):
        raise ValueError(f"Gaia acquisition failed: {result['error']}")
    with ArxivClient(timeout=25, max_retries=1, total_timeout=80) as client:
        result = build_paper_corpus("ctts_herczeg_pilot", [
            {"arxiv_id": p["versioned_id"], "selection_reason": p["herczeg_connection"]}
            for p in spec["selection"]["papers"]], include_pdf=False, client=client)
    write_json(output / "paper-acquisition.json", result)
    if not all(p.get("acquisition_complete") for p in result["papers"]):
        raise ValueError("Paper acquisition incomplete; inspect paper-acquisition.json and retry")
    print("Four explicit targets and four versioned papers acquired. Run prepare next.")


def pinned_manifest(root: Path) -> Path:
    current = read_json(root / "manifest.json")
    run = current.get("active_run_id")
    if not run or Path(run).name != run:
        raise ValueError(f"No published run in {root}")
    return root / "runs" / run / "manifest.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=SPEC)
    parser.add_argument("--output", type=Path, default=OUT)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("acquire", help="Network: fetch only the four explicit targets/papers")
    prep = sub.add_parser("prepare", help="Offline: compile pending tasks from immutable runs")
    prep.add_argument("--source-manifest", type=Path)
    prep.add_argument("--paper-manifest", type=Path)
    exp = sub.add_parser("export", help="Offline: produce A/B/C model prompts and response templates")
    exp.add_argument("--condition", choices=["A", "B", "C", "all"], default="all")
    sub.add_parser("self-check", help="Offline: check reference scoring; NOT a model evaluation")
    public = sub.add_parser("export-observatory", help="Export the selected stars/tasks as a public Pages example")
    public.add_argument("--source-manifest", type=Path)
    public.add_argument("--destination", type=Path, default=ROOT / "examples/observatory/research.json")
    grading = sub.add_parser("score", help="Offline: score responses from an external model run")
    grading.add_argument("predictions", type=Path)
    grading.add_argument("--report", type=Path)
    args = parser.parse_args()
    output = args.output
    if args.command == "acquire":
        acquire(read_json(args.spec), output)
    elif args.command == "prepare":
        bundle = compile_bundle(read_json(args.spec),
                                args.source_manifest or pinned_manifest(SOURCE),
                                args.paper_manifest or pinned_manifest(PAPERS))
        write_json(output / "bundle.json", bundle)
        print(f"Prepared {len(bundle['tasks'])} pending development tasks: {output / 'bundle.json'}")
    else:
        bundle = read_json(output / "bundle.json")
        if args.command == "export-observatory":
            from astro_pipeline.viz.research import public_research_snapshot
            manifest = args.source_manifest or SOURCE / "runs" / bundle["source_snapshot"]["run_id"] / "manifest.json"
            write_json(args.destination, public_research_snapshot(bundle, manifest))
            print(f"Exported selected public examples to {args.destination}; build/publish Pages to update the site.")
        elif args.command == "export":
            for condition in "ABC" if args.condition == "all" else args.condition:
                write_json(output / f"prompts-{condition}.json", export_prompts(bundle, condition))
                write_json(output / f"predictions-{condition}.template.json", {
                    "schema_version": "research-predictions-1", "bundle_sha256": bundle["bundle_sha256"],
                    "condition": condition, "run_kind": "model_inference", "model": None,
                    "settings": {"temperature": None, "max_output_tokens": None, "seed": None},
                    "responses": [],
                })
            print(f"Exported prompts and empty response templates to {output}; no model calls made.")
        elif args.command == "self-check":
            predictions = reference_predictions(bundle)
            report = score(bundle, predictions)
            write_json(output / "reference-self-check.json", predictions)
            write_json(output / "reference-self-check-score.json", report)
            print(f"Reference self-check only: {report['metrics']}; NOT model accuracy.")
        else:
            report = score(bundle, read_json(args.predictions))
            path = args.report or output / f"score-{report['condition']}.json"
            write_json(path, report)
            print(f"{report['run_kind']}: {report['metrics']}\n{path}")


if __name__ == "__main__":
    main()
