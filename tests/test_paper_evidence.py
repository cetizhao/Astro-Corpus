import pytest

from astro_pipeline.paper_evidence import analyze_paper, load_analysis, save_analysis
from astro_pipeline.library.store import LibraryStore


HTML = """<article><h1>TW Hya observations</h1>
<div class="ltx_abstract"><p>We observed TW Hya.</p></div>
<section><h2>3. Results</h2><p id="p1">TW Hya has a disk.</p>
<p>TWA 10 is not TWA 1's exact identifier.</p></section>
<figure id="f2"><figcaption>Figure 2: TW Hya image</figcaption></figure>
<table id="t1"><caption>Targets</caption><tr><th>Name</th><th>Flux</th></tr>
<tr><td>TW Hya</td><td>−1.5</td></tr></table>
<section id="references"><p>TW Hya bibliography title</p></section></article>"""


def analyze(**kwargs):
    return analyze_paper(source_id="123", release="dr3", aliases=["TW Hya"],
                         bibcode="2026TEST", document_url="https://example.org/paper",
                         html=HTML, **kwargs)


def test_overlapping_locations_and_original_scientific_text():
    result = analyze()
    assert set(result["groups"]) == {"title", "abstract", "body", "figure_caption", "table"}
    bodies = [e for e in result["evidence"] if e["location_type"] == "body"]
    assert len(bodies) == 1
    assert bodies[0]["section"] == "3. Results"
    assert bodies[0]["locator"]["url"].endswith("#p1")
    row = next(e for e in result["evidence"] if e.get("component") == "row")
    assert row["cells"] == ["TW Hya", "−1.5"]
    assert result["coverage"]["figure_text"]["status"] == "not_checked"
    assert result["document"]["original_html"] == HTML


def test_boundaries_and_missing_material_do_not_claim_absence():
    result = analyze_paper(source_id="123", release="dr3", aliases=["TWA 1"],
                           bibcode="x", html="<p>TWA 10</p>", document_url="x")
    assert not result["evidence"]
    assert result["status"] == "no_match_in_supplied_materials"
    assert result["coverage"]["abstract"]["status"] == "not_available"


def test_vizier_provenance_required_and_rows_preserved():
    table = {"bibcode": "2026TEST", "table_id": "J/AJ/test/table1",
             "association_provenance": "catalogue ReadMe bibcode",
             "rows": [{"Name": "TW Hya", "value": 0.3}]}
    result = analyze(vizier_tables=[table])
    evidence = next(e for e in result["evidence"] if e["location_type"] == "vizier")
    assert evidence["row"] == table["rows"][0]
    assert evidence["association_status"] == "caller_asserted"
    with pytest.raises(ValueError):
        analyze(vizier_tables=[{**table, "bibcode": "other"}])


def test_analysis_identity_and_sqlite_roundtrip(tmp_path):
    store = LibraryStore(tmp_path / "library")
    store.initialize()
    first = analyze(paper_version="v1")
    second = analyze(paper_version="v2")
    assert first["analysis_id"] != second["analysis_id"]
    save_analysis(store, first)
    save_analysis(store, second)
    assert load_analysis(store, first["analysis_id"])["analysis"] == first
    assert load_analysis(store, second["analysis_id"])["analysis"] == second
    assert load_analysis(None, "missing")["status"] == "not_found"


def test_mcp_saves_full_document_and_read_does_not_create_library(tmp_path, monkeypatch):
    from astro_pipeline import mcp_server
    root = tmp_path / "shared"
    monkeypatch.setenv("ASTRO_PIPELINE_LIBRARY_DIR", str(root))
    assert mcp_server.get_paper_evidence("missing")["status"] == "not_found"
    assert not root.exists()
    result = mcp_server.analyze_paper_evidence(
        source_id="123", bibcode="2026TEST", html=HTML,
        document_url="https://example.org/paper", aliases=["TW Hya"],
    )
    assert "original_html" not in result["document"]
    saved = mcp_server.get_paper_evidence(result["analysis_id"])
    assert saved["analysis"]["document"]["original_html"] == HTML
