"""Offline tests for fixed-region collection readiness and reporting."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys

import pandas as pd
import pytest

from astro_pipeline import membership
from astro_pipeline.corpus_builder import CorpusBuildResult


def _load_collect_regions():
    path = Path(__file__).resolve().parents[1] / "scripts" / "collect_regions.py"
    spec = importlib.util.spec_from_file_location("collect_regions_test_module", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_stage_options_are_cumulative():
    collect = _load_collect_regions()

    assert collect._stage_options("members") == {
        "xp_sampled": False,
        "xp_continuous": False,
        "skip_simbad": True,
        "enrich": False,
    }
    assert collect._stage_options("continuous") == {
        "xp_sampled": True,
        "xp_continuous": True,
        "skip_simbad": True,
        "enrich": False,
    }
    assert collect._stage_options("papers") == {
        "xp_sampled": True,
        "xp_continuous": True,
        "skip_simbad": False,
        "enrich": False,
    }
    assert collect._stage_options("all") == {
        "xp_sampled": True,
        "xp_continuous": True,
        "skip_simbad": False,
        "enrich": True,
    }


def test_job_identity_keeps_smoke_and_full_labels_separate():
    collect = _load_collect_regions()
    preset = collect.REGIONS["uppersco"]

    assert collect._job_identity(preset, full=False, smoke_limit=10) == (
        "uppersco_luhman2020_table5_smoke",
        10,
    )
    assert collect._job_identity(preset, full=True, smoke_limit=10) == (
        "uppersco_luhman2020_table5_full",
        None,
    )


def test_runner_forwards_fixed_preset_and_never_refreshes(
    monkeypatch, tmp_path, capsys
):
    collect = _load_collect_regions()
    calls = []

    def fake_build(*, timeout_seconds, **kwargs):
        assert timeout_seconds == 3600
        calls.append(kwargs)
        return {"run_id": "run-1"}

    def fake_report(**kwargs):
        return {
            "target": kwargs["preset"].key,
            "label": kwargs["label"],
            "completion": "completed",
        }

    monkeypatch.setattr(collect, "_bounded_build", fake_build)
    monkeypatch.setattr(collect, "_build_report", fake_report)

    code = collect.main(
        [
            "--target",
            "cra",
            "--stage",
            "continuous",
            "--smoke-limit",
            "7",
            "--retry-passes",
            "0",
            "--data-root",
            str(tmp_path),
        ]
    )

    assert code == 0
    assert len(calls) == 1
    call = calls[0]
    assert call["label"] == "cra_esplin2022_table1_smoke"
    assert call["catalog_id"] == "J/AJ/163/64"
    assert call["table_id"] == "J/AJ/163/64/table1"
    assert call["limit"] == 7
    assert call["match_radius_arcsec"] == 3.0
    assert call["xp_sampled"] is True
    assert call["xp_continuous"] is True
    assert call["skip_simbad"] is True
    assert call["refresh"] is False
    assert call["resume_selection_run_id"] is None
    assert call["static_viz_mode"] == "status_only"
    assert "collection_report" in capsys.readouterr().out


def test_runner_requires_single_target_for_explicit_selection_resume(monkeypatch, tmp_path):
    collect = _load_collect_regions()
    calls = []

    def fake_build(*, timeout_seconds, **kwargs):
        calls.append(kwargs)
        return {"run_id": "new-run"}

    monkeypatch.setattr(collect, "_bounded_build", fake_build)
    monkeypatch.setattr(collect, "_build_report", lambda **kwargs: {"completion": "completed"})
    with pytest.raises(SystemExit, match="2"):
        collect.main(["--resume-selection-run-id", "source-run"])
    assert not calls

    assert collect.main([
        "--target", "uppersco", "--full", "--retry-passes", "0",
        "--resume-selection-run-id", "source-run", "--data-root", str(tmp_path),
    ]) == 0
    assert len(calls) == 1
    assert calls[0]["resume_selection_run_id"] == "source-run"
    assert calls[0]["label"] == "uppersco_luhman2020_table5_full"
    assert calls[0]["limit"] is None
    assert calls[0]["refresh"] is False


def test_build_attempt_has_process_deadline(monkeypatch):
    collect = _load_collect_regions()

    def stalled(command, **kwargs):
        assert kwargs["timeout"] == 7
        assert kwargs["check"] is True
        raise collect.subprocess.TimeoutExpired(command, 7)

    monkeypatch.setattr(collect.subprocess, "run", stalled)
    with pytest.raises(collect.subprocess.TimeoutExpired):
        collect._bounded_build(timeout_seconds=7, label="fixture")


def test_report_uses_actual_artifacts_for_counts_and_pending_ids(
    monkeypatch, tmp_path
):
    collect = _load_collect_regions()
    corpus = tmp_path / "cra_esplin2022_table1_smoke"
    (corpus / "catalogs").mkdir(parents=True)

    original = pd.DataFrame(
        {"membership_catalog_row": [0, 1, 2], "Name": ["a", "b", "c"]}
    )
    members = pd.DataFrame(
        {
            "Gaia": ["101", "102"],
            "membership_catalog_row": [0, 1],
        }
    )
    unmatched = pd.DataFrame({"membership_catalog_row": [2], "Name": ["c"]})
    availability = pd.DataFrame(
        {
            "source_id": ["101", "102"],
            "has_xp_sampled": [True, True],
            "has_xp_continuous": [False, True],
        }
    )
    refs = pd.DataFrame(
        {
            "gaia_id": ["101"],
            "bibcode": ["2020AJ....1A"],
            "title": ["A paper"],
            "doi": ["10.1/example"],
            "arxiv_id": [""],
            "year": [2020],
        }
    )
    original_path = corpus / "membership_catalog.parquet"
    members_path = corpus / "members.parquet"
    unmatched_path = corpus / "unmatched_members.csv"
    availability_path = corpus / "catalogs" / "gaia_xp_availability.parquet"
    original.to_parquet(original_path, index=False)
    members.to_parquet(members_path, index=False)
    unmatched.to_csv(unmatched_path, index=False)
    availability.to_parquet(availability_path, index=False)
    refs.to_parquet(corpus / "simbad_refs.parquet", index=False)
    (corpus / "simbad_refs.checkpoint.json").write_text(
        '{"completed": ["101", "102"], "failed": {}}',
        encoding="utf-8",
    )
    (corpus / "manifest.json").write_text(
        """{
          "status": "completed",
          "active_run_id": "run-actual",
          "stages": {
            "resolve_members": {"status": "completed"},
            "save_members": {"status": "completed"},
            "simbad_papers": {"status": "completed"}
          }
        }""",
        encoding="utf-8",
    )

    def fake_resolve(directory, identifier, retrieval_type, release):
        if retrieval_type == "XP_SAMPLED" and identifier == "102":
            raise FileNotFoundError(identifier)
        return {"path": corpus / f"{identifier}.fits"}

    monkeypatch.setattr(collect, "resolve_product", fake_resolve)
    report = collect._build_report(
        preset=collect.REGIONS["cra"],
        label="cra_esplin2022_table1_smoke",
        full=False,
        limit=3,
        stage="papers",
        corpus_dir=corpus,
        result={
            "manifest_json": str(corpus / "manifest.json"),
            "membership_catalog_parquet": str(original_path),
            "members_parquet": str(members_path),
            "unmatched_members_csv": str(unmatched_path),
            "xp_products": {"availability_path": str(availability_path)},
        },
        error=None,
    )

    assert report["run_id"] == "run-actual"
    assert report["counts"] == {
        "original_rows": 3,
        "matched_rows": 2,
        "unmatched_rows": 1,
        "unique_gaia_rows": 2,
        "duplicate_matched_rows": 0,
        "row_reconciled": True,
        "full_table_complete": None,
    }
    assert report["xp"]["sampled"]["available_ids"] == ["101", "102"]
    assert report["xp"]["sampled"]["ready_ids"] == ["101"]
    assert report["xp"]["sampled"]["pending_ids"] == ["102"]
    assert report["xp"]["continuous"]["ready_ids"] == ["102"]
    assert report["simbad"]["completed_count"] == 2
    assert report["simbad"]["zero_reference_ids"] == ["102"]
    assert report["enrichment"] == {
        "requested": False,
        "metadata_known": True,
        "unique_bibcodes": 1,
        "with_title": 1,
        "with_doi": 1,
        "with_arxiv": 0,
        "with_year": 1,
    }
    assert report["completion"] == "partial"


def _write_report_inventory(directory, source_ids):
    (directory / "catalogs").mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"membership_catalog_row": range(len(source_ids))}).to_parquet(
        directory / "membership_catalog.parquet", index=False,
    )
    pd.DataFrame({"Gaia": source_ids}).to_parquet(directory / "members.parquet", index=False)
    pd.DataFrame({"membership_catalog_row": pd.Series(dtype=int)}).to_csv(
        directory / "unmatched_members.csv", index=False,
    )
    pd.DataFrame({
        "source_id": source_ids,
        "has_xp_sampled": [True] * len(source_ids),
        "has_xp_continuous": [False] * len(source_ids),
    }).to_parquet(directory / "catalogs" / "gaia_xp_availability.parquet", index=False)
    (directory / "simbad_refs.checkpoint.json").write_text(json.dumps({
        "completed": source_ids[:1], "failed": {},
    }))


def test_failed_attempt_reports_its_run_not_older_published_inventory(monkeypatch, tmp_path):
    collect = _load_collect_regions()
    corpus = tmp_path / "corpus"
    _write_report_inventory(corpus, ["101"])
    attempted = corpus / "runs" / "attempted-run"
    _write_report_inventory(attempted, ["201", "202"])
    (corpus / "manifest.json").write_text(json.dumps({
        "storage": {"kind": "shared_library"},
        "active_run_id": "older-run", "current_run_id": "attempted-run",
        "status": "running",
        "stages": {
            "resolve_members": {"status": "completed"},
            "save_members": {"status": "completed"},
        },
    }))

    def fake_resolve(directory, identifier, retrieval_type, release):
        assert directory == attempted
        assert release == "dr3"
        if identifier == "202":
            raise FileNotFoundError(identifier)
        return {"path": directory / f"{identifier}.fits"}

    monkeypatch.setattr(collect, "resolve_product", fake_resolve)
    report = collect._build_report(
        preset=collect.REGIONS["cra"], label="fixture", full=False, limit=2,
        stage="papers", corpus_dir=corpus, result=None, error="child failed",
        previous_run_id="older-run",
    )
    assert report["completion"] == "failed"
    assert report["run_id"] == "attempted-run"
    assert report["active_run_id"] == "older-run"
    assert report["counts"]["matched_rows"] == 2
    assert report["xp"]["sampled"]["availability_known"] is True
    assert report["xp"]["sampled"]["ready_ids"] == ["201"]
    assert report["xp"]["sampled"]["pending_ids"] == ["202"]
    assert report["simbad"]["completed_ids"] == ["201"]
    assert report["simbad"]["pending_ids"] == ["202"]


def test_report_pins_successful_result_and_ignores_mutable_root_paths(monkeypatch, tmp_path):
    collect = _load_collect_regions()
    corpus = tmp_path / "corpus"
    _write_report_inventory(corpus, ["999"])
    selected = corpus / "runs" / "result-run"
    _write_report_inventory(selected, ["201", "202"])
    (corpus / "manifest.json").write_text(json.dumps({
        "storage": {"kind": "shared_library"},
        "active_run_id": "later-run", "current_run_id": "later-run", "status": "running",
    }))
    (selected / "manifest.json").write_text(json.dumps({
        "storage": {"kind": "shared_library"},
        "snapshot_run_id": "result-run", "status": "completed",
        "stages": {
            "resolve_members": {"status": "completed"},
            "save_members": {"status": "completed"},
        },
    }))
    seen = []

    def fake_resolve(directory, identifier, retrieval_type, release):
        seen.append(directory)
        assert directory == selected
        return {"path": directory / f"{identifier}.fits"}

    monkeypatch.setattr(collect, "resolve_product", fake_resolve)
    report = collect._build_report(
        preset=collect.REGIONS["cra"], label="fixture", full=False, limit=2,
        stage="sampled", corpus_dir=corpus, error=None,
        result={
            "run_id": "result-run", "manifest_json": str(corpus / "manifest.json"),
            "members_parquet": str(corpus / "members.parquet"),
            "membership_catalog_parquet": str(corpus / "membership_catalog.parquet"),
            "unmatched_members_csv": str(corpus / "unmatched_members.csv"),
            "xp_products": {"availability_path": str(corpus / "catalogs" / "gaia_xp_availability.parquet")},
        },
    )
    assert seen
    assert report["completion"] == "completed"
    assert report["run_id"] == "result-run"
    assert report["active_run_id"] == "later-run"
    assert report["manifest_path"] == str(selected / "manifest.json")
    assert report["counts"]["matched_rows"] == 2
    assert report["xp"]["sampled"]["ready_ids"] == ["201", "202"]


def test_failed_attempt_without_new_run_does_not_reuse_previous_inventory(tmp_path):
    collect = _load_collect_regions()
    corpus = tmp_path / "corpus"
    _write_report_inventory(corpus, ["101"])
    (corpus / "manifest.json").write_text(json.dumps({
        "storage": {"kind": "shared_library"},
        "active_run_id": "older-run", "current_run_id": "older-run", "status": "completed",
    }))
    report = collect._build_report(
        preset=collect.REGIONS["cra"], label="fixture", full=False, limit=2,
        stage="papers", corpus_dir=corpus, result=None, error="preflight failed",
        previous_run_id="older-run",
    )
    assert report["completion"] == "failed"
    assert report["run_id"] == ""
    assert report["active_run_id"] == "older-run"
    assert report["counts"]["matched_rows"] is None
    assert report["xp"]["sampled"]["availability_known"] is False
    assert report["simbad"]["checkpoint_known"] is False


def test_enrichment_completion_distinguishes_negative_observations_from_throttling(tmp_path):
    collect = _load_collect_regions()
    coverage = {"metadata_known": True, "unique_bibcodes": 1, "with_title": 1}
    observation = {
        "schema_version": 1, "run_id": "run-1", "status": "completed",
        "metadata": {"status": "completed", "total": 1, "completed": 1, "failed": {}},
        "doi": {
            "status": "completed", "total": 1, "completed": 1, "found": 0,
            "not_found": 1, "failed": {}, "deferred": [], "rate_limited": False,
        },
    }
    path = tmp_path / "simbad_refs.enrichment.json"
    missing = collect._enrichment_status_report(tmp_path, run_id="run-1", requested=True)
    assert not collect._enrichment_complete(coverage, missing)
    path.write_text(json.dumps(observation))
    actual = collect._enrichment_status_report(tmp_path, run_id="run-1", requested=True)
    assert collect._enrichment_complete(coverage, actual)
    stale = collect._enrichment_status_report(tmp_path, run_id="run-2", requested=True)
    assert not collect._enrichment_complete(coverage, stale)
    actual["doi"].update(status="rate_limited", completed=0, not_found=0,
                         failed={"10.1/a": "HTTP 429"}, rate_limited=True)
    actual["status"] = "partial"
    assert not collect._enrichment_complete(coverage, actual)


def test_report_does_not_call_arxiv_rate_limited_enrichment_complete(monkeypatch, tmp_path):
    collect = _load_collect_regions()
    _write_report_inventory(tmp_path, ["101"])
    pd.DataFrame({
        "gaia_id": ["101"], "bibcode": ["2020fixture"], "title": ["A title"],
        "doi": ["10.1/a"], "arxiv_id": [""], "year": [2020],
    }).to_parquet(tmp_path / "simbad_refs.parquet", index=False)
    (tmp_path / "manifest.json").write_text(json.dumps({
        "status": "completed", "active_run_id": "run-1", "stages": {
            name: {"status": "completed"}
            for name in ("resolve_members", "save_members", "simbad_papers")
        },
    }))
    (tmp_path / "simbad_refs.enrichment.json").write_text(json.dumps({
        "schema_version": 1, "run_id": "run-1", "status": "partial",
        "metadata": {"status": "completed", "total": 1, "completed": 1, "failed": {}},
        "doi": {
            "status": "rate_limited", "total": 1, "completed": 0,
            "found": 0, "not_found": 0, "failed": {},
            "deferred": ["10.1/a"], "rate_limited": True,
        },
    }))
    monkeypatch.setattr(collect, "resolve_product", lambda *args, **kwargs: {})
    report = collect._build_report(
        preset=collect.REGIONS["cra"], label="fixture", full=False, limit=1,
        stage="all", corpus_dir=tmp_path, result=None, error=None,
    )
    assert report["enrichment"]["with_title"] == 1
    assert report["simbad"]["pending_count"] == 0
    assert report["completion"] == "partial"
    assert report["enrichment_observation"]["doi"]["deferred"] == ["10.1/a"]


def test_match_audit_keeps_duplicate_occurrences_and_ambiguous_rows():
    collect = _load_collect_regions()
    report = collect._match_audit(
        pd.DataFrame({
            "Gaia": ["101", "101", "102"],
            "membership_match_sep_arcsec": [0.1, 1.2, 2.9],
            "membership_match_candidate_count": [1, 2, 3],
        }),
        pd.DataFrame({"membership_match_status": ["no_gaia_match", "invalid_coordinates"]}),
    )
    assert report["multiple_candidate_rows"] == 2
    assert report["separation_over_1_arcsec_rows"] == 2
    assert report["maximum_separation_arcsec"] == 2.9
    assert report["duplicate_source_occurrences"] == {"101": 2}
    assert report["unmatched_reasons"] == {"no_gaia_match": 1, "invalid_coordinates": 1}
    assert collect._match_audit(None, None) == {"known": False}


def test_membership_builder_forwards_static_viz_mode(monkeypatch, tmp_path):
    import astro_pipeline.corpus_builder as corpus_builder
    import astro_pipeline.library as library

    original = pd.DataFrame(
        {"membership_catalog_row": [0], "RAJ2000": [68.0], "DEJ2000": [22.0]}
    )
    members = pd.DataFrame(
        {"Gaia": ["123"], "membership_catalog_row": [0], "RAICRS": [68.0]}
    )
    resolution = membership.MembershipResolution(
        catalog_id="J/AJ/1/1",
        table_id="J/AJ/1/1/table1",
        table_description="Members",
        catalog_title="A catalogue",
        original=original,
        members=members,
        unmatched=original.iloc[0:0].copy(),
        match_method="gaia_dr3_position",
        match_radius_arcsec=3.0,
        all_rows=original,
        coordinate_descriptor={
            "ra_column": "RAJ2000",
            "dec_column": "DEJ2000",
            "ra_unit": "deg",
            "dec_unit": "deg",
        },
    )
    captured = {}

    monkeypatch.setattr(library, "get_library", lambda: None)
    monkeypatch.setattr(
        membership,
        "resolve_membership_catalog",
        lambda *args, **kwargs: resolution,
    )

    def fake_build(**kwargs):
        captured.update(kwargs)
        return CorpusBuildResult(
            label=kwargs["label"],
            input_mode="membership_catalog",
            input_summary=kwargs["input_summary"],
            n_members=1,
            n_paper_edges=0,
            n_unique_papers=0,
            out_dir=str(kwargs["out_dir"]),
        )

    monkeypatch.setattr(corpus_builder, "build_corpus_from_members", fake_build)
    output = membership.build_membership_corpus(
        label="static_mode_test",
        out_dir=tmp_path / "static_mode_test",
        catalog_id="J/AJ/1/1",
        table_id="J/AJ/1/1/table1",
        skip_simbad=False,
        static_viz_mode="off",
    )

    assert captured["static_viz_mode"] == "off"
    manifest = (tmp_path / "static_mode_test" / "manifest.json").read_text(
        encoding="utf-8"
    )
    assert '"visualization": false' in manifest
    assert output["static_viz_mode"] == "off"


@pytest.mark.parametrize("value", ["0", "-1"])
def test_smoke_limit_rejects_nonpositive_values(value):
    collect = _load_collect_regions()
    with pytest.raises(SystemExit):
        collect._parser().parse_args(["--smoke-limit", value])


def test_retry_passes_are_bounded():
    collect = _load_collect_regions()
    with pytest.raises(argparse.ArgumentTypeError):
        collect._retry_count("4")
