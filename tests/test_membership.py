"""Offline regression tests for the literature membership workflow."""
from __future__ import annotations

from collections import OrderedDict

import pandas as pd
import pytest
from astropy.table import Column, Table

from astro_pipeline import membership


class _Resource:
    def __init__(
        self,
        description: str,
        *,
        name: object = None,
        resource_id: object = None,
    ) -> None:
        self.description = description
        self.name = name
        self.ID = resource_id


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


def test_search_skips_invalid_keys_and_uses_valid_resource_metadata(monkeypatch):
    calls: list[str] = []

    def fake_find(query, **kwargs):
        calls.append(query)
        if query != "CrA":
            return OrderedDict()
        return OrderedDict(
            {
                None: _Resource(
                    "Corona Australis members",
                    name="J/AJ/163/64",
                ),
                123: _Resource(
                    "Another valid catalogue",
                    name="not a catalogue id",
                    resource_id="J/AJ/999/1",
                ),
                "not a valid id": _Resource("Invalid search resource"),
            }
        )

    monkeypatch.setattr(membership.Vizier, "find_catalogs", fake_find)
    out = membership.search_membership_catalogs("CrA", max_results=5)

    assert calls[-1] == "CrA"
    assert {row["catalog_id"] for row in out["candidates"]} == {
        "J/AJ/163/64",
        "J/AJ/999/1",
    }
    assert all("None" not in row["url"] for row in out["candidates"])


def test_coordinate_detection_prefers_original_numeric_icrs_columns():
    table = Table()
    table["_RA.icrs"] = [1.0, 2.0]
    table["_DE.icrs"] = [3.0, 4.0]
    table["RA_ICRS"] = [10.0, 20.0]
    table["DE_ICRS"] = [-30.0, -40.0]
    for name in table.colnames:
        table[name].unit = "deg"

    detected = membership._detect_columns(table)

    assert detected["coordinate_columns"] == ["RA_ICRS", "DE_ICRS"]
    assert detected["coordinate_descriptor"] == {
        "ra_column": "RA_ICRS",
        "dec_column": "DE_ICRS",
        "ra_unit": "deg",
        "dec_unit": "deg",
        "ra_dtype": "float64",
        "dec_dtype": "float64",
        "match_unit": "deg",
        "frame": "ICRS",
        "reference_epoch": None,
        "epoch_source": "not_declared",
        "selection_reason": "numeric columns with explicit angular units",
    }


def test_coordinate_detection_rejects_sexagesimal_strings_without_parser():
    table = Table(
        {
            "RAJ2000": ["04 30 00", "04 31 00"],
            "DEJ2000": ["+22 00 00", "+23 00 00"],
        }
    )
    table["RAJ2000"].unit = "hourangle"
    table["DEJ2000"].unit = "deg"

    detected = membership._detect_columns(table)

    assert detected["coordinate_columns"] is None
    assert detected["gaia_match_ready"] is False
    assert detected["coordinate_status"] == (
        "unsupported_or_missing_numeric_angular_pair"
    )


def test_gaia_identifier_detection_excludes_astrometry_and_keeps_older_ids():
    table = _member_table()
    columns = [
        Column([1.0, 2.0], name="GaiaDR3PMRA"),
        Column([12.0, 13.0], name="GaiaDR3Gmag"),
        Column([123, 456], name="GaiaEDR3"),
        Column([789, 101], name="GaiaDR2"),
    ]
    descriptions = [
        "Gaia DR3 proper motion in right ascension",
        "Gaia DR3 G magnitude",
        "Gaia EDR3 source identifier",
        "Gaia DR2 source identifier",
    ]
    for column, description in zip(columns, descriptions):
        column.description = description
        table.add_column(column)

    detected = membership._detect_columns(table)

    assert detected["gaia_dr3_id_columns"] == []
    assert detected["older_gaia_id_columns"] == ["GaiaEDR3", "GaiaDR2"]
    assert detected["older_gaia_id_evidence"] == [
        {"column": "GaiaEDR3", "release": "edr3"},
        {"column": "GaiaDR2", "release": "dr2"},
    ]
    assert detected["match_strategy"] == "position_to_gaia_dr3"


def test_position_match_converts_units_and_marks_invalid_rows():
    original = pd.DataFrame(
        {
            "membership_catalog_row": [0, 1],
            "RA_ICRS": [1.0, float("nan")],
            "DE_ICRS": [0.5, 0.1],
        }
    )

    class FakeGaia:
        received: pd.DataFrame

        def crossmatch_positions(self, frame, *args, **kwargs):
            self.received = frame.copy()
            return pd.DataFrame(
                {
                    "membership_catalog_row": [0],
                    "source_id": ["1234567890123456789"],
                    "ra": [57.2958],
                    "dec": [28.6479],
                    "match_sep_arcsec": [0.1],
                    "match_candidate_count": [1],
                }
            )

    gaia = FakeGaia()
    matched, unmatched = membership._match_by_position(
        original,
        gaia=gaia,
        ra_col="RA_ICRS",
        dec_col="DE_ICRS",
        ra_unit="rad",
        dec_unit="rad",
        radius_arcsec=3.0,
        batch_size=25,
    )

    assert gaia.received["RA_ICRS"].iloc[0] == pytest.approx(57.2957795)
    assert gaia.received["DE_ICRS"].iloc[0] == pytest.approx(28.6478898)
    assert matched["member_RA_ICRS"].iloc[0] == 1.0
    assert unmatched["membership_catalog_row"].tolist() == [1]
    assert unmatched["membership_match_status"].tolist() == [
        "invalid_coordinates"
    ]


def test_preview_and_resolve_share_coordinate_descriptor(monkeypatch):
    table = Table()
    table["_RA.icrs"] = [67.0, 68.0]
    table["_DE.icrs"] = [-38.0, -37.0]
    table["RA_ICRS"] = [67.1, 68.1]
    table["DE_ICRS"] = [-38.1, -37.1]
    table["GaiaEDR3"] = [111, 222]
    for name in ("_RA.icrs", "_DE.icrs", "RA_ICRS", "DE_ICRS"):
        table[name].unit = "deg"
    table["GaiaEDR3"].description = "Gaia EDR3 source identifier"
    table.meta["description"] = "Members of Corona Australis"

    _FakeVizier.tables = _TableList({"J/AJ/163/64/table1": table})
    monkeypatch.setattr(membership, "Vizier", _FakeVizier)

    class FakeGaia:
        def __init__(self, *args, **kwargs):
            pass

        def crossmatch_positions(self, frame, *args, **kwargs):
            return pd.DataFrame(
                {
                    "membership_catalog_row": frame["membership_catalog_row"],
                    "source_id": ["123", "456"],
                    "ra": frame["RA_ICRS"],
                    "dec": frame["DE_ICRS"],
                    "match_sep_arcsec": [0.1, 0.2],
                    "match_candidate_count": [1, 1],
                }
            )

    monkeypatch.setattr(membership, "GaiaSource", FakeGaia)
    preview = membership.preview_membership_catalog(
        "J/AJ/163/64", table_id="J/AJ/163/64/table1", sample_size=2
    )
    resolution = membership.resolve_membership_catalog(
        "J/AJ/163/64", table_id="J/AJ/163/64/table1"
    )

    descriptor = preview["tables"][0]["coordinate_descriptor"]
    assert descriptor == resolution.coordinate_descriptor
    assert descriptor["ra_column"] == "RA_ICRS"
    assert resolution.match_method == "gaia_dr3_position"
    assert resolution.members["membership_match_ra_column"].unique().tolist() == [
        "RA_ICRS"
    ]
    assert resolution.members["membership_match_ra_unit"].unique().tolist() == [
        "deg"
    ]
    assert resolution.members["member_GaiaEDR3"].tolist() == [111, 222]


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
