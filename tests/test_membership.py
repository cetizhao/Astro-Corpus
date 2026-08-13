"""Offline regression tests for the literature membership workflow."""
from __future__ import annotations

from collections import OrderedDict

import pandas as pd
import pytest
from astropy.table import Column, Table

from astro_pipeline import membership


class _Resource:
    def __init__(self, description: str) -> None:
        self.description = description


class _TableList:
    def __init__(self, mapping: dict[str, Table]) -> None:
        self._mapping = mapping

    def keys(self):
        return self._mapping.keys()

    def __iter__(self):
        return iter(self._mapping.values())

    def __bool__(self) -> bool:
        return bool(self._mapping)


class _FakeVizier:
    tables: _TableList

    def __init__(self, *args, **kwargs) -> None:
        pass

    def get_catalogs(self, catalog_id):
        return self.tables

    def get_catalog_metadata(self, **kwargs):
        return Table({"title": ["Taurus membership"]})


def _member_table(description: str = "Members of Taurus") -> Table:
    table = Table(
        {
            "RAJ2000": [68.0, 69.0],
            "DEJ2000": [22.0, 23.0],
            "SpType": ["M4", "K7"],
            "Pmember": [0.98, 0.61],
        }
    )
    table.meta["description"] = description
    table["RAJ2000"].unit = "deg"
    table["RAJ2000"].description = "Right Ascension in decimal degrees (J2000)"
    table["DEJ2000"].unit = "deg"
    table["DEJ2000"].description = "Declination in decimal degrees (J2000)"
    table["Pmember"].description = "Membership probability"
    return table


def test_search_membership_catalogs_ranks_membership_title(monkeypatch):
    results = {
        "Taurus membership": OrderedDict(
            {
                "J/AJ/156/271": _Resource(
                    "The stellar membership of the Taurus SFR (Luhman, 2018)"
                )
            }
        ),
        "Taurus members": OrderedDict(
            {
                "J/ApJ/784/126": _Resource(
                    "Infrared photometry of all known members in Taurus"
                )
            }
        ),
    }

    def fake_find(query, **kwargs):
        return results.get(query, OrderedDict())

    monkeypatch.setattr(membership.Vizier, "find_catalogs", fake_find)
    out = membership.search_membership_catalogs("Taurus", max_results=5)

    assert out["n_candidates"] == 2
    assert out["candidates"][0]["catalog_id"] == "J/AJ/156/271"
    assert "membership" in out["candidates"][0]["relevance_signals"]
    assert out["query_kind"] == "published_membership_catalogues"


def test_preview_distinguishes_members_and_candidates(monkeypatch):
    tables = _TableList(
        {
            "J/AJ/156/271/table1": _member_table("Members of Taurus"),
            "J/AJ/156/271/Candidates": _member_table("Candidate members of Taurus"),
        }
    )

    _FakeVizier.tables = tables
    monkeypatch.setattr(membership, "Vizier", _FakeVizier)
    out = membership.preview_membership_catalog("J/AJ/156/271", sample_size=2)

    assert [table["role"] for table in out["tables"]] == ["members", "candidates"]
    member = out["tables"][0]
    assert member["coordinate_columns"] == ["RAJ2000", "DEJ2000"]
    assert member["match_strategy"] == "position_to_gaia_dr3"
    assert "Pmember" in member["membership_columns"]
    assert len(member["sample_rows"]) == 2


def test_download_requires_explicit_table_when_ambiguous(monkeypatch):
    tables = _TableList(
        {
            "X/one": _member_table("Member list A"),
            "X/two": _member_table("Member list B"),
        }
    )
    _FakeVizier.tables = tables
    monkeypatch.setattr(membership, "Vizier", _FakeVizier)

    with pytest.raises(ValueError, match="multiple plausible tables"):
        membership.resolve_membership_catalog("X")


def test_position_match_preserves_literature_columns(monkeypatch):
    original = pd.DataFrame(
        {
            "membership_catalog_row": [0, 1],
            "RAJ2000": [68.0, 69.0],
            "DEJ2000": [22.0, 23.0],
            "Pmember": [0.98, 0.61],
        }
    )

    class FakeGaia:
        def crossmatch_positions(self, *args, **kwargs):
            return pd.DataFrame(
                {
                    "membership_catalog_row": [0],
                    "source_id": ["1234567890123456789"],
                    "ra": [68.0001],
                    "dec": [22.0001],
                    "parallax": [7.0],
                    "match_sep_arcsec": [0.4],
                    "match_candidate_count": [2],
                }
            )

    matched, unmatched = membership._match_by_position(
        original,
        gaia=FakeGaia(),
        ra_col="RAJ2000",
        dec_col="DEJ2000",
        radius_arcsec=3.0,
        batch_size=500,
    )

    assert matched["Gaia"].tolist() == ["1234567890123456789"]
    assert matched["member_Pmember"].tolist() == [0.98]
    assert matched["membership_match_sep_arcsec"].tolist() == [0.4]
    assert matched["membership_match_candidate_count"].tolist() == [2]
    assert unmatched["membership_catalog_row"].tolist() == [1]


def test_detects_explicit_gaia_dr3_identifier():
    table = _member_table()
    source = Column([123, 456], name="GaiaDR3")
    source.description = "Gaia DR3 source identifier"
    table.add_column(source)

    detected = membership._detect_columns(table)

    assert detected["gaia_dr3_id_columns"] == ["GaiaDR3"]
    assert detected["match_strategy"] == "gaia_dr3_source_id"


@pytest.mark.network
def test_live_taurus_catalogue_preview():
    preview = membership.preview_membership_catalog("J/AJ/156/271", sample_size=2)
    ids = {table["table_id"] for table in preview["tables"]}
    assert "J/AJ/156/271/table1" in ids
    members = next(
        table for table in preview["tables"] if table["table_id"].endswith("/table1")
    )
    assert members["role"] == "members"
    assert members["coordinate_columns"] == ["RAJ2000", "DEJ2000"]
