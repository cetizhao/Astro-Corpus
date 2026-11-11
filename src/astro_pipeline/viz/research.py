"""Read-only, path-free projection of compiled research-task bundles for the viewer."""
from __future__ import annotations

from pathlib import Path
import csv
from datetime import datetime, timezone
import hashlib
import io
import math

from astro_pipeline.research_tasks import read_json, verify_bundle


def project_bundle(bundle: dict) -> dict:
    verify_bundle(bundle)
    if bundle["schema_version"] not in {"research-task-1", "research-task-2"}:
        raise ValueError("Unsupported bundle schema")
    paper_targets = {p["versioned_id"]: p.get("targets", []) for p in bundle["selection"]["papers"]}
    return {
        "suite_id": bundle["suite_id"], "bundle_sha256": bundle["bundle_sha256"],
        "created_at": bundle["created_at"], "split": bundle["split"],
        "review_status": bundle["review_status"],
        "targets": [{k: t[k] for k in ("name", "release", "source_id")}
                    for t in bundle["selection"]["targets"]],
        "papers": [{k: p[k] for k in ("versioned_id", "library_version_id", "title", "url", "artifact_sha256")}
                   | {"targets": paper_targets[p["versioned_id"]]}
                   for p in bundle["papers"]],
        "materials": bundle["materials"],
        "tasks": [{k: t[k] for k in ("id", "question", "category", "targets", "material_ids",
                                     "reference", "split", "review_status", "leakage_group")}
                  | {k: t[k] for k in ("choices", "title", "task_level", "response_fields") if k in t}
                  for t in bundle["tasks"]],
    }


def research_snapshot(root: Path | None) -> dict:
    """Read prepared bundles without opening SQLite or exposing arbitrary files."""
    suites, warnings = [], []
    for path in sorted(root.glob("*/bundle.json")) if root else []:
        try:
            bundle = read_json(path)
            suites.append(project_bundle(bundle))
        except (OSError, ValueError, KeyError, TypeError):
            # Do not turn a corrupted bundle into a successful-looking empty list.
            warnings.append(f"{path.parent.name}：任务包无法读取或校验失败，请重新生成。")
    return {"schema_version": "research-view-1", "suites": suites, "warnings": warnings}


def public_research_snapshot(bundle: dict, source_manifest: Path) -> dict:
    """Export only explicitly selected stars and evidence excerpts, never the local library."""
    suite = project_bundle(bundle)
    raw_manifest = source_manifest.read_bytes()
    pin = bundle["source_snapshot"]
    if hashlib.sha256(raw_manifest).hexdigest() != pin["manifest_sha256"]:
        raise ValueError("Source manifest differs from the task bundle")
    manifest = read_json(source_manifest)
    if manifest.get("snapshot_run_id") != pin["run_id"] or source_manifest.parent.name != pin["run_id"]:
        raise ValueError("An immutable source run is required")
    raw_csv = (source_manifest.parent / "members.csv").read_bytes()
    if hashlib.sha256(raw_csv).hexdigest() != pin["members_sha256"]:
        raise ValueError("Source data checksum mismatch")
    rows = list(csv.DictReader(io.StringIO(raw_csv.decode("utf-8-sig"))))
    dataset = {"corpus_id": manifest.get("corpus_id", suite["suite_id"]),
               "label": manifest.get("corpus", {}).get("label", suite["suite_id"]),
               "run_id": pin["run_id"], "status": "completed", "snapshot_state": "current",
               "selection_type": "literature_target_list", "n_member_rows": len(suite["targets"])}
    sources = []
    def number(row: dict, key: str):
        try:
            result = float(row.get(key, ""))
            return result if math.isfinite(result) else None
        except (ValueError, TypeError):
            return None
    for target in suite["targets"]:
        matches = [row for row in rows if (row["gaia_release"], row["Gaia"]) == (target["release"], target["source_id"])]
        if len(matches) != 1 or target["release"] != "dr3":
            raise ValueError("This export requires exactly one Gaia DR3 row per target")
        row = matches[0]
        ra, dec = number(row, "RAICRS"), number(row, "DEICRS")
        sources.append({
            "key": f"{target['release']}:{target['source_id']}", **target,
            "catalog_names": [], "annotation_count": 0, "annotation_paper_count": 0,
            "position": {"ra_deg": ra, "dec_deg": dec, "frame": "ICRS", "epoch": 2016.0,
                         "epoch_status": "release_reference_epoch",
                         "valid": ra is not None and dec is not None and 0 <= ra < 360 and -90 <= dec <= 90},
            "astrometry": {name: number(row, column) for name, column in {
                "parallax_mas": "Plx_gdr3", "pmra_masyr": "pmRA_gdr3", "pmdec_masyr": "pmDE_gdr3",
                "radial_velocity_kms": "RV_gdr3", "g_mag": "Gmag_gdr3", "bp_mag": "BPmag_gdr3",
                "rp_mag": "RPmag_gdr3", "ruwe": "RUWE_gdr3"}.items()},
            "datasets": [{**dataset, "n_member_rows": 1}], "working_datasets": [],
            "spectrum_status": "not_downloaded",
        })
    papers = {p["versioned_id"]: p for p in bundle["papers"]}
    contexts = {}
    for material in suite["materials"]:
        if material["kind"] != "paper_excerpt":
            continue
        provenance = material["provenance"]
        loc = provenance["location"]
        paper = papers[provenance["paper"]]
        key = f"{paper['library_version_id']}:{loc['block_id']}"
        context = contexts.setdefault(key, {"available": True, "paper": {
            "versioned_id": paper["versioned_id"], "title": paper["title"],
            "paper_version_id": paper["library_version_id"], "document_sha256": paper["artifact_sha256"],
            "parser_version": paper["parser"].get("cache_key", paper["parser"]["version"]),
        }, "units": [], "coverage": "selected_excerpts_only"})
        unit = {k: loc[k] for k in ("block_id", "start", "end", "row", "column", "section_path") if k in loc}
        unit.update(field="table_cell" if loc["field"] == "cell" else loc["field"], text=material["text"],
                    source_url=f"https://arxiv.org/html/{paper['versioned_id']}#{loc.get('original', {}).get('html_id', '')}")
        if unit not in context["units"]:
            context["units"].append(unit)
    return {"exported_at": datetime.now(timezone.utc).isoformat(), "sources": sources,
            "datasets": [dataset], "contexts": contexts,
            "research_tasks": {"schema_version": "research-view-1", "suites": [suite], "warnings": []}}


def merge_public_research(snapshots: list[dict]) -> dict:
    """Combine suites sharing source pins without duplicating stars or losing excerpts."""
    if not snapshots:
        raise ValueError("At least one research snapshot is required")
    sources, datasets, contexts, suites = {}, {}, {}, {}
    for snapshot in snapshots:
        for source in snapshot["sources"]:
            if source["key"] in sources and sources[source["key"]] != source:
                raise ValueError("Research suites disagree on a source snapshot")
            sources[source["key"]] = source
        for dataset in snapshot["datasets"]:
            key = dataset["corpus_id"], dataset["run_id"]
            if key in datasets and datasets[key] != dataset:
                raise ValueError("Research suites disagree on a dataset")
            datasets[key] = dataset
        for key, context in snapshot["contexts"].items():
            if key in contexts:
                if contexts[key]["paper"] != context["paper"]:
                    raise ValueError("Research suites disagree on a paper version")
                units = contexts[key]["units"]
                def contains(outer: dict, inner: dict) -> bool:
                    return (all(outer.get(k) == inner.get(k) for k in ("block_id", "field", "row", "column", "note_index"))
                            and outer["start"] <= inner["start"] <= inner["end"] <= outer["end"]
                            and outer["text"][inner["start"] - outer["start"]:inner["end"] - outer["start"]] == inner["text"])
                for unit in context["units"]:
                    # Keep the wider exact excerpt once; both tasks' offsets still resolve.
                    if not any(contains(existing, unit) for existing in units):
                        units[:] = [existing for existing in units if not contains(unit, existing)]
                        units.append(unit)
            else:
                contexts[key] = {**context, "units": list(context["units"])}
        for suite in snapshot["research_tasks"]["suites"]:
            if suite["suite_id"] in suites:
                raise ValueError("Duplicate research suite")
            suites[suite["suite_id"]] = suite
    return {"exported_at": max(s["exported_at"] for s in snapshots),
            "sources": list(sources.values()), "datasets": list(datasets.values()), "contexts": contexts,
            "research_tasks": {"schema_version": "research-view-1", "suites": list(suites.values()), "warnings": []}}
