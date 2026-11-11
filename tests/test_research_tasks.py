"""Offline data contracts, leakage prevention and scorer failure modes."""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

import pytest

from astro_pipeline.research_tasks import (
    compile_bundle, digest, export_prompts, read_json, reference_predictions, score, write_json,
)


@pytest.fixture
def inputs(tmp_path):
    source_run, paper_run = "source-run", "paper-run"
    src = tmp_path / "sources" / "runs" / source_run
    pp = tmp_path / "papers" / "runs" / paper_run
    src.mkdir(parents=True)
    pp.mkdir(parents=True)
    csv = b"Gaia,gaia_release,Plx_gdr3\n5401795662560500352,dr3,16.6\n"
    (src / "members.csv").write_bytes(csv)
    write_json(src / "manifest.json", {
        "snapshot_run_id": source_run, "pipeline_status": "completed", "errors": [],
        "artifacts": {"members_csv": {"sha256": hashlib.sha256(csv).hexdigest()}},
    })
    document = {
        "paper": {"versioned_id": "2308.14590v1"}, "parser": {"version": "fixture"},
        "blocks": [{"id": "b1", "text": "Synthetic context: uncertainty is 2 out of 5.",
                    "locator": {"html_id": "S1.p1"}, "section_path": ["Results"]},
                   {"id": "b2", "cells": [[{"text": "Object"}, {"text": "Value"}],
                                            [{"text": "TW Hya"}, {"text": "2"}]]}],
    }
    blob = tmp_path / "objects" / "paper.json"
    write_json(blob, document)
    write_json(pp / "manifest.json", {
        "run_id": paper_run, "status": "partial", "library_root": str(tmp_path),
        "papers": [{"versioned_id": "2308.14590v1", "version_id": "version-1",
                    "acquisition_complete": True, "status": "partial", "title": "Synthetic paper",
                    "artifacts": [{"kind": "json", "relative_path": "objects/paper.json",
                                   "sha256": hashlib.sha256(blob.read_bytes()).hexdigest()}]}],
    })
    spec = {
        "suite_id": "fixture", "selection": {
            "targets": [{"name": "TW Hya", "release": "dr3", "source_id": "5401795662560500352"}],
            "papers": [{"versioned_id": "2308.14590v1"}]},
        "materials": [{"id": "m1", "kind": "paper", "paper": "2308.14590v1", "block_id": "b1",
                       "quote": "uncertainty is 2 out of 5"},
                      {"id": "m2", "kind": "gaia", "target": "TW Hya",
                       "fields": [{"column": "Plx_gdr3", "unit": "mas"}]}],
        "tasks": [{"id": "case1", "targets": ["TW Hya"], "paper_ids": ["2308.14590v1"],
                   "category": "numeric", "split": "development", "review_status": "pending",
                   "leakage_group": "one", "question": "Return the relative uncertainty, unit=1.",
                   "material_ids": ["m1"], "reference": {
                       "kind": "number", "answer": 0.4, "unit": "1", "rtol": 0.001, "atol": 0,
                       "required_evidence_ids": ["m1"], "worked_solution": ["SECRET_REFERENCE"]}},
                  {"id": "case2", "targets": ["TW Hya"], "paper_ids": [],
                   "category": "missing", "split": "development", "review_status": "pending",
                   "leakage_group": "one", "question": "Can parallax alone fix accretion?",
                   "material_ids": ["m2"], "choices": ["yes", "insufficient_information"],
                   "reference": {"kind": "choice", "answer": "insufficient_information",
                                 "required_evidence_ids": ["m2"]}}],
    }
    return spec, src / "manifest.json", pp / "manifest.json", blob


def bundle_from(inputs):
    return compile_bundle(*inputs[:3])


def test_real_pilot_is_one_pending_component_and_math_is_recomputable():
    spec = read_json(Path(__file__).parents[1] / "benchmarks/ctts_herczeg/tasks.json")
    assert len(spec["selection"]["targets"]) == 4
    assert len(spec["tasks"]) == 16
    assert {t["split"] for t in spec["tasks"]} == {"development"}
    assert {t["review_status"] for t in spec["tasks"]} == {"pending"}
    assert len({t["leakage_group"] for t in spec["tasks"]}) == 1
    operations = {"sum": sum, "subtract": lambda x: x[0] - x[1],
                  "multiply": math.prod, "divide": lambda x: x[0] / x[1],
                  "power": lambda x: x[0] ** x[1], "veiling_fraction": lambda x: x[0] / (1 + x[0])}
    for task in spec["tasks"]:
        ref = task["reference"]
        if "calculation" in ref:
            calc = ref["calculation"]
            assert math.isclose(operations[calc["operation"]](calc["operands"]), ref["answer"], rel_tol=1e-10)


def test_compile_pins_identity_and_exact_quote_offsets(inputs):
    bundle = bundle_from(inputs)
    assert bundle["selection"]["targets"][0]["source_id"] == "5401795662560500352"
    assert bundle["papers"][0]["acquisition_status"] == "partial"
    evidence = bundle["materials"][0]
    loc = evidence["provenance"]["location"]
    text = read_json(inputs[3])["blocks"][0]["text"]
    assert text[loc["start"]:loc["end"]] == evidence["text"]
    assert score(bundle, reference_predictions(bundle))["run_kind"] == "reference_self_check"
    assert not bundle["scientifically_verified"]


@pytest.mark.parametrize("change", ["csv_checksum", "paper_checksum", "release", "incomplete_paper", "paper_identity", "mutable_manifest", "quote", "missing_field", "split_group"])
def test_compile_rejects_mismatched_or_unpinned_inputs(inputs, change, tmp_path):
    spec, sm, pm, blob = inputs
    if change == "csv_checksum":
        (sm.parent / "members.csv").write_text("modified", encoding="utf-8")
    elif change == "paper_checksum":
        blob.write_text("{}", encoding="utf-8")
    elif change == "release":
        spec["selection"]["targets"][0]["release"] = "dr4"
    elif change == "incomplete_paper":
        p = read_json(pm); p["papers"][0]["acquisition_complete"] = False; write_json(pm, p)
    elif change == "paper_identity":
        d = read_json(blob); d["paper"]["versioned_id"] = "2308.14590v2"; write_json(blob, d)
        p = read_json(pm); p["papers"][0]["artifacts"][0]["sha256"] = hashlib.sha256(blob.read_bytes()).hexdigest(); write_json(pm, p)
    elif change == "mutable_manifest":
        target = tmp_path / "manifest.json"; target.write_bytes(sm.read_bytes()); sm = target
    elif change == "quote":
        spec["materials"][0]["quote"] = "not in source"
    elif change == "missing_field":
        spec["materials"][1]["fields"][0]["column"] = "absent"
    elif change == "split_group":
        spec["tasks"][1]["leakage_group"] = "other"
    with pytest.raises(ValueError):
        compile_bundle(spec, sm, pm)


def test_table_cell_checks_row_and_retains_zero_based_location(inputs):
    spec = inputs[0]
    spec["materials"][0] = {"id": "m1", "kind": "paper", "paper": "2308.14590v1", "block_id": "b2",
                            "field": "cell", "row": 1, "column": 1, "equals": "2",
                            "row_guards": [{"column": 0, "equals": "TW Hya"}]}
    b = bundle_from(inputs)
    assert b["materials"][0]["provenance"]["location"]["row_label"] == "TW Hya"
    spec["materials"][0]["row_guards"][0]["equals"] = "RU Lup"
    with pytest.raises(ValueError, match="row identity"):
        bundle_from(inputs)


def test_prompt_export_never_includes_reference_or_future_tasks(inputs):
    b = bundle_from(inputs)
    outputs = {c: export_prompts(b, c) for c in "ABC"}
    for condition, output in outputs.items():
        text = json.dumps(output)
        assert "SECRET_REFERENCE" not in text and "worked_solution" not in text
        assert "bundle_sha256" in output
        first = output["prompts"][0]["messages"][-1]["content"]
        assert "parallax" not in first
        assert ("uncertainty is 2 out of 5" in first) == (condition != "A")
    c_text = outputs["C"]["prompts"][0]["messages"][-1]["content"]
    c_materials = json.loads(c_text.split("材料：\n", 1)[1])
    assert c_materials == [b["materials"][0]]
    b_text = outputs["B"]["prompts"][0]["messages"][-1]["content"]
    def leaves(value):
        if isinstance(value, dict): return [v for x in value.values() for v in leaves(x)]
        if isinstance(value, list): return [v for x in value for v in leaves(x)]
        return [json.dumps(value, ensure_ascii=False)]
    assert all(leaf in b_text for leaf in leaves(c_materials))


@pytest.mark.parametrize("answer", [0, False, "0.4", None, float("nan"), float("inf")])
def test_numeric_scoring_rejects_wrong_or_nonfinite_values(inputs, answer):
    b = bundle_from(inputs)
    p = reference_predictions(b)
    p["responses"][0]["answer"] = answer
    assert not score(b, p)["results"][0]["answer_correct"]


def test_tiny_accretion_rates_do_not_accept_zero(inputs):
    inputs[0]["tasks"][0]["reference"]["answer"] = 2.51e-9
    b = bundle_from(inputs)
    p = reference_predictions(b)
    p["responses"][0]["answer"] = 0
    assert not score(b, p)["results"][0]["answer_correct"]


def test_units_missing_answers_and_unsupported_citations_reduce_score(inputs):
    b = bundle_from(inputs)
    p = reference_predictions(b)
    p["responses"][0]["unit"] = "%"
    p["responses"][0]["evidence_ids"] = ["m2"]  # Real material, unavailable to this task.
    p["responses"].pop()
    report = score(b, p)
    assert report["metrics"]["n"] == 2
    assert report["metrics"]["answered"] == 1
    assert report["metrics"]["automated_pass_rate"] == 0
    assert not report["results"][0]["reference_evidence_match"]
    assert score(b, reference_predictions(b, "A"))["metrics"]["reference_evidence_match_rate"] is None


@pytest.mark.parametrize("change", ["duplicate", "unknown", "bundle", "model"])
def test_misbound_prediction_files_are_rejected(inputs, change):
    b = bundle_from(inputs)
    p = reference_predictions(b)
    if change == "duplicate": p["responses"].append(deepcopy(p["responses"][0]))
    elif change == "unknown": p["responses"][0]["task_id"] = "case99"
    elif change == "bundle": p["bundle_sha256"] = "stale"
    else: p["run_kind"] = "model_inference"
    with pytest.raises(ValueError): score(b, p)


def test_edited_bundle_requires_new_export(inputs):
    b = bundle_from(inputs)
    b["tasks"][0]["question"] = "edited"
    with pytest.raises(ValueError, match="checksum"):
        export_prompts(b, "C")
