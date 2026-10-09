"""Chrome checks: uv run --with playwright python -m pytest tests/test_viewer_browser.py."""
from __future__ import annotations

import json
import threading

import pytest

playwright = pytest.importorskip("playwright.sync_api")

from astropy.table import Table
from astro_pipeline.library.identity import product_item_key
from astro_pipeline.library.store import LibraryStore
from astro_pipeline.viz.library_viewer import create_library_viewer_server


@pytest.fixture
def viewer(tmp_path):
    store = LibraryStore(tmp_path / "library")
    corpus = store.register_corpus(tmp_path / "corpus", "browser-fixture")
    run = store.start_run(corpus)
    store.index_members(run, [
        {"source_id": str(1000000000000000000 + i), "gaia_release": "dr3",
         "RAICRS": float(i % 360), "DEICRS": -20.0, "Gmag_gdr3": 12.0,
         "main_id": '<img src=x onerror="window.injected=true">' if i == 0 else None}
        for i in range(520)
    ] + [{"source_id": "1000000000000000000", "gaia_release": "dr4"}])
    store.finish_run(run, "completed", metadata={"members_indexed": True, "published": True})
    source_id = "1000000000000000000"
    spectrum = tmp_path / "sampled.fits"
    Table({"source_id": [int(source_id)], "wavelength": [[400., 500., 600.]],
           "flux": [[1e-15, 2e-15, 1e-15]], "flux_error": [[1e-16] * 3]}).write(spectrum)
    blob = store.blobs.publish(spectrum, content_type="application/fits")
    contract = {"release": "dr3", "retrieval_type": "XP_SAMPLED",
                "linking_parameter": "SOURCE_ID", "data_structure": "INDIVIDUAL",
                "output_format": "fits", "valid_data": False}
    lease = store.acquire_lease("product", product_item_key(contract, source_id))
    store.save_product(contract, source_id, [{"blob": blob, "role": "normalized",
                        "slot": "spectrum", "metadata": {"is_primary": True}}], lease)
    store.release_lease(lease)
    server = create_library_viewer_server(store, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1080}, color_scheme="light")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/")
            playwright.expect(page.locator("#connection")).to_contain_text("已同步", timeout=30000)
            yield page, store, corpus, errors
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_complete_sky_pagination_release_spectrum_and_refresh(viewer):
    page, store, corpus, errors = viewer
    expect = playwright.expect
    expect(page.locator("#count-sources")).to_have_text("521")
    expect(page.locator("#count-positions")).to_have_text("520")
    expect(page.locator("#count-missing")).to_have_text("1")
    assert page.evaluate("() => AstroLibraryView.state.sky.length") == 520
    first_key = page.locator("#source-rows tr[data-key]").first.get_attribute("data-key")
    page.locator("#next").click()
    expect(page.locator("#page-label")).to_have_text("第 2 页")
    assert page.locator("#source-rows tr[data-key]").first.get_attribute("data-key") != first_key
    page.locator("#previous").click()
    expect(page.locator("#page-label")).to_have_text("第 1 页")
    row = page.locator("#source-rows tr[data-key]").first
    row.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#spectrum-chart")).to_be_visible(timeout=15000)
    expect(page.locator("#spectrum-note")).to_contain_text("XP combined")
    expect(page.locator("#spectrum-provenance")).to_contain_text("SHA-256")
    expect(page.locator("#spectrum-table-body tr")).to_have_count(3)
    pixels = page.evaluate("""() => {
        const chart = echarts.getInstanceByDom(document.getElementById('spectrum-chart'));
        const points = chart.getOption().series[0].data;
        return points.map(point => chart.convertToPixel({seriesIndex: 0}, point));
    }""")
    assert abs(pixels[0][1] - pixels[1][1]) > 20
    page.locator("#theme").click()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    selected = page.evaluate("() => AstroLibraryView.state.selected.key")
    new_run = store.start_run(corpus)
    store.index_members(new_run, [{"source_id": "9000000000000000000", "gaia_release": "dr3"}])
    store.finish_run(new_run, "completed", metadata={"members_indexed": True, "published": True})
    expect(page.locator("#count-sources")).to_have_text("522", timeout=15000)
    assert page.evaluate("() => AstroLibraryView.state.selected.key") == selected
    page.locator("#release").select_option("dr4")
    expect(page.locator("#count-sources")).to_have_text("1")
    page.locator("#source-rows tr[data-key]").first.click()
    expect(page.locator("#spectrum-empty")).to_contain_text("待适配", timeout=15000)
    expect(page.locator("#spectrum-chart")).to_be_hidden()
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(200)
    assert not page.evaluate("() => document.documentElement.scrollWidth > innerWidth")
    assert not errors


def test_untrusted_source_names_and_paper_titles_remain_text(viewer):
    page, _, _, errors = viewer
    payload = '<img src=x onerror="window.injected=true">'

    def paper_response(route):
        response = route.fetch()
        data = response.json()
        data["active_bibliography"] = {
            "papers": [{"title": payload, "bibcode": "fixture", "doi": "javascript:alert(1)"}]
        }
        route.fulfill(response=response, json=data)

    page.route("**/api/v1/source/dr3/1000000000000000000", paper_response)
    row = page.locator("#source-rows tr[data-key]").first
    playwright.expect(row).to_contain_text(payload)
    row.click()
    playwright.expect(page.locator("#source-title")).to_have_text(payload)
    playwright.expect(page.locator("#source-papers a")).to_have_text(payload)
    assert page.locator("#source-papers a").get_attribute("href").startswith("https://doi.org/")
    assert page.locator("#source-rows img, #source-title img, #source-papers img").count() == 0
    assert page.evaluate("() => window.injected === undefined")
    assert not errors


def test_latest_runs_are_visible_even_when_api_returns_oldest_first(viewer):
    page, _, _, errors = viewer

    def meta_response(route):
        response = route.fetch()
        data = response.json()
        data["runs"] = [
            {"run_id": f"run-{i}", "label": f"attempt-{i}",
             "started_at": i, "status": "completed", "snapshot_state": "historical"}
            for i in range(21)
        ]
        route.fulfill(response=response, json=data)

    page.route("**/api/v1/meta", meta_response)
    page.reload()
    playwright.expect(page.locator("#runs .run-card")).to_have_count(18)
    playwright.expect(page.locator("#runs .run-title").first).to_have_text("attempt-20")
    playwright.expect(page.locator("#runs .run-title").last).to_have_text("attempt-3")
    assert not errors


@pytest.mark.parametrize("status", ["failed", "completed", "running", "finalizing"])
def test_unpublished_runs_do_not_imply_active_work(viewer, status):
    page, _, _, errors = viewer
    relation = {"run_id": "unpublished-fixture", "label": "unpublished fixture",
                "status": status, "snapshot_state": "pending", "started_at": 9e12}

    def meta_response(route):
        response = route.fetch()
        data = response.json()
        data["runs"].append(relation)
        route.fulfill(response=response, json=data)

    def detail_response(route):
        response = route.fetch()
        data = response.json()
        data["working_datasets"] = [relation]
        data["dto"]["working_datasets"] = [relation]
        route.fulfill(response=response, json=data)

    page.route("**/api/v1/meta", meta_response)
    page.route("**/api/v1/source/dr3/1000000000000000000", detail_response)
    page.reload()
    card = page.locator("#runs .run-card").first
    playwright.expect(card).to_contain_text("unpublished fixture")
    playwright.expect(card).to_contain_text("尚未发布")
    playwright.expect(card).not_to_contain_text("获取中")
    page.locator("#source-rows tr[data-key]").first.click()
    playwright.expect(page.locator("#spectrum-chart")).to_be_visible()
    if status in ("running", "finalizing"):
        playwright.expect(page.locator("#source-badges")).to_contain_text("工作中")
    else:
        playwright.expect(page.locator("#source-badges")).not_to_contain_text("工作中")
    assert not errors


def test_library_disappearance_clears_selection_and_ignores_late_responses(viewer):
    page, _, _, errors = viewer
    expect = playwright.expect
    page.locator("#source-rows tr[data-key]").first.click()
    expect(page.locator("#spectrum-chart")).to_be_visible()
    page.locator("#next").click()
    expect(page.locator("#page-label")).to_have_text("第 2 页")
    page.wait_for_function("() => !AstroLibraryView.state.refreshing")
    expect(page.locator("#spectrum-chart")).to_be_visible()
    page.evaluate("""() => {
        const savedDetail = AstroLibraryView.state.detail;
        const savedSpectrum = AstroLibraryView.state.spectrum;
        const original = window.fetch;
        window.fetch = (url, options) => {
            if (String(url).includes('/api/v1/source/')) return new Promise(resolve => {
                window.releaseDetail = () => resolve(new Response(JSON.stringify(savedDetail)));
            });
            if (String(url).includes('/api/v1/spectrum/')) return new Promise(resolve => {
                window.releaseSpectrum = () => resolve(new Response(JSON.stringify(savedSpectrum)));
            });
            return original(url, options);
        };
        const selected = AstroLibraryView.state.selected;
        AstroLibraryView.selectSource(selected.release, selected.source_id, true);
        document.getElementById('spectrum-product').dispatchEvent(new Event('change'));
    }""")
    page.wait_for_function("() => window.releaseDetail && window.releaseSpectrum")
    page.route("**/api/v1/meta", lambda route: route.fulfill(
        json={"exists": False, "revision": "absent-library", "total_sources": 0,
              "datasets": [], "releases": [], "runs": []},
    ))
    page.locator("#refresh").click()
    expect(page.locator("#count-sources")).to_have_text("0")
    expect(page.locator("#page-label")).to_have_text("第 1 页")
    expect(page.locator("#previous")).to_be_disabled()
    expect(page.locator("#next")).to_be_disabled()
    page.evaluate("() => { window.releaseDetail(); window.releaseSpectrum(); }")
    page.wait_for_timeout(100)
    page.locator("#theme").click()
    expect(page.locator("#source-title")).to_have_text("请选择一个源")
    expect(page.locator("#source-properties")).to_be_empty()
    expect(page.locator("#spectrum-chart")).to_be_hidden()
    expect(page.locator("#spectrum-table-body")).to_be_empty()
    expect(page.locator("#spectrum-provenance")).to_be_empty()
    assert page.evaluate("() => [AstroLibraryView.state.selected, AstroLibraryView.state.detail, AstroLibraryView.state.spectrum]") == [None, None, None]
    assert not errors


@pytest.mark.parametrize("failure", ["source", "spectrum"])
def test_failed_refresh_cannot_redisplay_previous_spectrum_on_theme_change(viewer, failure):
    page, _, _, errors = viewer
    expect = playwright.expect
    page.locator("#source-rows tr[data-key]").first.click()
    expect(page.locator("#spectrum-chart")).to_be_visible()
    if failure == "source":
        page.route("**/api/v1/source/**", lambda route: route.fulfill(
            json={"resolved": False, "error": "Source no longer exists"},
        ))
        page.locator("#refresh").click()
        expect(page.locator("#source-title")).to_have_text("无法读取源")
        expect(page.locator("#source-properties")).to_be_empty()
    else:
        page.route("**/api/v1/spectrum/**", lambda route: route.fulfill(
            status=503, json={"message": "Spectrum request failed"},
        ))
        page.locator("#spectrum-product").dispatch_event("change")
    expect(page.locator("#spectrum-empty")).to_contain_text("读取失败")
    page.locator("#theme").click()
    expect(page.locator("#spectrum-chart")).to_be_hidden()
    assert page.evaluate("() => AstroLibraryView.state.spectrum") is None
    assert not errors


def test_projection_and_recovery_from_transient_page_failure(viewer):
    page, _, _, errors = viewer
    values = page.evaluate("""() => {
      const h = AstroLibraryView.hammer;
      return [h(180, 0), h(0, 0), h(360, 0), h(90, 0), h(270, 0), h(180, 90)];
    }""")
    assert values[0] == pytest.approx([0, 0])
    assert values[1] == pytest.approx(values[2])
    assert values[3][0] > 0 > values[4][0]
    assert values[5] == pytest.approx([0, 2 ** 0.5])
    page.route("**/api/v1/sources?*", lambda route: route.fulfill(
        status=503, content_type="application/json", body=json.dumps({"error": {"code": "temporary", "message": "transient fixture"}})), times=1)
    page.locator("#refresh").click()
    playwright.expect(page.locator("#notice")).to_contain_text("transient fixture")
    playwright.expect(page.locator("#connection")).to_contain_text("已同步", timeout=15000)
    playwright.expect(page.locator("#notice")).to_be_hidden()
    assert not errors
