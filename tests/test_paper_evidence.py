"""TWA-shaped synthetic contracts, not a scientific classifier benchmark."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import sqlite3

import pytest
from pydantic import ValidationError

from astro_pipeline import mcp_server, paper_text
from astro_pipeline.library import paper_corpus as pc
from astro_pipeline.library.evidence import (
    get_paper_evidence_context as context,
    query_source_evidence as query,
    record_paper_annotations as record,
)
from astro_pipeline.library.files import BlobIntegrityError
from astro_pipeline.library.schema import SCHEMA_VERSION
from astro_pipeline.library.store import LibraryStore
from astro_pipeline.models.evidence import PaperAnnotationBatch
from tests.test_paper_corpus import FakeArxiv

FIXTURE = Path(__file__).parent / "fixtures" / "twa_evidence.html"


class TwaFixture(FakeArxiv):
    def __init__(self):
        super().__init__()
        self.html = FIXTURE.read_bytes()

    def fetch(self, identifier, kind):
        from dataclasses import replace
        fetched = super().fetch(identifier, kind)
        return replace(fetched, data=self.html) if kind == "html" else fetched


@pytest.fixture
def pilot(tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "_parse_document", lambda kind, data, metadata: paper_text.parse_html(data, paper=metadata))
    store = LibraryStore(tmp_path / "library")
    corpus_id = store.register_corpus(tmp_path / "synthetic-twa", "synthetic-twa", provenance_status="test_fixture")
    run_id = store.start_run(corpus_id)
    store.index_members(run_id, [{"gaia_release": release, "source_id": source}
                                for release in ("dr3", "dr4") for source in ("111", "222", "333")])
    store.put_source_references("dr3", "111", [{"bibcode": "synthetic-bib", "title": "Synthetic bibliography"}], main_id="TW Hya")
    result = pc.build_paper_corpus("synthetic-twa-papers", ["2305.03557v1"], store=store,
                                   client=TwaFixture(), output_dir=tmp_path / "data", include_pdf=False)
    version = result["papers"][0]["version_id"]
    doc = context(store, version, mode="all", limit=100)
    return store, version, doc


def target(name="TWA 1", source="111", release="dr3"):
    return {"release": release, "source_id": source,
            "aliases": [{"name": name, "provenance": "Synthetic fixture mapping, not a measured identity"}]}


def span(pilot, html_id, phrase, field="text", **coordinates):
    unit = next(u for u in pilot[2]["units"] if u["locator"].get("html_id") == html_id
                and u["field"] == field and all(u.get(k) == v for k, v in coordinates.items())
                and phrase in u["text"])
    start = unit["start"] + unit["text"].index(phrase)
    return {"block_id": unit["block_id"], "field": field, "start": start,
            "end": start + len(phrase), "quote": phrase, **coordinates}


def batch(pilot, batch_id="fixture-batch"):
    identity = span(pilot, "sample", "TWA 1")
    analysis = span(pilot, "sample", "We fitted its spectrum in this synthetic example.")
    result = span(pilot, "result", "For TWA 1, we report a radial velocity of 13.2 ± 0.3 km/s.")
    return {"batch_id": batch_id, "paper_version_id": pilot[1],
            "extractor": {"name": "synthetic-test", "version": "1", "prompt_version": "twa-evidence-1"},
            "annotations": [{"target": target(), "paper_name": "TWA 1", "subject_scope": "source",
                             "identity_status": "resolved", "identity_basis": "Synthetic fixture mapping",
                             "identity_evidence": [identity], "assessment": "evidence_found",
                             "inspected_block_ids": [identity["block_id"], result["block_id"]],
                             "relations": [
                                 {"role": "in_sample", "statement": "Included in the working sample; no physical membership claim.", "evidence": [identity]},
                                 {"role": "analysed", "statement": "This synthetic paper fits the spectrum.", "evidence": [analysis]},
                                 {"role": "reports_result", "statement": "This synthetic paper reports a radial velocity.", "evidence": [result]},
                             ]}]}


def test_absent_read_and_invalid_write_do_not_create(tmp_path):
    store = LibraryStore(tmp_path / "absent")
    assert not context(store, "missing")["available"]
    assert query(store, "111")["assessment_status"] == "not_assessed"
    assert not query(store, "111", paper_version_id="missing")["fulltext_available"]
    assert query(None, "111")["schema_ready"] is False
    with pytest.raises(ValidationError):
        record(store, {})
    assert not store.root.exists()


def test_context_searches_cells_captions_aliases_and_boundaries(pilot):
    store, version, _ = pilot
    found = context(store, version, targets=[target("TWA 7", "222")], context_blocks=0, limit=100)
    hit = found["candidate_matches"][0]
    assert hit["field"] == "table_cell" and (hit["row"], hit["column"]) == (1, 0)
    assert any(u["field"] == "table_cell" and u["row"] == 0 for u in found["units"])
    assert any(u["field"] == "table_note" for u in found["units"])
    empty = next(u for u in found["units"] if u["field"] == "table_cell" and (u["row"], u["column"]) == (2, 1))
    assert empty["text"] == "" and empty["start"] == empty["end"] == 0
    assert found["paper"]["document_sha256"]
    assert found["paper"]["source"]["sha256"]
    assert found["units"][0]["source_url"].endswith("#table1")
    hits = context(store, version, targets=[target()], context_blocks=0)["candidate_matches"]
    prefix = next(u for u in pilot[2]["units"] if u["locator"].get("html_id") == "prefix")
    assert [h["quote"] for h in hits if h["block_id"] == prefix["block_id"]] == ["TWA 1"]
    # Existing SIMBAD main_id is another explicit candidate, not a semantic edge.
    assert any(h["quote"] == "TW Hya" for h in hits)
    # TW Hya may still match the library alias for source 111; use another source.
    no_hits = context(store, version, targets=[target("TWA 404", "222")], context_blocks=0)
    assert no_hits["units"] == []
    assert no_hits["coverage"]["empty_matches_mean_no_relation"] is False


def test_context_pagination_and_explicit_block_selection(pilot):
    store, version, full = pilot
    units = []
    for offset in range(0, full["page"]["total"], 2):
        page = context(store, version, mode="all", limit=2, offset=offset)
        units.extend(page["units"])
    assert units == full["units"]
    selected = context(store, version, block_ids=[full["units"][0]["block_id"]])
    assert selected["page"]["total"] == 1
    with pytest.raises(ValueError, match="existing blocks"):
        context(store, version, block_ids=["b-does-not-exist"])
    prior = next(u for u in full["units"] if u["locator"].get("html_id") == "prior")
    linked = prior["citations"][0]["linked_block_id"]
    assert context(store, version, block_ids=[linked])["units"][0]["block_type"] == "reference"


def test_multilabel_round_trip_preserves_bibliography_and_pending_review(pilot):
    store, version, _ = pilot
    original = store.read("SELECT * FROM source_papers")
    result = record(store, batch(pilot))
    assert result["created"] and result["review_status"] == "pending"
    rows = query(store, "111", paper_version_id=version)["assessments"]
    assert len(rows) == 1
    entry = rows[0]
    assert entry["paper"]["document_sha256"] == pilot[2]["paper"]["document_sha256"]
    assert {r["role"] for r in entry["annotation"]["relations"]} == {"in_sample", "analysed", "reports_result"}
    assert entry["annotation"]["relations"][2]["evidence"][0]["locator"]["html_id"] == "result"
    assert entry["identity_verified"] is False
    assert store.read("SELECT * FROM source_papers") == original
    assert store.read("PRAGMA foreign_key_check") == []


def test_table_cells_and_notes_are_exact_evidence(pilot):
    identity = span(pilot, "table1", "TWA 7", "table_cell", row=1, column=0)
    role = span(pilot, "table1", "comparison and calibrator", "table_cell", row=1, column=1)
    note = span(pilot, "table1", "The upper limit is not a detection.", "table_note", note_index=0)
    request = batch(pilot)
    item = request["annotations"][0]
    item.update(target=target("TWA 7"), paper_name="TWA 7", identity_evidence=[identity],
                inspected_block_ids=[identity["block_id"]], relations=[
                    {"role": "comparison", "statement": "Synthetic comparison object.", "evidence": [role, note]},
                    {"role": "calibrator", "statement": "Synthetic calibrator.", "evidence": [role]},
                ])
    record(pilot[0], request)
    evidence = query(pilot[0], "111")["assessments"][0]["annotation"]["relations"][0]["evidence"]
    assert evidence[0]["row"] == 1 and evidence[1]["note_index"] == 0


def test_prior_result_and_system_scope_do_not_turn_into_new_individual_result(pilot):
    request = batch(pilot)
    item = request["annotations"][0]
    quote = span(pilot, "prior", "For TWA 2A, we adopt the radial velocity reported by Example et al. (2000); we do not remeasure it.")
    item.update(target=target("TWA 2A"), paper_name="TWA 2A", subject_scope="component",
                identity_evidence=[quote], inspected_block_ids=[quote["block_id"]],
                relations=[{"role": "cites_prior_result", "statement": "Adopted prior result only.", "evidence": [quote]}])
    record(pilot[0], request)
    assert query(pilot[0], "111", role="reports_result")["page"]["total"] == 0
    assert query(pilot[0], "111", role="cites_prior_result")["assessments"][0]["annotation"]["subject_scope"] == "component"
    system = deepcopy(item)
    quote = span(pilot, "system", "TWA 3 is an unresolved system. We model its combined light, not the separate components.")
    system.update(target=target("TWA 3", "333"), paper_name="TWA 3", subject_scope="system",
                  identity_evidence=[quote], inspected_block_ids=[quote["block_id"]],
                  relations=[{"role": "analysed", "statement": "Combined system light only.", "evidence": [quote]}])
    request.update(batch_id="system", annotations=[system])
    record(pilot[0], request)
    assert query(pilot[0], "333")["assessments"][0]["annotation"]["subject_scope"] == "system"


@pytest.mark.parametrize("assessment", ["uncertain", "not_found_in_inspected_blocks"])
def test_coverage_is_not_a_false_negative_or_global_claim(pilot, assessment):
    request = batch(pilot)
    item = request["annotations"][0]
    item.update(assessment=assessment, relations=[], identity_evidence=[], note="Only these blocks were inspected; no complete-paper absence claim.")
    if assessment == "uncertain":
        item["identity_status"] = "ambiguous"
    record(pilot[0], request)
    result = query(pilot[0], "111", assessment=assessment)
    assert result["assessment_status"] == "recorded"
    assert result["assessments"][0]["annotation"]["assessment"] == assessment
    assert query(pilot[0], "222")["assessment_status"] == "not_assessed"
    filtered = query(pilot[0], "111", role="analysed")
    assert filtered["page"]["total"] == 0 and filtered["assessment_status"] == "recorded"


def test_idempotent_retries_revisions_and_duplicate_occurrences(pilot):
    store = pilot[0]
    request = batch(pilot)
    request["annotations"].append(deepcopy(request["annotations"][0]))
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: record(store, request), range(2)))
    assert sorted(r["created"] for r in replies) == [False, True]
    assert replies[0]["annotation_ids"] == replies[1]["annotation_ids"]
    assert query(store, "111")["page"]["total"] == 2
    revised = deepcopy(request)
    revised["annotations"][0]["note"] = "Second interpretation retained separately."
    with pytest.raises(ValueError, match="different payload"):
        record(store, revised)
    revised["batch_id"] = "revision"
    record(store, revised)
    assert query(store, "111", limit=1)["page"] == {"limit": 1, "offset": 0, "total": 4, "returned": 1, "has_more": True}
    assert query(store, "111", offset=3)["page"]["returned"] == 1


@pytest.mark.parametrize("fault", ["quote", "offset", "block", "uninspected", "cell", "name", "prefix", "prefix_slice", "source"])
def test_invalid_batch_is_atomic(pilot, fault):
    request = batch(pilot)
    invalid = deepcopy(request["annotations"][0])
    request["annotations"].append(invalid)
    evidence = invalid["relations"][0]["evidence"][0]
    if fault == "quote":
        evidence["quote"] = "invented quotation"
    elif fault == "offset":
        evidence["start"] += 1
    elif fault == "block":
        evidence["block_id"] = "missing"
    elif fault == "uninspected":
        invalid["inspected_block_ids"] = [invalid["inspected_block_ids"][1]]
    elif fault == "cell":
        evidence.update(field="table_cell", row=99, column=0)
    elif fault == "name":
        invalid["paper_name"] = "not a declared alias"
    elif fault == "prefix":
        invalid["identity_evidence"] = [span(pilot, "prefix", "TWA 11A")]
        invalid["inspected_block_ids"].append(invalid["identity_evidence"][0]["block_id"])
    elif fault == "prefix_slice":
        invalid["identity_evidence"] = [span(pilot, "prefix", "TWA 1")]
        invalid["inspected_block_ids"].append(invalid["identity_evidence"][0]["block_id"])
    else:
        invalid["target"]["source_id"] = "999999"
    with pytest.raises(ValueError):
        record(pilot[0], request)
    assert pilot[0].read("SELECT * FROM paper_annotation_batches") == []
    assert pilot[0].read("SELECT * FROM paper_annotations") == []


def test_release_identity_and_pinned_versions_stay_separate(pilot):
    request = batch(pilot)
    record(pilot[0], request)
    assert query(pilot[0], "111", release="dr4")["page"]["total"] == 0
    assert query(pilot[0], "111", paper_version_id="another-version")["page"]["total"] == 0
    request["batch_id"] = "dr4-separate"
    request["annotations"][0]["target"]["release"] = "dr4"
    record(pilot[0], request)
    assert query(pilot[0], "111", release="dr4")["page"]["total"] == 1
    request["batch_id"] = "unknown-version"
    request["paper_version_id"] = "unknown-version"
    with pytest.raises(ValueError, match="no locally cached full text"):
        record(pilot[0], request)


def test_read_v2_without_migration_then_valid_write_migrates_additively(pilot, tmp_path):
    store = pilot[0]
    with sqlite3.connect(store.db_path) as connection:
        for table in ("paper_annotation_roles", "paper_annotations", "paper_annotation_batches"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version = 2")
    original = store.read("SELECT * FROM source_papers")
    assert context(store, pilot[1])["available"]
    assert query(store, "111")["schema_ready"] is False
    assert store.status()["schema"] == 2 and store.status()["n_sources"] == 6
    assert store.read("PRAGMA user_version")[0]["user_version"] == 2
    backup = store.backup(tmp_path / "before-v3.sqlite3")
    record(store, batch(pilot))
    assert store.status()["schema"] == SCHEMA_VERSION
    assert store.read("SELECT * FROM source_papers") == original
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_corrupt_pinned_artifact_cannot_be_used_as_evidence(pilot):
    path = Path(pilot[2]["paper"]["document_path"])
    path.chmod(0o666)
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(BlobIntegrityError):
        record(pilot[0], batch(pilot))
    assert query(pilot[0], "111")["assessment_status"] == "not_assessed"


def test_retry_returns_original_ids_after_artifact_loss(pilot):
    request = batch(pilot)
    first = record(pilot[0], request)
    path = Path(pilot[2]["paper"]["document_path"])
    path.chmod(0o666)
    path.unlink()
    retry = record(pilot[0], request)
    assert retry["created"] is False and retry["annotation_ids"] == first["annotation_ids"]
    assert query(pilot[0], "111")["page"]["total"] == 1


def test_refresh_keeps_old_evidence_and_new_text_uses_new_pin(pilot, tmp_path):
    store, old_version, _ = pilot
    request = batch(pilot)
    record(store, request)
    client = TwaFixture()
    client.html = client.html.replace(b"13.2", b"99.9")
    refreshed = pc.build_paper_corpus("synthetic-twa-refresh", ["2305.03557v1"], store=store,
                                      client=client, output_dir=tmp_path / "data", include_pdf=False, refresh=True)
    new_version = refreshed["papers"][0]["version_id"]
    assert new_version != old_version
    request.update(batch_id="new-pin-wrong-quote", paper_version_id=new_version)
    with pytest.raises(ValueError, match="quote/offset mismatch"):
        record(store, request)
    old = query(store, "111", paper_version_id=old_version)["assessments"][0]
    assert "13.2" in old["annotation"]["relations"][2]["evidence"][0]["quote"]
    assert query(store, "111", paper_version_id=new_version)["assessment_status"] == "not_assessed"


def test_long_unicode_fields_have_usable_absolute_offsets(pilot, tmp_path):
    client = TwaFixture()
    text = "星" * 4500 + " TWA 1 appears here."
    client.html = client.html.replace(b"TW Hya is mentioned here for historical context only.", text.encode())
    built = pc.build_paper_corpus("long-field", ["2305.03557v1"], store=pilot[0],
                                  client=client, output_dir=tmp_path / "data", include_pdf=False, refresh=True)
    version = built["papers"][0]["version_id"]
    found = context(pilot[0], version, targets=[target()], context_blocks=0, limit=100)
    units = [u for u in found["units"] if u["locator"].get("html_id") == "intro"]
    assert [u["start"] for u in units] == [0, 4000]
    assert "".join(u["text"] for u in units) == text
    evidence = {"block_id": units[1]["block_id"], "field": "text", "start": 4501, "end": 4506, "quote": "TWA 1"}
    request = batch(pilot)
    request["paper_version_id"] = version
    request["annotations"][0].update(identity_evidence=[evidence], inspected_block_ids=[evidence["block_id"]],
                                       relations=[{"role": "mentioned", "statement": "Name mentioned in a long field.", "evidence": [evidence]}])
    record(pilot[0], request)


def test_reference_title_alone_cannot_bind_identity(pilot):
    evidence = span(pilot, "bib1", "TWA 99")
    request = batch(pilot)
    request["annotations"][0].update(target=target("TWA 99"), paper_name="TWA 99",
                                      identity_evidence=[evidence], inspected_block_ids=[evidence["block_id"]],
                                      relations=[{"role": "mentioned", "statement": "Reference title only.", "evidence": [evidence]}])
    with pytest.raises(ValueError, match="bibliography entry alone"):
        record(pilot[0], request)


def test_library_alias_provenance_is_pinned_across_reference_refresh(pilot):
    store = pilot[0]
    snapshot = store.get_source_references("dr3", "111")["snapshot_id"]
    identity = span(pilot, "intro", "TW Hya")
    request = batch(pilot)
    request["annotations"][0].update(target={"release": "dr3", "source_id": "111"},
                                      paper_name="TW Hya", identity_evidence=[identity],
                                      inspected_block_ids=[identity["block_id"]],
                                      relations=[{"role": "mentioned", "statement": "Named in the text.", "evidence": [identity]}])
    record(store, request)
    store.put_source_references("dr3", "111", [], main_id="A changed current name")
    annotation = query(store, "111")["assessments"][0]["annotation"]
    assert annotation["matched_aliases"][0]["provenance"] == f"SIMBAD bibliography snapshot {snapshot}"
    assert record(store, request)["created"] is False


def test_latex_raw_table_has_offsets_without_invented_cells(pilot, tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "_parse_document", lambda kind, data, metadata: paper_text.parse_latex(data, paper=metadata))

    class LatexFixture(FakeArxiv):
        def fetch(self, identifier, kind):
            from dataclasses import replace
            fetched = super().fetch(identifier, kind)
            return replace(fetched, data=br"\begin{document}\section{Results}We analyse stars.\begin{table}\caption{Targets}\begin{tabular}{ll}TWA 1 & comparison\\\end{tabular}\end{table}\end{document}")

    client = LatexFixture()
    client.absent.add("html")
    built = pc.build_paper_corpus("latex-evidence", ["2305.03557v1"], store=pilot[0],
                                  client=client, output_dir=tmp_path / "data", include_pdf=False, refresh=True)
    version = built["papers"][0]["version_id"]
    found = context(pilot[0], version, targets=[target()], context_blocks=0)
    assert found["paper"]["quality"]["status"] == "partial"
    hit = next(h for h in found["candidate_matches"] if h["quote"] == "TWA 1")
    assert hit["field"] == "raw"
    assert not any(u["field"] == "table_cell" for u in found["units"])
    evidence = {key: hit[key] for key in ("block_id", "field", "start", "end", "quote")}
    request = batch(pilot)
    request["paper_version_id"] = version
    request["annotations"][0].update(identity_evidence=[evidence], inspected_block_ids=[hit["block_id"]],
                                       relations=[{"role": "mentioned", "statement": "Name occurs in raw LaTeX table.", "evidence": [evidence]}])
    record(pilot[0], request)


def test_model_forbids_unsupported_roles_review_status_and_ambiguous_assertions(pilot):
    for extra in ({"review_status": "approved"}, {"subject_scope": "sample"}, {"identity_status": "ambiguous"}):
        request = batch(pilot)
        request["annotations"][0].update(extra)
        with pytest.raises(ValidationError):
            PaperAnnotationBatch.model_validate(request)
    request = batch(pilot)
    request["annotations"][0]["relations"][0]["role"] = "physically_confirmed_member"
    with pytest.raises(ValidationError):
        PaperAnnotationBatch.model_validate(request)


def test_mcp_contract_round_trip_and_structured_errors(pilot, monkeypatch):
    import astro_pipeline.library
    monkeypatch.setattr(astro_pipeline.library, "get_library", lambda **kwargs: pilot[0])
    tools = {tool.name: tool for tool in asyncio.run(mcp_server.mcp.list_tools())}
    assert "PaperAnnotationBatch" in tools["record_paper_annotations"].inputSchema["$defs"]
    assert mcp_server.get_paper_evidence_context(pilot[1], targets=[target()])["available"]

    async def call():
        # Exercises FastMCP/Pydantic deserialization, not just the Python wrapper.
        return await mcp_server.mcp.call_tool("record_paper_annotations", {"batch": batch(pilot)})
    result = asyncio.run(call())
    assert result
    assert mcp_server.query_source_evidence("111")["page"]["total"] == 1
    assert "error" in mcp_server.get_paper_evidence_context("missing", limit=0)
    assert "error" in mcp_server.query_source_evidence("111", role="unknown")
