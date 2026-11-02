"""Evidence UI integration: pinned history, Unicode slices and offline snapshots."""
from __future__ import annotations

import json
from urllib.parse import urlencode

import pytest

from astro_pipeline.library.evidence import record_paper_annotations
from astro_pipeline.library.overview import LibraryOverview, SourceFilters
from astro_pipeline.viz.evidence import evidence_snapshot, export_evidence_html
from tests.test_library_viewer import _running_server, _request
from tests.test_paper_evidence import pilot, batch, span, target


def test_routes_filters_history_and_snapshot(pilot, tmp_path):
    store, version, _ = pilot
    request = batch(pilot)
    record_paper_annotations(store, request)
    request["batch_id"] = "revision-two"
    record_paper_annotations(store, request)
    overview = LibraryOverview(store)
    try:
        page = overview.sources(filters=SourceFilters.create(evidence_state="recorded"))
        assert page["total"] == 1
        assert page["sources"][0]["annotation_count"] == 2
        assert page["sources"][0]["annotation_paper_count"] == 1
        assert overview.sources(filters=SourceFilters.create(evidence_state="not_assessed"))["total"] == 5
    finally:
        overview.close()
    with _running_server(store) as server:
        status, _, body = _request(server, "GET", "/api/v1/source/dr3/111/evidence?limit=1")
        result = json.loads(body)
        assert status == 200 and result["page"]["has_more"]
        assert result["assessments"][0]["paper"]["title"]
        block = request["annotations"][0]["identity_evidence"][0]["block_id"]
        status, _, body = _request(server, "GET", f"/api/v1/paper/{version}/context?" + urlencode({"block_id": block, "revision": result["revision"]}))
        assert status == 200 and json.loads(body)["units"][0]["block_id"] == block
        status, _, _ = _request(server, "POST", "/api/v1/source/dr3/111/evidence")
        assert status == 405
    snapshot = evidence_snapshot(store, [("dr3", "111"), ("dr4", "111")])
    assert len(snapshot["sources"]["dr3:111"]) == 2
    assert snapshot["sources"]["dr4:111"] == []
    assert snapshot["batch_ids"] == ["fixture-batch", "revision-two"]
    out = export_evidence_html(store, [("dr3", "111")], tmp_path / "snapshot.html")
    assert "revision-two" in out.read_text(encoding="utf-8")
    assert "script src=" not in out.read_text(encoding="utf-8")


def test_live_and_offline_evidence_browser(pilot, tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    store, _, _ = pilot
    from tests.test_membership_evidence import membership_batch
    request = membership_batch(pilot)
    cell = span(pilot, "table1", "TWA 7", "table_cell", row=1, column=0)
    note = span(pilot, "table1", "The upper limit", "table_note", note_index=0)
    request["annotations"].append({
        "target": target("TWA 7", "222"), "paper_name": "TWA 7", "subject_scope": "source",
        "identity_status": "resolved", "identity_basis": "Synthetic mapping", "identity_evidence": [cell],
        "assessment": "evidence_found", "inspected_block_ids": [cell["block_id"]],
        "relations": [{"role": "in_sample", "statement": "Synthetic table", "evidence": [cell, note]}],
    })
    record_paper_annotations(store, request)
    out = export_evidence_html(store, [("dr3", "111"), ("dr3", "222")], tmp_path / "evidence.html")
    with _running_server(store) as server, pw.sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1080})
        errors = []; page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"http://127.0.0.1:{server.server_address[1]}/")
        page.locator('#source-rows tr[data-key="dr3:222"]').click()
        pw.expect(page.locator(".e-summary")).to_contain_text("1 篇论文")
        page.locator(".e-relation button").first.click()
        pw.expect(page.locator("dialog mark")).to_have_text("TWA 7")
        pw.expect(page.locator("dialog th").first).to_be_visible()
        cells = page.locator("dialog tr").nth(2).locator("td")
        pw.expect(cells).to_have_count(3)
        pw.expect(cells.nth(1)).to_have_text("")
        pw.expect(cells.nth(2)).to_have_text("upper limit")
        page.get_by_role("button", name="关闭原文").click()
        page.locator("#evidence-filter").select_option("recorded")
        pw.expect(page.locator("#count-sources")).to_have_text("2")
        page.goto(out.as_uri())
        page.locator("#e-source").select_option("dr3:111")
        page.locator(".role-reports_result button").click()
        pw.expect(page.locator("dialog mark")).to_contain_text("13.2 ± 0.3")
        page.get_by_role("button", name="关闭原文").click()
        page.locator(".e-result button").click()
        pw.expect(page.locator("dialog mark")).to_have_text("candidate member of TWA, conditional on a future systemic velocity measurement.")
        page.get_by_role("button", name="关闭原文").click()
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors
        browser.close()


def test_existing_source_paper_overview_embeds_history(pilot, tmp_path, monkeypatch):
    import pandas as pd
    import astro_pipeline.library
    from astro_pipeline.viz.source_paper import build_overview_html
    record_paper_annotations(pilot[0], batch(pilot))
    monkeypatch.setattr(astro_pipeline.library, "get_library", lambda **kwargs: pilot[0])
    catalog, edges = tmp_path / "members.csv", tmp_path / "edges.parquet"
    pd.DataFrame([{"Gaia": "111", "RAICRS": 10.0, "DEICRS": 20.0}]).to_csv(catalog, index=False)
    pd.DataFrame([{"source_id": "111", "bibcode": "test", "year": 2023}]).to_parquet(edges)
    out = build_overview_html(tmp_path / "source_paper_viz.html", catalog_path=catalog, edges_path=edges, inline_echarts=True)
    document = out.read_text(encoding="utf-8")
    assert '"evidence_snapshot":' in document and '"fixture-batch"' in document
    assert '"dr3:111"' in document and "window.AstroEvidence.mount(ul" in document
