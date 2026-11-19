"""Scientific-corpus lifecycle, evidence integrity and revision concurrency."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json

import pytest
from pydantic import ValidationError

from astro_pipeline import mcp_server
from astro_pipeline.library.research import (
    export_research_corpus, prepare_research_workspace, query_research_items,
    record_research_item, update_research_item,
)
from astro_pipeline.library.store import LibraryStore
from tests.test_paper_evidence import pilot, span, target


def scientific_item(pilot):
    return {"kind": "scientific_record", "title": "Synthetic velocity study", "topic": "velocity",
        "question": "What velocity is reported and to whom is it attributed?", "summary": "A synthetic contract fixture, not astronomy data.",
        "targets": [{**target(), "name": "TWA 1", "identity_basis": "Synthetic fixture identity"}],
        "evidence": [{"id": "ev1", "kind": "paper", "paper_version_id": pilot[1],
                      "span": span(pilot, "result", "For TWA 1, we report a radial velocity of 13.2 ± 0.3 km/s.")}],
        "units": [{"id": "result1", "kind": "result", "statement": "The synthetic authors report 13.2 ± 0.3 km/s.",
                   "origin": "paper_report", "attribution": "Synthetic authors", "target_keys": ["dr3:111"], "evidence_ids": ["ev1"],
                   "measurement": {"quantity": "radial_velocity", "value": 13.2, "unit": "km/s", "reported_text": "13.2 ± 0.3 km/s", "uncertainty": "±0.3 km/s"}}],
        "author": {"name": "test-fixture", "version": "1", "prompt_version": "scientific-record-1"}}


def task_item(pilot, revision):
    item = scientific_item(pilot)
    item.update(kind="research_task", title="Compare observations", evidence=[], units=[], related_revision_ids=[revision],
                task={"objective": "Compare velocities across epochs", "procedure": ["Locate the observing dates", "Compare compatible measurements"],
                      "expected_outputs": ["An attributed comparison with uncertainty and evidence"]})
    return item


def test_scientific_record_and_open_task_roundtrip_and_workspace(pilot):
    store = pilot[0]
    result = record_research_item(store, scientific_item(pilot), "first")
    r = result["record"]
    task = record_research_item(store, task_item(pilot, r["revision_id"]), "task")["record"]
    assert r["review_status"] == "pending" and not r["scientifically_verified"]
    assert "source_path" not in r["evidence"][0]["span"]
    assert "document_path" not in r["evidence"][0]["paper"]
    assert task["item"]["task"]["status"] == "planned" and "reference" not in task["item"]
    assert query_research_items(store, source_id="111")["page"]["total"] == 2
    assert not query_research_items(store, source_id="111", release="dr4")["items"]
    assert query_research_items(store, kind="scientific_record", text="velocity", topic="velocity")["page"]["total"] == 1
    assert query_research_items(store, paper_version_id=pilot[1])["page"]["total"] == 1
    prepared = prepare_research_workspace(store, [target()], "velocity")
    papers = prepared["targets"][0]["papers"]
    assert any(v["paper_version_id"] == pilot[1] for p in papers for v in p["fulltext_versions"])
    assert len(prepared["targets"][0]["existing_records"]) == 2
    assert "Benchmark" not in prepared["workflow"]  # Instructions say not to silently convert to tests.
    assert store.read("PRAGMA foreign_key_check") == []


def test_revisions_preserve_history_and_retries_do_not_change_heads(pilot):
    store = pilot[0]; item = scientific_item(pilot)
    first = record_research_item(store, item, "r1")["record"]
    changed = deepcopy(item); changed["summary"] = "Corrected attribution detail"
    second = update_research_item(store, first["record_id"], first["revision_id"], changed, "r2", "Correct detail")["record"]
    assert second["ordinal"] == 2 and second["parent_revision_id"] == first["revision_id"]
    assert record_research_item(store, item, "r1")["created"] is False
    assert update_research_item(store, first["record_id"], first["revision_id"], changed, "r2", "Correct detail")["created"] is False
    assert query_research_items(store, record_id=first["record_id"])["items"][0]["revision_id"] == second["revision_id"]
    assert len(query_research_items(store, record_id=first["record_id"], include_history=True)["items"]) == 2
    with pytest.raises(ValueError, match="conflict"):
        update_research_item(store, first["record_id"], first["revision_id"], item, "r3", "Stale write")
    with pytest.raises(ValueError, match="different write"):
        record_research_item(store, changed, "r1")


def test_concurrent_updates_have_one_winner(pilot):
    store = pilot[0]; item = scientific_item(pilot)
    first = record_research_item(store, item, "start")["record"]
    def update(index):
        try:
            return update_research_item(store, first["record_id"], first["revision_id"], item, f"u{index}", str(index))["created"]
        except ValueError as error:
            assert "conflict" in str(error)
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update, [1, 2])) == [False, True]
    assert len(query_research_items(store, include_history=True)["items"]) == 2


@pytest.mark.parametrize("change", ["quote", "version", "source", "citation", "cycle", "nan", "origin", "reference", "completed"])
def test_invalid_scientific_content_is_not_saved(pilot, change):
    item = scientific_item(pilot)
    if change == "quote": item["evidence"][0]["span"]["quote"] = "Invented result"
    elif change == "version": item["evidence"][0]["paper_version_id"] = "missing"
    elif change == "source": item["targets"][0]["source_id"] = "999"; item["units"][0]["target_keys"] = ["dr3:999"]
    elif change == "citation": item["units"][0]["evidence_ids"] = ["absent"]
    elif change == "cycle": item["units"][0]["depends_on"] = ["result1"]
    elif change == "nan": item["units"][0]["measurement"]["value"] = float("nan")
    elif change == "origin": item["units"][0]["evidence_ids"] = []
    elif change == "reference": item["related_revision_ids"] = ["nonexistent"]
    else: item.update(kind="research_task", units=[], task={"objective": "Compare", "expected_outputs": ["Result"], "status": "completed"})
    with pytest.raises((ValueError, ValidationError)):
        record_research_item(pilot[0], item, "invalid")
    assert query_research_items(pilot[0])["page"]["total"] == 0


def test_catalog_evidence_is_a_completed_release_qualified_occurrence(pilot):
    store = pilot[0]
    corpus = store.register_corpus(store.root / "catalog", "catalog")
    run = store.start_run(corpus)
    store.index_members(run, [{"gaia_release": "dr3", "source_id": "111", "Plx_gdr3": 16.6}])
    item = scientific_item(pilot)
    item["evidence"].append({"id": "gaia", "kind": "catalog", "target": target(), "run_id": run,
                             "member_ordinal": 0, "fields": [{"column": "Plx_gdr3", "unit": "mas"}]})
    item["units"].append({"id": "parallax", "kind": "observation", "statement": "Fixture parallax: 16.6 mas",
                          "origin": "catalog_record", "attribution": "Synthetic catalog", "target_keys": ["dr3:111"], "evidence_ids": ["gaia"]})
    with pytest.raises(ValueError, match="completed run"):
        record_research_item(store, item, "catalog")
    store.finish_run(run, "completed")
    r = record_research_item(store, item, "catalog")["record"]
    assert r["evidence"][1]["fields"][0]["value"] == 16.6
    assert r["evidence"][1]["member_ordinal"] == 0
    wrong = deepcopy(item); wrong["units"][-1]["origin"] = "paper_report"
    with pytest.raises(ValueError, match="paper evidence"): record_research_item(store, wrong, "wrong-origin")
    wrong = deepcopy(item); wrong["units"][0]["origin"] = "catalog_record"
    with pytest.raises(ValueError, match="catalog evidence"): record_research_item(store, wrong, "wrong-catalog")
    item["evidence"][1]["target"]["release"] = "dr4"
    with pytest.raises(ValueError): record_research_item(store, item, "other-release")


def test_export_freezes_input_revisions_including_dependencies(pilot, tmp_path):
    store = pilot[0]; item = scientific_item(pilot)
    first = record_research_item(store, item, "base")["record"]
    task = record_research_item(store, task_item(pilot, first["revision_id"]), "task")["record"]
    changed = deepcopy(item); changed["summary"] = "Newer interpretation"
    newer = update_research_item(store, first["record_id"], first["revision_id"], changed, "new", "Revision")["record"]
    out = export_research_corpus(store, "selected", output_root=tmp_path, record_ids=[task["record_id"]])
    assert out["selected_revision_ids"] == [task["revision_id"]]
    assert out["dependency_revision_ids"] == [first["revision_id"]]
    assert newer["revision_id"] not in out["dependency_revision_ids"]
    from pathlib import Path
    for name, sha in out["files"].items():
        assert hashlib.sha256(Path(out["paths"][name]).read_bytes()).hexdigest() == sha
    corpus = json.loads(Path(out["paths"]["corpus.json"]).read_text(encoding="utf-8"))
    assert len(corpus["records"]) == 2
    again = export_research_corpus(store, "selected", output_root=tmp_path, revision_ids=[first["revision_id"]])
    assert out["directory"] != again["directory"]
    with pytest.raises(ValueError): export_research_corpus(store, "../outside", output_root=tmp_path, record_ids=[first["record_id"]])


def test_read_only_absent_and_schema3_migration(pilot, tmp_path):
    absent = LibraryStore(tmp_path / "absent")
    assert query_research_items(absent)["items"] == []
    assert prepare_research_workspace(absent, [target()], "velocity")["targets"][0]["source_in_library"] is False
    assert not absent.root.exists()
    store = pilot[0]
    with store.transaction() as connection:
        for table in ("research_targets", "research_revisions", "research_items"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version=3")
    assert not query_research_items(store)["schema_ready"]
    assert store.status()["schema"] == 3
    record_research_item(store, scientific_item(pilot), "migrate")
    assert store.status()["schema"] == 4 and store.status()["n_research_items"] == 1


def test_mcp_tools_expose_source_led_workflow_and_write_contract(pilot, monkeypatch, tmp_path):
    import astro_pipeline.library
    monkeypatch.setattr(astro_pipeline.library, "get_library", lambda *args, **kwargs: pilot[0])
    tools = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert {"prepare_research_workspace", "record_research_item", "query_research_items", "update_research_item", "export_research_corpus"} <= tools.keys()
    schema = tools["record_research_item"].inputSchema
    assert "ResearchItem" in schema["$defs"]
    written = mcp_server.record_research_item(scientific_item(pilot), "mcp-write")
    assert written["created"]
    assert mcp_server.query_research_items(record_id=written["record"]["record_id"])["page"]["total"] == 1
    assert "error" in mcp_server.update_research_item(written["record"]["record_id"], "stale", scientific_item(pilot), "update", "Try")
