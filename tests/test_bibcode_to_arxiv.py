"""Tests for SIMBAD bibcode -> arXiv id parsing (pure, no network)."""
from __future__ import annotations

import pytest

from astro_pipeline.sources.simbad_refs import (
    bibcode_to_arxiv,
    _bibcode_year,
    _bibcode_journal,
)


@pytest.mark.parametrize("bibcode,expected", [
    ("2018arXiv180605059X", "1806.05059"),   # new-style, no dot in bibcode
    ("2019arXiv190104567", "1901.04567"),    # new-style, 5-digit suffix
    ("2007arXiv0706123B", "0706.123"),       # old-style 7-digit
    ("2020arXiv200512345", "2005.12345"),    # new-style, longer
])
def test_arxiv_bibcode_parses(bibcode, expected):
    assert bibcode_to_arxiv(bibcode) == expected


@pytest.mark.parametrize("bibcode", [
    "2018A&A...620A.172Z",   # journal bibcode, no arXiv
    "2019AJ....157..234S",
    "2023MNRAS.519.2718R",
    "",
    "garbage",
    "2018",                  # too short
])
def test_non_arxiv_bibcode_returns_none(bibcode):
    assert bibcode_to_arxiv(bibcode) is None


def test_already_dotted_form():
    # Some sources emit the dotted form directly.
    assert bibcode_to_arxiv("1810.09819") == "1810.09819"


def test_bibcode_year():
    assert _bibcode_year("2018A&A...620A.172Z") == 2018
    assert _bibcode_year("2025MNRAS.528.4760L") == 2025
    assert _bibcode_year("garbage") is None
    assert _bibcode_year("1700A&A....1...1X") is None  # out of plausible range


def test_bibcode_journal():
    assert _bibcode_journal("2018A&A...620A.172Z") == "A&A"
    assert _bibcode_journal("2019AJ....157..234S") == "AJ"
    assert _bibcode_journal("2023MNRAS.519.2718R") == "MNRAS"
