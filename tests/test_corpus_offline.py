"""Offline tests for the bidirectional corpus API.

Uses a small synthetic data dir (catalog + paper_edges + cached survey parquets)
so the tests run with zero network. Verifies:
  - lookup_source returns truthful multimodal availability + papers.
  - lookup_paper returns the sources for a paper (inverse of lookup_source).
  - a missing survey degrades gracefully (status="not_in_catalog"/"unavailable").
  - corrupt (HTML) cached parquets are ignored, not loaded.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from astro_pipeline.corpus import Corpus


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    catalog = pd.DataFrame({
        "Gaia": ["111", "222", "333"],
        "RAICRS": [10.0, 20.0, 30.0],
        "DEICRS": [-10.0, -20.0, -30.0],
        "Plx_gdr3": [8.0, 7.0, 6.0],
        "e_Plx_gdr3": [0.1, 0.1, 0.1],
        "RUWE_gdr3": [1.0, 1.0, 1.0],
        "Gmag_gdr3": [11.0, 12.0, 13.0],
    })
    catalog_path = tmp_path / "members.csv"
    catalog.to_csv(catalog_path, index=False)

    # The Corpus data_dir is the "twa" directory itself.
    twa = tmp_path / "twa"
    twa.mkdir()
    (twa / "catalogs").mkdir()

    # paper edges: 111 in paper A+B, 222 in paper A only.
    edges = pd.DataFrame([
        {"source_id": "111", "arxiv_id": "1806.05059", "bibcode": "2018arXiv180605059X",
         "title": "Paper A", "year": 2018, "journal": "arXiv", "doi": "", "main_id": "S1"},
        {"source_id": "111", "arxiv_id": "1904.07193", "bibcode": "2019AJ....157..234S",
         "title": "Paper B", "year": 2019, "journal": "AJ", "doi": "10.1/xyz", "main_id": "S1"},
        {"source_id": "222", "arxiv_id": "1806.05059", "bibcode": "2018arXiv180605059X",
         "title": "Paper A", "year": 2018, "journal": "arXiv", "doi": "", "main_id": "S2"},
    ])
    edges.to_parquet(twa / "paper_edges.parquet", index=False)

    # cached Gaia XP: 111 and 222 have XP.
    xp = pd.DataFrame({"source_id": ["111", "222"], "ra": [10.0, 20.0], "dec": [-10.0, -20.0],
                       "phot_g_mean_mag": [11.0, 12.0], "has_xp_sampled": ["true", "true"]})
    xp.to_parquet(twa / "catalogs" / "twa_gaia_xp.parquet", index=False)

    # cached LAMOST keyed on gaiadr3.
    lm = pd.DataFrame({"obsid": [80000001], "gaiadr3": ["111"], "snrg": [20.0], "snrr": [18.0],
                       "raj2000": [10.0], "dej2000": [-10.0]})
    lm.to_parquet(twa / "catalogs" / "twa_lamost.parquet", index=False)

    # Return the twa dir (the Corpus data_dir), plus the catalog at tmp_path.
    return twa


def _corpus(data_dir: Path) -> Corpus:
    return Corpus(data_dir=data_dir, catalog_path=data_dir.parent / "members.csv", offline=True)


def test_lookup_source_resolves(data_dir: Path):
    c = _corpus(data_dir)
    res = c.lookup_source("111")
    assert res["resolved"] is True
    assert res["gaia_release"] == "dr3"
    assert res["astrometry"]["RAICRS"] == 10.0


def test_lookup_source_multimodal_truthful(data_dir: Path):
    """Source 111 has XP + LAMOST (cached) but no SDSS."""
    c = _corpus(data_dir)
    mm = c.lookup_source("111")["multimodal"]
    assert mm["gaia_xp"]["available"] is True
    assert mm["gaia_xp"]["status"] == "cached"
    assert mm["lamost"]["available"] is True
    assert mm["sdss"]["available"] is False
    assert mm["sdss"]["status"] == "unavailable"  # no cached sdss at all


def test_lookup_source_no_multimodal_for_333(data_dir: Path):
    """Source 333 has no XP, no LAMOST."""
    c = _corpus(data_dir)
    mm = c.lookup_source("333")["multimodal"]
    assert mm["gaia_xp"]["available"] is False
    assert mm["gaia_xp"]["status"] == "not_in_catalog"
    assert mm["lamost"]["available"] is False


def test_lookup_source_papers(data_dir: Path):
    c = _corpus(data_dir)
    papers = c.lookup_source("111")["papers"]
    titles = {p["title"] for p in papers}
    assert titles == {"Paper A", "Paper B"}
    pa = next(p for p in papers if p["title"] == "Paper A")
    assert pa["arxiv_id"] == "1806.05059"
    # arXiv id present -> arXiv URL wins over DOI/bibcode.
    assert pa["url"] == "https://arxiv.org/abs/1806.05059"
    pb = next(p for p in papers if p["title"] == "Paper B")
    assert pb["url"] == "https://arxiv.org/abs/1904.07193"
    assert pb["doi"] == "10.1/xyz"


def test_paper_url_prefers_doi_when_no_arxiv(data_dir: Path):
    """A journal-only paper (no arXiv id) should fall back to DOI then bibcode."""
    c = _corpus(data_dir)
    # Insert a journal-only edge for 333.
    edge_df = c.paper_edges.df
    new_row = pd.DataFrame([{
        "source_id": "333", "arxiv_id": "", "bibcode": "2019MNRAS.487..100X",
        "title": "Journal only", "year": 2019, "journal": "MNRAS",
        "doi": "10.1093/j/100", "main_id": "S3",
    }])
    edge_df = pd.concat([edge_df, new_row], ignore_index=True)
    c.paper_edges.df = edge_df
    papers = c.lookup_source("333")["papers"]
    assert len(papers) == 1
    assert papers[0]["url"] == "https://doi.org/10.1093/j/100"


def test_lookup_source_unknown_id(data_dir: Path):
    c = _corpus(data_dir)
    res = c.lookup_source("999999")
    assert res["resolved"] is False
    assert "not in catalog" in res["error"]


def test_lookup_paper_by_arxiv_inverse(data_dir: Path):
    """Paper A is linked to sources 111 and 222."""
    c = _corpus(data_dir)
    res = c.lookup_paper("1806.05059")
    assert res["resolved"] is True
    src_ids = sorted(s["gaia_id"] for s in res["sources"])
    assert src_ids == ["111", "222"]
    assert res["paper"]["title"] == "Paper A"


def test_lookup_paper_by_bibcode(data_dir: Path):
    c = _corpus(data_dir)
    res = c.lookup_paper("2019AJ....157..234S")
    assert res["resolved"] is True
    assert [s["gaia_id"] for s in res["sources"]] == ["111"]


def test_lookup_paper_by_title_substring(data_dir: Path):
    c = _corpus(data_dir)
    res = c.lookup_paper("Paper B")
    assert res["resolved"] is True
    assert [s["gaia_id"] for s in res["sources"]] == ["111"]


def test_lookup_paper_unknown(data_dir: Path):
    c = _corpus(data_dir)
    res = c.lookup_paper("nonexistent")
    assert res["resolved"] is False


def test_bidirectional_inverse(data_dir: Path):
    """source->papers->source must return the original source (the inverse check)."""
    c = _corpus(data_dir)
    src = c.lookup_source("111")
    for paper in src["papers"]:
        arxiv = paper["arxiv_id"]
        if not arxiv:
            continue
        back = c.lookup_paper(arxiv)
        assert "111" in [s["gaia_id"] for s in back["sources"]], f"111 missing from {arxiv}"


def test_summary(data_dir: Path):
    c = _corpus(data_dir)
    s = c.summary()
    assert s["gaia_release"] == "dr3"
    assert s["catalog_sources"] == 3
    assert s["paper_edges"] == 3
    assert s["unique_papers"] == 2
    assert s["gaia_xp_in_cache"] == 2
    assert s["lamost_in_cache"] == 1


def test_corrupt_html_cache_ignored(data_dir: Path):
    """An HTML-error-page cached parquet must not be loaded."""
    bad = pd.DataFrame({"<html><head>": ["junk"]})
    bad.to_parquet(data_dir / "catalogs" / "twa_sdss.parquet", index=False)
    c = _corpus(data_dir)
    # sdss should be unavailable, not loaded as junk.
    assert c._cached_surveys.get("sdss") is None
