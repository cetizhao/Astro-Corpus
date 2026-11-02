from copy import deepcopy
import asyncio
import json

import pytest
from pydantic import ValidationError

from astro_pipeline import mcp_server
from astro_pipeline.library.evidence import record_paper_annotations as record, query_source_evidence as query
from astro_pipeline.library.identity import identity_hash
from tests.test_paper_evidence import pilot, batch, span


def membership_batch(pilot, *, origin="current_analysis"):
    request = batch(pilot, "membership-v2")
    request["ontology_version"] = "object-paper-2"
    evidence = span(pilot, "membership", "candidate member of TWA, conditional on a future systemic velocity measurement.")
    a = request["annotations"][0]
    a["inspected_block_ids"].append(evidence["block_id"])
    if origin == "prior_work":
        a["relations"].append({"role": "cites_prior_result", "statement": "Synthetic attribution example.", "evidence": [evidence]})
    a["results"] = [{"kind": "membership", "association": "TWA", "status": "candidate",
                     "reported_label": "candidate member", "origin": origin,
                     "attribution": "Synthetic author, not a real scientific claim",
                     "method": "Synthetic membership judgement", "qualification": "Needs systemic velocity",
                     "evidence": [evidence]}]
    return request


def test_old_payload_hash_and_retry_survive_v2(pilot):
    request = batch(pilot)
    record(pilot[0], request)
    row = pilot[0].read("SELECT request_json,payload_hash FROM paper_annotation_batches")[0]
    saved = json.loads(row["request_json"])
    assert "results" not in saved["annotations"][0]
    assert row["payload_hash"] == identity_hash(saved, namespace="object-paper-1")
    assert record(pilot[0], saved)["created"] is False


def test_membership_history_filter_and_exact_unicode_evidence(pilot):
    store = pilot[0]
    record(store, batch(pilot))
    request = membership_batch(pilot)
    record(store, request)
    prior = membership_batch(pilot, origin="prior_work")
    prior["batch_id"] = "prior-claim"
    record(store, prior)
    found = query(store, "111", membership_status="candidate")
    assert found["ontology_version"] == "object-paper-1"
    assert all(a["annotation"]["ontology_version"] == "object-paper-2" for a in found["assessments"])
    assert found["page"]["total"] == 2
    assert {a["annotation"]["results"][0]["origin"] for a in found["assessments"]} == {"prior_work", "current_analysis"}
    for item in found["assessments"]:
        result = item["annotation"]["results"][0]
        assert result["evidence"][0]["locator"]["html_id"] == "membership"
        assert item["review_status"] == "pending"
    assert query(store, "111")["page"]["total"] == 3
    empty = query(store, "111", membership_status="non_member")
    assert empty["assessment_status"] == "recorded" and empty["page"]["total"] == 0
    assert not query(store, "111", release="dr4", membership_status="candidate")["assessments"]


@pytest.mark.parametrize("case", ["v1", "bad_quote", "no_role", "invented_label", "bad_offset"])
def test_invalid_membership_batch_is_atomic(pilot, case):
    request = membership_batch(pilot)
    a = request["annotations"][0]; result = a["results"][0]
    if case == "v1": request["ontology_version"] = "object-paper-1"
    if case == "bad_quote": result["evidence"][0]["quote"] += " invented"
    if case == "no_role": result["origin"] = "prior_work"
    if case == "invented_label": result["reported_label"] = "confirmed member"
    if case == "bad_offset": result["evidence"][0]["start"] += 1
    with pytest.raises((ValueError, ValidationError)):
        record(pilot[0], request)
    assert query(pilot[0], "111")["page"]["total"] == 0


def test_v2_mcp_deserialization_and_filter(pilot, monkeypatch):
    import astro_pipeline.library
    monkeypatch.setattr(astro_pipeline.library, "get_library", lambda **kwargs: pilot[0])
    asyncio.run(mcp_server.mcp.call_tool("record_paper_annotations", {"batch": membership_batch(pilot)}))
    assert mcp_server.query_source_evidence("111", membership_status="candidate")["page"]["total"] == 1
