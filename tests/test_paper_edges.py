"""Tests for the authoritative SIMBAD-grounded paper edges.

Two layers:
  - Pure structural tests on a synthetic edge table (no network).
  - If the real data/twa/paper_edges.parquet exists, validate it has none of the
    v0.1 over-matching pathology (e.g. one paper linked to ALL sources) and that
    source->paper->source is an inverse.
"""
from __future__ import annotations

from pathlib import Path
import json

import pandas as pd
import pytest

from astro_pipeline.models.paper import PaperEdgeTable
from astro_pipeline.sources import simbad_refs
from astro_pipeline.sources.simbad_refs import (
    PaperRecord,
    SourcePapers,
    bibcode_to_arxiv,
    build_paper_edges,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "twa"
REAL_EDGES = DATA_DIR / "paper_edges.parquet"


def test_synthetic_edges_bidirectional():
    e = PaperEdgeTable()
    e.df = pd.DataFrame([
        {"source_id": "111", "arxiv_id": "1806.05059", "bibcode": "2018arXiv180605059X"},
        {"source_id": "222", "arxiv_id": "1806.05059", "bibcode": "2018arXiv180605059X"},
        {"source_id": "111", "arxiv_id": "1904.07193", "bibcode": "2019AJ....157..234S"},
    ])
    # source -> papers -> source is an inverse
    papers_111 = set(e.get_papers("111"))
    for p in papers_111:
        assert "111" in set(e.get_sources(p))


def test_synthetic_no_overmatching():
    """The v0.1 bug: a paper linked to every source. This synthetic table must
    NOT have that property, and we assert the guard explicitly."""
    e = PaperEdgeTable()
    e.df = pd.DataFrame([
        {"source_id": "111", "arxiv_id": "1806.05059", "bibcode": "2018arXiv180605059X"},
        {"source_id": "222", "arxiv_id": "1904.07193", "bibcode": "2019AJ....157..234S"},
    ])
    # No paper should be linked to every source.
    n_sources = e.df["source_id"].nunique()
    for aid in e.df["arxiv_id"].unique():
        assert len(e.get_sources(aid)) < n_sources


def test_simbad_source_checkpoint_resumes_and_records_zero_refs(
    monkeypatch, tmp_path
):
    catalog = pd.DataFrame({"Gaia": ["111", "222"]})
    cache_path = tmp_path / "simbad_refs.parquet"
    first_calls = []

    def interrupted(source_id: str) -> SourcePapers:
        first_calls.append(source_id)
        if source_id == "222":
            raise KeyboardInterrupt("stop after first source")
        return SourcePapers(
            gaia_id=source_id,
            main_id="Source 111",
            resolved=True,
            papers=[PaperRecord(bibcode="2024TEST....1A", year=2024)],
        )

    monkeypatch.setattr(simbad_refs, "get_source_bibcodes", interrupted)
    with pytest.raises(KeyboardInterrupt):
        build_paper_edges(
            catalog,
            cache_path=cache_path,
            enrich=False,
            sleep_seconds=0,
            progress_every=1,
        )

    checkpoint_path = tmp_path / "simbad_refs.checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["completed"] == ["111"]
    assert pd.read_parquet(cache_path)["gaia_id"].astype(str).tolist() == ["111"]

    resumed_calls = []

    def resumed(source_id: str) -> SourcePapers:
        resumed_calls.append(source_id)
        # A successful query with no object/references is still complete.
        return SourcePapers(gaia_id=source_id, resolved=False)

    monkeypatch.setattr(simbad_refs, "get_source_bibcodes", resumed)
    edges, refs = build_paper_edges(
        catalog,
        cache_path=cache_path,
        enrich=False,
        sleep_seconds=0,
        progress_every=1,
    )

    assert first_calls == ["111", "222"]
    assert resumed_calls == ["222"]
    assert len(edges) == 1
    assert refs["gaia_id"].astype(str).tolist() == ["111"]
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["completed"] == ["111", "222"]
    assert checkpoint["failed"] == {}

    monkeypatch.setattr(
        simbad_refs,
        "get_source_bibcodes",
        lambda source_id: pytest.fail(f"completed source was queried again: {source_id}"),
    )
    build_paper_edges(
        catalog,
        cache_path=cache_path,
        enrich=False,
        sleep_seconds=0,
    )


def test_simbad_failure_remains_retryable(monkeypatch, tmp_path):
    catalog = pd.DataFrame({"Gaia": ["111"]})
    cache_path = tmp_path / "simbad_refs.parquet"
    monkeypatch.setattr(
        simbad_refs,
        "get_source_bibcodes",
        lambda source_id: SourcePapers(gaia_id=source_id, error="service busy"),
    )

    _, refs = build_paper_edges(
        catalog,
        cache_path=cache_path,
        enrich=False,
        sleep_seconds=0,
    )
    checkpoint_path = tmp_path / "simbad_refs.checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert refs.attrs["source_progress"]["failed"] == 1
    assert checkpoint["completed"] == []
    assert checkpoint["failed"] == {"111": "service busy"}

    calls = []
    monkeypatch.setattr(
        simbad_refs,
        "get_source_bibcodes",
        lambda source_id: calls.append(source_id)
        or SourcePapers(gaia_id=source_id, resolved=False),
    )
    _, refs = build_paper_edges(
        catalog,
        cache_path=cache_path,
        enrich=False,
        sleep_seconds=0,
    )
    assert calls == ["111"]
    assert refs.attrs["source_progress"]["failed"] == 0
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["completed"] == ["111"]
    assert checkpoint["failed"] == {}


def test_enrichment_checkpoint_preserves_sources_outside_current_subset(
    monkeypatch, tmp_path
):
    cache_path = tmp_path / "simbad_refs.parquet"
    rows = [
        {
            "gaia_id": "111",
            "main_id": "Target",
            "bibcode": f"2024TEST{i:04d}A",
            "arxiv_id": "",
            "title": "",
            "year": 2024,
            "journal": "TEST",
            "doi": "",
        }
        for i in range(26)
    ]
    rows.append(
        {
            "gaia_id": "999",
            "main_id": "Outside subset",
            "bibcode": "2023KEEP....1A",
            "arxiv_id": "",
            "title": "Keep this row",
            "year": 2023,
            "journal": "TEST",
            "doi": "",
        }
    )
    pd.DataFrame(rows).to_parquet(cache_path, index=False)
    (tmp_path / "simbad_refs.checkpoint.json").write_text(
        json.dumps({"completed": ["111", "999"], "failed": {}}),
        encoding="utf-8",
    )
    calls = 0

    def interrupted_enrichment(bibcode, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 26:
            raise KeyboardInterrupt("interrupt after enrichment checkpoint")
        return PaperRecord(bibcode=bibcode, title="Enriched")

    monkeypatch.setattr(simbad_refs, "enrich_bibcode", interrupted_enrichment)
    with pytest.raises(KeyboardInterrupt):
        build_paper_edges(
            pd.DataFrame({"Gaia": ["111"]}),
            cache_path=cache_path,
            enrich=True,
            resolve_doi=False,
            sleep_seconds=0,
        )

    cached = pd.read_parquet(cache_path)
    assert set(cached["gaia_id"].astype(str)) == {"111", "999"}
    outside = cached[cached["gaia_id"].astype(str) == "999"]
    assert outside.iloc[0]["title"] == "Keep this row"


# ---- Real-data validation (skipped if paper_edges.parquet not built/legacy) ----
def _is_valid_edges(path: Path) -> bool:
    """True only if the edges parquet exists AND has the v0.2 bibcode schema
    (the old hardcoded demo file lacks a 'bibcode' column and must be skipped)."""
    if not path.exists():
        return False
    try:
        cols = pd.read_parquet(path, columns=None).columns
    except Exception:
        return False
    return "bibcode" in cols and "source_id" in cols


@pytest.mark.skipif(not _is_valid_edges(REAL_EDGES), reason="v0.2 paper_edges.parquet not built yet")
class TestRealEdges:
    @pytest.fixture(scope="class")
    @staticmethod
    def edges():
        return pd.read_parquet(REAL_EDGES)

    def test_has_required_columns(self, edges):
        for col in ("source_id", "bibcode"):
            assert col in edges.columns, f"missing {col}"

    def test_no_overmatching(self, edges):
        """Guard against the v0.1 over-matching bug.

        In v0.1, 94 papers were each matched to ALL sources via substring
        matching. A correct SIMBAD-grounded table may still have a FEW
        high-fanout papers (a genuine census paper like 'A Census of the TW Hya
        Association' legitimately cites every member), but the *median* paper
        should cite only a handful of sources. We assert that most papers are
        not universal-matches.
        """
        n_sources = edges["source_id"].nunique()
        per_paper = edges.groupby("bibcode")["source_id"].nunique()
        # The v0.1 bug had ~all papers matching all sources. Here we require
        # that fewer than 20% of papers are "universal" (citing >= 90% of the
        # catalog) — census papers are rare, substring-matching artifacts are not.
        n_universal = int((per_paper >= 0.9 * n_sources).sum())
        frac_universal = n_universal / len(per_paper)
        assert frac_universal < 0.2, (
            f"{n_universal}/{len(per_paper)} ({frac_universal:.0%}) of papers cite "
            f">=90% of sources — looks like over-matching, not curation"
        )

    def test_bidirectional_inverse(self, edges):
        """For every (source, paper), looking the paper up must include the source."""
        for _, row in edges.iterrows():
            sid, bib = row["source_id"], row["bibcode"]
            matched = edges.loc[edges["bibcode"] == bib, "source_id"].astype(str).tolist()
            assert str(sid) in matched

    def test_arxiv_ids_parse_when_present(self, edges):
        """Any arXiv-preprint bibcode must parse to the same arxiv_id stored."""
        arxiv_bibs = edges[edges["bibcode"].astype(str).str.contains("arXiv", case=False, na=False)]
        if "arxiv_id" in edges.columns:
            for _, row in arxiv_bibs.iterrows():
                parsed = bibcode_to_arxiv(str(row["bibcode"]))
                stored = str(row["arxiv_id"] or "")
                if stored and parsed:
                    assert parsed == stored, f"{row['bibcode']}: parsed {parsed} != stored {stored}"

    def test_reasonable_paper_count(self, edges):
        """Sanity: more than 1 edge and fewer than a pathological 10k."""
        assert 1 < len(edges) < 10000
