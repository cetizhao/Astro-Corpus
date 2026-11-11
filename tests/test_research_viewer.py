"""Research tasks in the local viewer and the public, database-free Pages build."""
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import re
import threading

import pytest

from astro_pipeline.library.store import LibraryStore
from astro_pipeline.research_tasks import compile_bundle, digest, read_json, write_json
from astro_pipeline.viz.research import public_research_snapshot, research_snapshot
from tests.test_library_viewer import _running_server, _request
from tests.test_research_tasks import inputs

ROOT = Path(__file__).parents[1]
SID = "5401795662560500352"


@pytest.fixture
def prepared(inputs, tmp_path):
    inputs[0]["selection"]["papers"][0]["targets"] = ["TW Hya"]
    bundle = compile_bundle(*inputs[:3])
    path = tmp_path / "research_tasks/fixture/bundle.json"
    write_json(path, bundle)
    return bundle, path, inputs[1]


def test_research_http_is_read_only_and_reports_invalid_bundles(prepared, tmp_path):
    bundle, path, _ = prepared
    store = LibraryStore(tmp_path / "_library")
    assert not store.db_path.exists()
    with _running_server(store) as server:
        status, _, raw = _request(server, "GET", "/api/v1/research-tasks")
        assert status == 200
        response = json.loads(raw)
        assert len(response["suites"][0]["tasks"]) == 2
        assert response["suites"][0]["targets"][0]["source_id"] == SID
        assert "source_snapshot" not in response["suites"][0]
        assert str(tmp_path).encode() not in raw
        assert _request(server, "POST", "/api/v1/research-tasks")[0] == 405
        assert _request(server, "GET", "/api/v1/research-tasks?path=elsewhere")[0] == 400
        bundle["tasks"][0]["question"] = "tampered"
        write_json(path, bundle)
        response = json.loads(_request(server, "GET", "/api/v1/research-tasks")[2])
        assert not response["suites"] and response["warnings"]
    assert not store.db_path.exists()
    absent = tmp_path / "absent"
    assert research_snapshot(absent) == {"schema_version": "research-view-1", "suites": [], "warnings": []}
    assert not absent.exists()


def test_public_export_is_pinned_and_contains_only_selected_data(prepared):
    bundle, _, source_manifest = prepared
    exported = public_research_snapshot(bundle, source_manifest)
    assert len(exported["sources"]) == 1
    assert exported["sources"][0]["source_id"] == SID
    assert exported["sources"][0]["astrometry"]["parallax_mas"] == 16.6
    assert "source_snapshot" not in exported["research_tasks"]["suites"][0]
    assert not re.search(r'"[A-Za-z]:[\\/]|[\\/]Users[\\/]|password|api_key', json.dumps(exported), re.I)
    (source_manifest.parent / "members.csv").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        public_research_snapshot(bundle, source_manifest)


def test_local_research_browser_tracks_release_and_expands_evidence(prepared, tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    bundle, path, _ = prepared
    payload = '<img src=x onerror="window.injected=true">'
    bundle["tasks"][0]["question"] += payload
    bundle["bundle_sha256"] = digest({k: v for k, v in bundle.items() if k != "bundle_sha256"})
    write_json(path, bundle)
    store = LibraryStore(tmp_path / "_library")
    corpus = store.register_corpus(tmp_path / "corpus", "ctts-fixture")
    run = store.start_run(corpus)
    store.index_members(run, [{"source_id": SID, "gaia_release": r, "RAICRS": 10, "DEICRS": 20} for r in ["dr3", "dr4"]])
    store.finish_run(run, "completed", metadata={"members_indexed": True, "published": True})
    m = bundle["materials"][0]
    loc = m["provenance"]["location"]
    context = {"available": True, "paper": {
        "paper_version_id": "version-1", "document_sha256": bundle["papers"][0]["artifact_sha256"],
        "title": "Synthetic", "versioned_id": "2308.14590v1", "parser_version": "fixture",
    }, "units": [{**loc, "text": m["text"], "source_url": "https://arxiv.org/abs/2308.14590v1"}],
        "page": {"returned": 1, "has_more": False}}
    with _running_server(store) as server, pw.sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1080})
        errors = []; page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/api/v1/paper/version-1/context?*", lambda route: route.fulfill(json=context))
        page.goto(f"http://127.0.0.1:{server.server_address[1]}/")
        pw.expect(page.locator(".r-task")).to_have_count(2)
        first = page.locator('.r-task[data-task-id="case1"]')
        pw.expect(first.locator(".r-answer-value")).to_be_hidden()
        first.locator(".r-answer summary").click()
        pw.expect(first.locator(".r-answer-value")).to_have_text("0.4 1")
        first.locator(".r-materials summary").click()
        first.get_by_role("button", name="查看原文位置").click()
        pw.expect(page.locator(".r-dialog mark")).to_have_text(m["text"])
        page.get_by_role("button", name="关闭任务原文").click()
        page.locator(f'#source-rows tr[data-key="dr3:{SID}"]').click()
        pw.expect(page.locator("#source-papers")).to_contain_text("科研任务选文")
        page.locator(f'#source-rows tr[data-key="dr4:{SID}"]').click()
        pw.expect(page.locator("#research-count")).to_have_text("0 / 2 道任务")
        page.get_by_role("button", name="查看全部任务").click()
        page.locator("#research-category").select_option("insufficient_information")
        # Synthetic category is intentionally not a production category; select all again.
        page.locator("#research-category").select_option("")
        pw.expect(page.locator(".r-task")).to_have_count(2)
        assert page.locator("#research-tasks img").count() == 0
        assert page.evaluate("window.injected === undefined")
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(200)  # ECharts resizes on the next browser frame.
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors
        browser.close()


def test_public_research_browser_runs_without_local_api(tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    spec = importlib.util.spec_from_file_location("pages_research", ROOT / "scripts/build_pages.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    module.build(tmp_path)
    data = read_json(tmp_path / "demo.json")
    assert len(data["sources"]) == 12
    assert sorted(len(s["tasks"]) for s in data["research_tasks"]["suites"]) == [12, 16]
    assert not re.search(r'"[A-Za-z]:[\\/]|[\\/]Users[\\/]|password|api_key', json.dumps(data), re.I)
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(SimpleHTTPRequestHandler, directory=str(tmp_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        with pw.sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1080})
            errors, api_requests = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: api_requests.append(request.url) if "/api/v1/" in request.url else None)
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/")
            pw.expect(page.locator("#count-sources")).to_have_text("12")
            pw.expect(page.locator("#source-title")).to_have_text("TW Hya")
            pw.expect(page.locator("#source-papers .e-paper")).to_have_count(3)
            page.get_by_role("button", name="查看全部任务").click()
            pw.expect(page.locator(".r-task")).to_have_count(28)
            page.locator("#research-level").select_option("integrated_case")
            pw.expect(page.locator(".r-task")).to_have_count(12)
            integrated = page.locator('.r-task[data-task-id="CTTS-C06"]')
            pw.expect(integrated.locator(".r-checkpoints")).to_be_hidden()
            integrated.locator(".r-fields summary").click()
            pw.expect(integrated.locator(".r-fields li")).to_have_count(5)
            integrated.locator(".r-answer summary").click()
            pw.expect(integrated.locator(".r-checkpoints li")).to_have_count(5)
            pw.expect(integrated.locator(".r-answer")).to_contain_text("2.2779")
            integrated.locator(".r-materials summary").click()
            integrated.get_by_role("button", name="查看原文位置").first.click()
            pw.expect(page.locator(".r-dialog mark")).to_contain_text("155.0")
            page.get_by_role("button", name="关闭任务原文").click()
            page.locator("#research-level").select_option("basic")
            pw.expect(page.locator(".r-task")).to_have_count(16)
            page.locator("#research-category").select_option("numerical_reasoning")
            pw.expect(page.locator(".r-task")).to_have_count(4)
            task = page.locator('.r-task[data-task-id="CTTS-012"]')
            pw.expect(task.locator(".r-answer-value")).to_be_hidden()
            task.locator(".r-materials summary").click()
            task.get_by_role("button", name="查看原文位置").first.click()
            pw.expect(page.locator(".r-dialog mark")).to_contain_text("veiling")
            page.get_by_role("button", name="关闭任务原文").click()
            task.locator(".r-answer summary").click()
            pw.expect(task.locator(".r-answer-value")).to_contain_text("0.401")
            page.locator("#search").fill("CTTS")
            pw.expect(page.locator("#count-sources")).to_have_text("4")
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_timeout(200)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not errors and not api_requests
            browser.close()
    finally:
        server.shutdown(); thread.join(timeout=5); server.server_close()
