from astro_pipeline.online_evidence import OnlineEvidence


def test_online_html_only_and_progress(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr("astro_pipeline.online_evidence.discover", lambda *a: (["TW Hya"], [{"bibcode":"x", "title":"TW Hya", "year":2025}]))
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def resolve(self, aid): return {"versioned_id":"2501.00001v1"}
        def fetch(self, aid, kind):
            assert kind == "html"
            return SimpleNamespace(data=b"<article><h1>TW Hya</h1><p>TW Hya is observed.</p></article>", final_url="https://arxiv.org/html/2501.00001v1")
    monkeypatch.setattr("astro_pipeline.online_evidence.ArxivClient", Client)
    service = OnlineEvidence(None)
    key = ("dr3","123")
    service.jobs[key] = {"processed":0,"papers":[],"cancel_requested":False}
    service._run(key,[{"bibcode":"x","arxiv_id":"2501.00001"}])
    result = service.status(*key)
    assert result["state"] == "completed"
    assert result["processed"] == 1
    assert result["papers"][0]["groups"] == ["body","title"]
    assert "original_html" not in result["papers"][0]["document"]
    # Selected requests ignore discovery order and preserve prior results.
    service.jobs[key].update(requested=["y"], processed=0)
    service._run(key,[{"bibcode":"y","title":"Second paper", "arxiv_id":"2501.00001"}])
    result = service.status(*key)
    assert result["total"] == result["processed"] == 1
    assert {p["paper"]["bibcode"] for p in result["papers"]} == {"x","y"}


def test_discovery_failure_is_not_empty_success(monkeypatch):
    def fail(*args): raise RuntimeError("network down")
    monkeypatch.setattr("astro_pipeline.online_evidence.discover", fail)
    service = OnlineEvidence(None)
    service.jobs[("dr3","123")] = {}
    service._run(("dr3","123"),[])
    assert service.status("dr3","123")["state"] == "failed"


def test_search_429_never_parses_error_body(monkeypatch):
    import httpx
    import pytest
    from astro_pipeline.sources.arxiv_search import search_arxiv, ArxivSearchRateLimited
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url):
            return httpx.Response(429, text="Too many requests", request=httpx.Request("GET", url))
    monkeypatch.setattr("astro_pipeline.sources.arxiv_search.httpx.Client", Client)
    with pytest.raises(ArxivSearchRateLimited, match="429"):
        search_arxiv("ti:test", max_retries=1)


def test_rate_limit_stops_batch(monkeypatch):
    service = OnlineEvidence(None)
    key = ("dr3", "123")
    service.jobs[key] = {"processed":0,"papers":[],"cancel_requested":False,
                         "aliases":["TW Hya"],"discovered":[{"bibcode":"a"},{"bibcode":"b"}]}
    monkeypatch.setattr("astro_pipeline.online_evidence.resolve_online_paper", lambda p: {"status":"rate_limited","errors":["429"]})
    service._run(key, [])
    assert service.status(*key)["state"] == "paused"
    assert service.status(*key)["processed"] == 1


def test_atom_url_identifier_and_title_fallback(monkeypatch):
    import xml.etree.ElementTree as ET
    from types import SimpleNamespace
    from astro_pipeline.sources.arxiv_search import _extract_id
    from astro_pipeline.online_evidence import resolve_online_paper
    for aid in ("2512.17074v1", "astro-ph/9901234v2"):
        entry = ET.fromstring(f'<entry xmlns="http://www.w3.org/2005/Atom"><id>http://arxiv.org/abs/{aid}</id></entry>')
        assert _extract_id(entry) == aid
    monkeypatch.setattr("astro_pipeline.online_evidence.time.sleep", lambda _: None)
    monkeypatch.setattr("astro_pipeline.sources.arxiv_search.search_arxiv", lambda *a, **k: [SimpleNamespace(title="TW Hya: observations", arxiv_id="2512.17074v1", abstract="summary")])
    assert resolve_online_paper({"title":"TW Hya observations."})["id"] == "2512.17074v1"
    result = resolve_online_paper({"title":"An unrelated paper"})
    assert result["status"] == "identifier_candidates"
    assert "id" not in result
