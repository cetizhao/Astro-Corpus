"""Pinned research-task packets and deterministic scoring, without model calls.

Reference solutions are development annotations. Matching them is not scientific
verification, evidence entailment checking, or evidence of a training benefit.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import re
from typing import Any


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def read_json(path: Path | str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path | str, value: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")


def _checked_bytes(path: Path, sha256: str) -> bytes:
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError(f"Artifact checksum mismatch: {path.name}")
    return data


def _unique(items: list[dict], key: str) -> dict:
    result = {}
    for item in items:
        if item[key] in result:
            raise ValueError(f"Duplicate {key}: {item[key]}")
        result[item[key]] = item
    return result


def _slice(text: str, selector: dict) -> tuple[str, int, int]:
    if "quote" not in selector:
        return text, 0, len(text)
    quote = selector["quote"]
    if not quote or text.count(quote) != 1:
        raise ValueError("Evidence quote must occur exactly once in its selected field")
    start = text.index(quote)
    return quote, start, start + len(quote)


def _paper_material(selector: dict, document: dict, pin: dict) -> dict:
    blocks = _unique(document["blocks"], "id")
    block = blocks[selector["block_id"]]
    field = selector.get("field", "text")
    location = {"block_id": block["id"], "field": field,
                "original": block.get("locator", {}),
                "section_path": block.get("section_path", [])}
    if field == "cell":
        row, column = selector["row"], selector["column"]
        if row < 0 or column < 0:
            raise ValueError("Table indices must be nonnegative")
        text = block["cells"][row][column]["text"]
        location.update(row=row, column=column)
        for guard in selector.get("row_guards", []):
            if block["cells"][row][guard["column"]]["text"] != guard["equals"]:
                raise ValueError("Table row identity changed")
        location["row_label"] = block["cells"][row][0]["text"]
        location["column_label"] = block["cells"][0][column]["text"]
    elif field in {"text", "caption"}:
        text = block[field]
    else:
        raise ValueError(f"Unsupported evidence field: {field}")
    for anchor in selector.get("contains", []):
        if anchor not in text:
            raise ValueError(f"Evidence anchor absent in {block['id']}: {anchor}")
    if "equals" in selector and text != selector["equals"]:
        raise ValueError(f"Evidence field changed: {block['id']}")
    text, start, end = _slice(text, selector)
    location.update(start=start, end=end)
    return {"id": selector["id"], "kind": "paper_excerpt", "text": text,
            "provenance": {"paper": pin["versioned_id"], "title": pin["title"],
                           "url": pin["url"], "artifact_sha256": pin["artifact_sha256"],
                           "location": location}}


def compile_bundle(spec: dict, source_manifest: Path | str,
                   paper_manifest: Path | str) -> dict:
    """Compile explicit selectors against immutable runs; never fetch or migrate."""
    source_manifest, paper_manifest = Path(source_manifest), Path(paper_manifest)
    source = read_json(source_manifest)
    papers = read_json(paper_manifest)
    source_run = source.get("snapshot_run_id")
    paper_run = papers.get("run_id")
    if not source_run or source_manifest.parent.name != source_run:
        raise ValueError("Select a source runs/<run_id>/manifest.json snapshot")
    if not paper_run or paper_manifest.parent.name != paper_run:
        raise ValueError("Select a paper runs/<run_id>/manifest.json snapshot")
    if source.get("pipeline_status", source.get("status")) != "completed" or source.get("errors"):
        raise ValueError("Source snapshot is not complete")
    if papers.get("status") not in {"completed", "complete", "partial"}:
        raise ValueError("Paper run is not terminal")
    artifact = source["artifacts"]["members_csv"]
    members_bytes = _checked_bytes(source_manifest.parent / "members.csv", artifact["sha256"])
    # csv.DictReader keeps 19-digit Gaia identifiers as strings.
    rows = list(csv.DictReader(io.StringIO(members_bytes.decode("utf-8-sig"))))
    targets = _unique(spec["selection"]["targets"], "name")
    selected_rows = {}
    identities = set()
    for name, target in targets.items():
        identity = target["release"], target["source_id"]
        if not isinstance(identity[1], str) or not re.fullmatch(r"[0-9]+", identity[1]):
            raise ValueError("Gaia source_id must be a decimal string")
        if identity in identities:
            raise ValueError("Duplicate selected Gaia identity")
        identities.add(identity)
        matches = [r for r in rows if (r.get("gaia_release"), r.get("Gaia")) == identity]
        if len(matches) != 1:
            raise ValueError(f"Expected one explicit target occurrence for {name}; found {len(matches)}")
        selected_rows[name] = matches[0]
    if len(rows) != len(targets):
        raise ValueError("Source snapshot differs from the explicit target list")

    items = _unique(papers["papers"], "versioned_id")
    pins, documents = {}, {}
    for selected in spec["selection"]["papers"]:
        versioned_id = selected["versioned_id"]
        if not re.search(r"v[1-9][0-9]*$", versioned_id):
            raise ValueError("Paper selection requires explicit arXiv versions")
        item = items[versioned_id]
        if not item.get("acquisition_complete"):
            raise ValueError(f"Incomplete paper acquisition: {versioned_id}")
        artifacts = [a for a in item["artifacts"] if a["kind"] == "json"]
        if len(artifacts) != 1:
            raise ValueError(f"Expected one parsed document: {versioned_id}")
        a = artifacts[0]
        # The immutable manifest records its library root; no current-version lookup.
        relative = Path(a["relative_path"].replace("\\", "/"))
        document = json.loads(_checked_bytes(Path(papers["library_root"]) / relative, a["sha256"]))
        if document["paper"]["versioned_id"] != versioned_id:
            raise ValueError("Parsed paper identity conflicts with its pin")
        pins[versioned_id] = {
            "versioned_id": versioned_id, "library_version_id": item["version_id"],
            "artifact_sha256": a["sha256"], "parser": document["parser"],
            "title": item["title"], "authors": document["paper"].get("authors", []),
            "url": f"https://arxiv.org/abs/{versioned_id}",
            "rights": document.get("rights", {}), "quality": document.get("quality", {}),
            "acquisition_status": item["status"],
        }
        documents[versioned_id] = document

    materials = []
    for selector in spec["materials"]:
        if selector["kind"] == "paper":
            p = selector["paper"]
            material = _paper_material(selector, documents[p], pins[p])
        elif selector["kind"] == "gaia":
            target = targets[selector["target"]]
            row = selected_rows[selector["target"]]
            fields = []
            for f in selector["fields"]:
                if f["column"] not in row:
                    raise ValueError(f"Missing Gaia column: {f['column']}")
                fields.append({"column": f["column"], "unit": f["unit"],
                               "value": row[f["column"]] or None})
            material = {"id": selector["id"], "kind": "gaia_fields", "fields": fields,
                        "provenance": {"target": target["name"], "release": target["release"],
                                       "source_id": target["source_id"], "run_id": source_run,
                                       "artifact_sha256": artifact["sha256"]}}
        else:
            raise ValueError("Unknown material kind")
        materials.append(material)
    material_index = _unique(materials, "id")
    tasks = list(_unique(spec["tasks"], "id").values())
    # Objects, papers and companion campaigns must stay together across splits.
    for i, task in enumerate(tasks):
        if task["split"] != "development" or task["review_status"] != "pending":
            raise ValueError("This compiler only produces pending development tasks")
        if not set(task["targets"]) <= targets.keys():
            raise ValueError("Task names an unselected target")
        if not set(task["paper_ids"]) <= pins.keys():
            raise ValueError("Task names an unselected paper")
        if not set(task["material_ids"]) <= material_index.keys():
            raise ValueError("Task references absent material")
        ref = task["reference"]
        if not set(ref["required_evidence_ids"]) <= set(task["material_ids"]):
            raise ValueError("Reference evidence is unavailable to the task")
        if ref["kind"] == "number":
            if not _number(ref["answer"]) or ref["atol"] < 0 or ref["rtol"] < 0:
                raise ValueError("Invalid numeric reference")
        elif ref["kind"] != "choice" or ref["answer"] not in task["choices"]:
            raise ValueError("Invalid categorical reference")
        for other in tasks[:i]:
            if task["leakage_group"] != other["leakage_group"]:
                shared_targets = set(task["targets"]) & set(other["targets"])
                shared_papers = set(task["paper_ids"]) & set(other["paper_ids"])
                if shared_targets or shared_papers:
                    raise ValueError("Shared objects/papers cannot have distinct leakage groups")
    bundle = {
        "schema_version": "research-task-1", "suite_id": spec["suite_id"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "split": "development", "review_status": "pending", "scientifically_verified": False,
        "selection": spec["selection"],
        "source_snapshot": {"run_id": source_run, "members_sha256": artifact["sha256"],
                            "manifest_sha256": hashlib.sha256(source_manifest.read_bytes()).hexdigest()},
        "paper_snapshot": {"run_id": paper_run,
                           "manifest_sha256": hashlib.sha256(paper_manifest.read_bytes()).hexdigest()},
        "papers": list(pins.values()), "materials": materials, "tasks": tasks,
    }
    bundle["bundle_sha256"] = digest(bundle)
    return bundle


def verify_bundle(bundle: dict) -> None:
    payload = {k: v for k, v in bundle.items() if k != "bundle_sha256"}
    if digest(payload) != bundle.get("bundle_sha256"):
        raise ValueError("Bundle checksum mismatch")


def _plain(value: Any, prefix: str = "") -> list[str]:
    """Render every leaf once, without adding facts to either B or C."""
    if isinstance(value, dict):
        return [line for key, item in value.items()
                for line in _plain(item, f"{prefix}.{key}" if prefix else key)]
    if isinstance(value, list):
        return [line for i, item in enumerate(value) for line in _plain(item, f"{prefix}[{i}]")]
    return [f"{prefix}: {json.dumps(value, ensure_ascii=False)}"]


def export_prompts(bundle: dict, condition: str) -> dict:
    """A has no material; B/C contain the exact same selected evidence facts."""
    verify_bundle(bundle)
    if condition not in {"A", "B", "C"}:
        raise ValueError("Condition must be A, B or C")
    materials = _unique(bundle["materials"], "id")
    instructions = (
        "回答天文学研究问题。仅使用本题材料和题目明确给出的假设；没有材料时可用已有知识，"
        "但不要编造数据。输出 JSON 对象，字段为 task_id、answer、unit、evidence_ids。"
        "数值 answer 使用 JSON 数字，选择题使用指定选项原样字符串；"
        "非数值题 unit 为 null，数值题使用题目指定的单位。evidence_ids 为支持回答的材料 id 列表；"
        "未提供材料时用空列表。可另附简短 explanation，不要求输出内部思维过程。"
    )
    prompts = []
    for task in bundle["tasks"]:
        # Explicit whitelist: references, derivations and review notes never enter prompts.
        query = {"task_id": task["id"], "question": task["question"]}
        if "choices" in task:
            query["choices"] = task["choices"]
        text = json.dumps(query, ensure_ascii=False, indent=2)
        if condition != "A":
            facts = [materials[mid] for mid in task["material_ids"]]
            rendered = "\n\n".join("\n".join(_plain(m)) for m in facts) if condition == "B" else json.dumps(facts, ensure_ascii=False, indent=2)
            text += "\n\n材料：\n" + rendered
        messages = [{"role": "system", "content": instructions}, {"role": "user", "content": text}]
        prompts.append({"task_id": task["id"], "messages": messages,
                        "character_count": sum(len(m["content"]) for m in messages)})
    return {"schema_version": "research-prompts-1", "bundle_sha256": bundle["bundle_sha256"],
            "condition": condition, "prompts": prompts}


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def reference_predictions(bundle: dict, condition: str = "C") -> dict:
    """Fixture for protocol checks only; never label this a model evaluation."""
    verify_bundle(bundle)
    return {"schema_version": "research-predictions-1", "bundle_sha256": bundle["bundle_sha256"],
            "condition": condition, "run_kind": "reference_self_check", "model": None,
            "settings": {}, "responses": [
                {"task_id": task["id"], "answer": task["reference"]["answer"],
                 "unit": task["reference"].get("unit"),
                 "evidence_ids": task["reference"]["required_evidence_ids"] if condition != "A" else []}
                for task in bundle["tasks"]]}


def score(bundle: dict, predictions: dict) -> dict:
    verify_bundle(bundle)
    if predictions.get("bundle_sha256") != bundle["bundle_sha256"]:
        raise ValueError("Predictions refer to a different bundle")
    condition = predictions["condition"]
    if condition not in {"A", "B", "C"}:
        raise ValueError("Unknown evaluation condition")
    run_kind = predictions["run_kind"]
    if run_kind not in {"model_inference", "reference_self_check"}:
        raise ValueError("Unknown run kind")
    if run_kind == "model_inference" and not predictions.get("model"):
        raise ValueError("Record the model used for inference")
    responses = _unique(predictions["responses"], "task_id")
    if set(responses) - {t["id"] for t in bundle["tasks"]}:
        raise ValueError("Predictions contain unknown task IDs")
    results = []
    for task in bundle["tasks"]:
        p = responses.get(task["id"], {})
        ref = task["reference"]
        answer = p.get("answer")
        if ref["kind"] == "number":
            correct = _number(answer) and math.isclose(answer, ref["answer"], rel_tol=ref["rtol"], abs_tol=ref["atol"])
            unit = isinstance(p.get("unit"), str) and p["unit"].strip() in [ref["unit"], *ref.get("unit_aliases", [])]
        else:
            correct = isinstance(answer, str) and answer.strip() == ref["answer"]
            unit = bool(p) and p.get("unit") is None
        citations = p.get("evidence_ids")
        valid_citations = isinstance(citations, list) and all(isinstance(c, str) for c in citations)
        evidence = None if condition == "A" else bool(
            valid_citations and set(citations) <= set(task["material_ids"])
            and set(ref["required_evidence_ids"]) <= set(citations))
        results.append({"task_id": task["id"], "category": task["category"],
                        "answered": bool(p), "answer_correct": correct, "unit_correct": unit,
                        "reference_evidence_match": evidence,
                        "automated_pass": bool(correct and unit and (evidence is not False))})
    def metrics(rows: list[dict]) -> dict:
        n = len(rows)
        return {"n": n, "answered": sum(r["answered"] for r in rows),
                "answer_and_unit_accuracy": sum(r["answer_correct"] and r["unit_correct"] for r in rows) / n,
                "reference_evidence_match_rate": None if condition == "A" else sum(r["reference_evidence_match"] for r in rows) / n,
                "automated_pass_rate": sum(r["automated_pass"] for r in rows) / n}
    if not results:
        raise ValueError("Cannot score an empty suite")
    return {"schema_version": "research-score-1", "bundle_sha256": bundle["bundle_sha256"],
            "condition": condition, "run_kind": run_kind, "model": predictions.get("model"),
            "settings": predictions.get("settings", {}), "review_status": "pending",
            "interpretation": "Reference matching only; no scientific verification or training-gain claim.",
            "metrics": metrics(results), "categories": {
                category: metrics([r for r in results if r["category"] == category])
                for category in sorted({r["category"] for r in results})}, "results": results}
