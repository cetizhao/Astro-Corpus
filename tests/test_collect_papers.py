"""Reproducible pilot selection and offline report CLI tests."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import uuid

import pandas as pd
import pytest

from astro_pipeline.library.files import sha256_file


def _script():
    path = Path(__file__).parents[1] / "scripts" / "collect_papers.py"
    spec = importlib.util.spec_from_file_location("collect_papers_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_selection_is_deterministic_and_preserves_original_occurrences(tmp_path):
    path = tmp_path / "stars" / "runs" / str(uuid.uuid4()) / "simbad_refs.parquet"
    path.parent.mkdir(parents=True)
    rows = [{"arxiv_id": f"2305.{i:05d}", "bibcode": f"bib{i}",
             "title": ["A stellar census", "Disk accretion", "A spectroscopic model"][i % 3],
             "year": [2005, 2015, 2023][i % 3], "doi": None}
            for i in range(1, 26)]
    rows.append(dict(rows[0], bibcode="second-alias"))
    pd.DataFrame(rows).to_parquet(path)
    module = _script()
    first = module.select_papers([path], 25)
    second = module.select_papers([path], 25)
    assert first["papers"] == second["papers"]
    assert len({p["arxiv_id"] for p in first["papers"]}) == 25
    assert first["source_snapshots"][0]["sha256"] == sha256_file(path)
    full = module.select_papers([path], 26)
    duplicated = next(p for p in full["papers"] if p["arxiv_id"] == "2305.00001")
    assert duplicated["provenance"][0]["row_ordinals"] == [0, 25]
    assert {a["bibcode"] for a in duplicated["aliases"]} == {"bib1", "second-alias"}
    with pytest.raises(ValueError, match="20 and 50"):
        module.select_papers([path], 51)
    with pytest.raises(ValueError, match="pinned"):
        module.select_papers([tmp_path / "mutable.parquet"], 20)


def test_audit_verifies_content_and_does_not_certify_science(tmp_path):
    artifact = tmp_path / "paper.json"
    artifact.write_text(json.dumps({"parser": {"format": "html"},
                                    "quality": {"status": "partial", "warnings": ["test"]},
                                    "blocks": [{"type": "paragraph", "text": "0.8 ± 0.1"}]}))
    manifest = {"run_id": "test", "papers": [{"ordinal": 0, "work_id": "2305.00001", "status": "partial",
                                                "license": None, "artifacts": [{"kind": "json", "path": str(artifact),
                                                                                "sha256": sha256_file(artifact)}]}]}
    module = _script()
    good = module.audit_collection(manifest)
    assert good["verified_artifacts"] == 1
    assert good["known_licenses"] == 0
    assert good["parser_formats"] == {"html": 1}
    assert "not established" in good["scientific_fidelity"]
    artifact.write_text("changed")
    bad = module.audit_collection(manifest)
    assert bad["integrity_failures"] == 1
    assert bad["verified_artifacts"] == 0
