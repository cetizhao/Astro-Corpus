import json
from types import SimpleNamespace

from astro_pipeline.library.source_evidence import source_paper_evidence


def test_existing_structured_artifact_is_used(tmp_path, monkeypatch):
    document = {"paper": {"title": "TW Hya observations"}, "blocks": [
        {"id": "p1", "type": "paragraph", "text": "TW Hya has emission",
         "section_path": ["Results"], "locator": {"html_id": "p1"}},
        {"id": "t1", "type": "table", "text": "Members", "cells": [[{"text": "TW Hya"}, {"text": "−1.2"}]]},
        {"type": "reference", "text": "TW Hya reference"}]}
    path = tmp_path / "document.json"
    path.write_text(json.dumps(document))
    store = SimpleNamespace(blobs=SimpleNamespace(verify=lambda artifact: path))
    monkeypatch.setattr("astro_pipeline.library.source_evidence.lookup_paper_text",
                        lambda *args, **kwargs: {"versions": [{"version_id": "v1", "artifacts": [{"kind": "json"}]}]})
    result = source_paper_evidence(store, {"source": {"source_id": "123", "gaia_release": "dr3"},
        "active_bibliography": {"main_id": "TW Hya", "papers": [{"bibcode": "test"}]}})
    paper = result["papers"][0]
    assert paper["groups"] == ["body", "table", "title"]
    assert len(paper["evidence"]) == 3
    assert paper["evidence"][-1]["row"] == 1


def test_missing_fulltext_is_not_classified_as_absent(monkeypatch):
    monkeypatch.setattr("astro_pipeline.library.source_evidence.lookup_paper_text", lambda *a, **k: {"versions": []})
    result = source_paper_evidence(None, {"active_bibliography": {"papers": [{"bibcode": "x"}]}})
    assert result["papers"][0]["status"] == "fulltext_not_cached"
