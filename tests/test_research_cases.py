"""Integrated cases: partial credit, withheld evidence and portable offline scoring."""
from copy import deepcopy
import json
import math
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from astro_pipeline.research_tasks import (
    compile_bundle, export_evaluation_package, export_prompts, read_json,
    reference_predictions, score, write_json,
)
from astro_pipeline.viz.research import merge_public_research, public_research_snapshot
from tests.test_research_tasks import inputs  # noqa: F401 -- shared immutable-snapshot fixture


@pytest.fixture
def cases(inputs):
    task = inputs[0]["tasks"][0]
    first = {"id": "ratio", **task["reference"]}
    second = {"id": "parallax", "kind": "number", "answer": 16.6, "unit": "mas",
              "rtol": 0, "atol": 0.001, "required_evidence_ids": ["m2"]}
    task.update(material_ids=["m1", "m2"], task_level="integrated_case", title="Synthetic case",
                response_fields=[{"id": "ratio", "request": "Relative uncertainty", "kind": "number", "unit": "1"},
                                 {"id": "parallax", "request": "Read parallax", "kind": "number", "unit": "mas"}],
                reference={"kind": "structured", "checkpoints": [first, second], "required_evidence_ids": ["m1", "m2"]},
                ablation={"withhold_material_ids": ["m1"], "reference_overrides": {
                    "ratio": {"kind": "choice", "answer": "insufficient_information", "unit": None, "required_evidence_ids": []}}})
    inputs[0]["tasks"] = [task]
    return inputs


def test_checkpoints_receive_partial_credit_and_missing_steps_count(cases):
    b = compile_bundle(*cases[:3])
    p = reference_predictions(b)
    p["responses"][0]["checkpoints"].pop()
    report = score(b, p)
    assert report["metrics"]["checkpoint_count"] == 2
    assert report["metrics"]["checkpoint_pass_rate"] == .5
    assert report["metrics"]["automated_pass_rate"] == 0
    assert not report["results"][0]["checkpoints"][1]["answered"]
    p["responses"][0]["checkpoints"][0]["evidence_ids"] = ["m2"]
    assert score(b, p)["metrics"]["checkpoint_pass_rate"] == 0


@pytest.mark.parametrize("change", ["duplicate", "unknown", "wrong_unit", "zero", "boolean"])
def test_checkpoints_reject_bad_responses(cases, change):
    b = compile_bundle(*cases[:3])
    p = reference_predictions(b)
    values = p["responses"][0]["checkpoints"]
    if change == "duplicate": values.append(deepcopy(values[0]))
    elif change == "unknown": values[0]["id"] = "fabricated"
    elif change == "wrong_unit": values[0]["unit"] = "percent"
    elif change == "zero": values[0]["answer"] = 0
    else: values[0]["answer"] = True
    if change in {"duplicate", "unknown"}:
        with pytest.raises(ValueError): score(b, p)
    else:
        assert score(b, p)["metrics"]["checkpoint_pass_rate"] == .5


def test_ablation_removes_only_evidence_and_changes_only_dependent_rubric(cases):
    b = compile_bundle(*cases[:3])
    c = export_prompts(b, "C")["prompts"][0]["messages"][1]["content"]
    d = export_prompts(b, "D")["prompts"][0]["messages"][1]["content"]
    assert c.split("材料：")[0] == d.split("材料：")[0]
    assert "uncertainty is 2 out of 5" in c and "uncertainty is 2 out of 5" not in d
    assert json.loads(d.split("材料：\n")[1]) == [b["materials"][1]]
    assert "SECRET_REFERENCE" not in d and "reference_overrides" not in d
    expected = reference_predictions(b, "D")
    assert score(b, expected)["metrics"]["checkpoint_pass_rate"] == 1
    # Persisting with the complete-evidence numerical answer must not pass D.
    wrong = reference_predictions(b, "C"); wrong["condition"] = "D"
    assert score(b, wrong)["metrics"]["checkpoint_pass_rate"] == .5
    # A valid abstention must not cite a material that D did not provide.
    expected["responses"][0]["checkpoints"][0]["evidence_ids"] = ["m1"]
    assert score(b, expected)["metrics"]["checkpoint_pass_rate"] == .5


def test_compiler_rejects_ablation_that_retains_unavailable_gold(cases):
    cases[0]["tasks"][0]["ablation"]["reference_overrides"] = {"parallax": {"required_evidence_ids": ["m2"]}}
    with pytest.raises(ValueError, match="unavailable"):
        compile_bundle(*cases[:3])


@pytest.mark.parametrize("condition", ["", "AB", "BC", "unknown"])
def test_conditions_are_single_named_experiments(cases, condition):
    b = compile_bundle(*cases[:3])
    with pytest.raises(ValueError): export_prompts(b, condition)
    p = reference_predictions(b); p["condition"] = condition
    with pytest.raises(ValueError): score(b, p)


def test_portable_package_runs_without_repository_or_dependencies(cases, tmp_path):
    b = compile_bundle(*cases[:3])
    archive = export_evaluation_package(b, tmp_path / "package")
    standalone = tmp_path / "standalone"
    with zipfile.ZipFile(archive) as zipped:
        zipped.extractall(standalone)
    assert read_json(standalone / "manifest.json")["model_calls"] == 0
    assert read_json(standalone / "responses/predictions-D.template.json")["model"] is None
    write_json(standalone / "fixture.json", reference_predictions(b, "D"))
    run = subprocess.run([sys.executable, "-I", "-c",
        "import sys,runpy;sys.path.insert(0,'.');sys.argv=['grade.py','fixture.json','score.json'];runpy.run_path('grade.py',run_name='__main__')"],
        cwd=standalone, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    report = read_json(standalone / "score.json")
    assert report["run_kind"] == "reference_self_check" and report["metrics"]["checkpoint_pass_rate"] == 1


def test_public_suites_share_stars_but_preserve_both_tasks_and_excerpts(cases):
    b = compile_bundle(*cases[:3])
    a = public_research_snapshot(b, cases[1])
    second = deepcopy(a)
    second["research_tasks"]["suites"][0]["suite_id"] = "another"
    unit = next(iter(second["contexts"].values()))["units"][0]
    unit["start"] += 1; unit["end"] -= 1; unit["text"] = unit["text"][1:-1]
    merged = merge_public_research([a, second])
    assert len(merged["sources"]) == 1
    assert len(merged["research_tasks"]["suites"]) == 2
    assert all(len(c["units"]) == 1 for c in merged["contexts"].values())
    assert next(iter(merged["contexts"].values()))["units"][0]["text"] == b["materials"][0]["text"]
    second["sources"][0]["astrometry"]["parallax_mas"] = 99
    with pytest.raises(ValueError, match="source snapshot"):
        merge_public_research([a, second])


def test_real_cases_have_substantive_steps_and_independently_checked_quantities():
    root = Path(__file__).parents[1]
    spec = read_json(root / "benchmarks/ctts_herczeg/cases.json")
    basic = read_json(root / "benchmarks/ctts_herczeg/tasks.json")
    tasks = spec["tasks"]
    assert len(tasks) == 12 and sum(len(t["response_fields"]) for t in tasks) == 51
    assert all(len(t["paper_ids"]) >= 2 and len(t["response_fields"]) >= 4 for t in tasks)
    assert {t["leakage_group"] for t in tasks} == {basic["tasks"][0]["leakage_group"]}
    assert all(t["review_status"] == "pending" and t["split"] == "development" for t in tasks)
    refs = {(t["id"], c["id"]): c["answer"] for t in tasks for c in t["reference"]["checkpoints"]}
    # Separate hand-calculated/unit-aware checks: NOT model-performance measurements.
    from astropy import constants as const, units as u
    luminosity = .04154 * 3.828e26 * u.W
    mdot = 1.25 * luminosity * (.93 * 6.957e8 * u.m) / (const.G * (.79 * 1.98847e30 * u.kg))
    expected = mdot.to_value(u.kg / u.s) * 31557600 / 1.98847e30
    assert math.isclose(refs['CTTS-C01', 'accretion_rate'], expected, rel_tol=1e-8)
    assert math.isclose(refs['CTTS-C03', 'elapsed'], 23.76, abs_tol=1e-8)
    assert math.isclose(refs['CTTS-C04', 'log_sigma'], .093425, abs_tol=1e-6)
    assert math.isclose(refs['CTTS-C05', 'radius_updated'], 6.92742658e-8, rel_tol=1e-5)
    assert math.isclose(refs['CTTS-C06', 'required_factor'], 2.277904328, rel_tol=1e-8)
    assert math.isclose(refs['CTTS-C08', 'flux_ratio'], 16.21810097, rel_tol=1e-8)
    assert math.isclose(refs['CTTS-C09', 'weighted_flux'], 170/13, rel_tol=1e-8)
    assert math.isclose(refs['CTTS-C11', 'mean_epoch2'], 8.15e-8, rel_tol=1e-8)
    assert math.isclose(refs['CTTS-C12', 'transferred_ratio'], .2683858644, rel_tol=1e-8)
