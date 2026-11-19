"""Scientific-record query routes, public projections and source-linked browsing."""
from copy import deepcopy
import json
from pathlib import Path
import re

import pytest

from astro_pipeline.library.research import export_research_corpus, record_research_item, update_research_item
from astro_pipeline.library.store import LibraryStore
from tests.test_library_viewer import _running_server, _request
from tests.test_paper_evidence import pilot
from tests.test_research_corpus import scientific_item, task_item


def test_scientific_http_filters_and_read_only_absent(tmp_path, pilot):
    absent = LibraryStore(tmp_path / "absent")
    with _running_server(absent) as server:
        assert json.loads(_request(server, "GET", "/api/v1/research-items")[2])["items"] == []
        for query in ("path=elsewhere", "include_history=perhaps", "limit=1000", "offset=-1", "kind=answer"):
            assert _request(server, "GET", "/api/v1/research-items?" + query)[0] == 400
        assert _request(server, "POST", "/api/v1/research-items")[0] == 405
    assert not absent.root.exists()
    first = record_research_item(pilot[0], scientific_item(pilot), "r1")["record"]
    item = deepcopy(first["item"]); item["summary"] = "Updated interpretation"
    second = update_research_item(pilot[0], first["record_id"], first["revision_id"], item, "r2", "Correct summary")["record"]
    with _running_server(pilot[0]) as server:
        def query(params): return json.loads(_request(server, "GET", "/api/v1/research-items?" + params)[2])
        assert query("source_id=111&release=dr3")["items"][0]["revision_id"] == second["revision_id"]
        assert query("source_id=111&release=dr4")["items"] == []
        assert query("record_id=" + first["record_id"] + "&include_history=true&limit=1")["page"]["has_more"]
        assert query("revision_id=" + first["revision_id"])["items"][0]["item"]["summary"] == first["item"]["summary"]


def test_public_scientific_export_keeps_pinned_inputs_without_local_paths(pilot, tmp_path):
    record = record_research_item(pilot[0], scientific_item(pilot), "record")["record"]
    task = record_research_item(pilot[0], task_item(pilot, record["revision_id"]), "task")["record"]
    out = export_research_corpus(pilot[0], "public", output_root=tmp_path,
                                  record_ids=[task["record_id"]], include_observatory=True)
    raw = Path(out["paths"]["observatory.json"]).read_text(encoding="utf-8")
    public = json.loads(raw)
    assert not re.search(r'"[A-Za-z]:[\\/]|[\\/]Users[\\/]|password|api_key', raw, re.I)
    assert public["science"]["items"][0]["revision_id"] == task["revision_id"]
    assert {r["revision_id"] for r in public["science"]["archive"]} == {task["revision_id"], record["revision_id"]}
    assert all("source_snapshots" not in r for r in public["science"]["archive"])
    assert public["sources"][0]["key"] == "dr3:111"
    assert public["sources"][0]["annotation_count"] == 0


def test_local_scientific_browser_history_evidence_and_release(pilot):
    pw = pytest.importorskip("playwright.sync_api")
    item = scientific_item(pilot)
    item["title"] = '<img src=x onerror="window.injected=true">'
    first = record_research_item(pilot[0], item, "record")["record"]
    changed = deepcopy(item); changed["summary"] = "Updated synthetic record"
    update_research_item(pilot[0], first["record_id"], first["revision_id"], changed, "revision", "Clarify")
    task = record_research_item(pilot[0], task_item(pilot, first["revision_id"]), "task")["record"]
    with _running_server(pilot[0]) as server, pw.sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1080})
        errors = []; page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"http://127.0.0.1:{server.server_address[1]}/")
        pw.expect(page.locator("#science-list > .sci-record")).to_have_count(2)
        record = page.locator(f'#science-list > [data-record-id="{first["record_id"]}"]')
        pw.expect(record).to_contain_text("版本 2")
        record.locator(".sci-evidence > summary").click()
        record.get_by_role("button", name="定位科研证据").click()
        pw.expect(page.locator(".sci-dialog mark")).to_contain_text("13.2 ± 0.3")
        page.get_by_role("button", name="关闭科研证据").click()
        record.locator(".sci-history > summary").click()
        record.get_by_role("button", name="读取历史版本").click()
        pw.expect(record.locator(".sci-old-version")).to_have_count(2)
        plan = page.locator(f'#science-list > [data-record-id="{task["record_id"]}"]')
        plan.locator(".sci-related > summary").click()
        plan.get_by_role("button", name="读取版本 " + first["revision_id"]).click()
        pw.expect(plan.locator(".sci-related .sci-record")).to_contain_text("版本 1")
        page.locator('#source-rows tr[data-key="dr3:111"]').click()
        pw.expect(page.locator("#science-summary")).to_contain_text("2 条科研内容")
        page.locator('#source-rows tr[data-key="dr4:111"]').click()
        pw.expect(page.locator("#science-summary")).to_contain_text("0 条科研内容")
        page.get_by_role("button", name="查看全部科研内容").click()
        page.locator("#science-kind").select_option("research_task")
        pw.expect(page.locator("#science-list > .sci-record")).to_have_count(1)
        assert not page.locator("#science-list .r-answer").count()
        assert not page.locator("#science-list img").count()
        assert page.evaluate("window.injected === undefined")
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(200)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors
        browser.close()
