"""Offline regressions for intake input-row provenance."""
from __future__ import annotations

import hashlib
import json

import pandas as pd

from astro_pipeline.intake import (
    describe_input_contract,
    resolve_members,
    resolve_members_with_provenance,
)


def test_id_catalog_preserves_bytes_rows_duplicates_and_large_ids(tmp_path):
    raw = (
        b"Gaia,note,weight\n"
        b"9007199254740993,first,1.5\n"
        b"9007199254740993,duplicate,2.5\n"
        b",invalid,3.5\n"
        b"9007199254740995,missing,4.5\n"
        b"9007199254740997,excluded,5.5\n"
    )
    first_path = tmp_path / "first-name.csv"
    second_path = tmp_path / "renamed.csv"
    first_path.write_bytes(raw)
    second_path.write_bytes(raw)
    calls: list[list[str]] = []

    def fetch_ids(source_ids: list[str]) -> pd.DataFrame:
        calls.append(source_ids.copy())
        return pd.DataFrame(
            {
                "source_id": ["9007199254740993"],
                "ra": [10.0],
                "dec": [20.0],
                "parallax": [5.0],
            }
        )

    preflight = describe_input_contract(
        catalog=first_path,
        limit=4,
        id_fetcher=fetch_ids,
    )
    assert calls == []
    resolution = resolve_members_with_provenance(
        catalog=first_path,
        limit=4,
        id_fetcher=fetch_ids,
    )
    renamed = resolve_members_with_provenance(
        catalog=second_path,
        limit=4,
        id_fetcher=fetch_ids,
    )

    assert calls == [
        ["9007199254740993", "9007199254740995"],
        ["9007199254740993", "9007199254740995"],
    ]
    assert resolution.raw_input == raw
    assert first_path.read_bytes() == raw
    assert set(tmp_path.iterdir()) == {first_path, second_path}
    assert resolution.original_name == "first-name.csv"
    assert renamed.original_name == "renamed.csv"
    assert preflight == resolution.contract
    assert resolution.contract == renamed.contract
    assert "first-name.csv" not in json.dumps(resolution.contract)
    assert resolution.contract == {
        "release": "dr3",
        "input_mode": "catalog",
        "limit": 4,
        "selector": {
            "csv_sha256": hashlib.sha256(raw).hexdigest(),
            "column_mapping": {"source_id": "Gaia"},
        },
    }

    assert resolution.input_rows["input_row_id"].tolist() == [0, 1, 2, 3, 4]
    assert resolution.input_rows["ordinal"].tolist() == [0, 1, 2, 3, 4]
    assert resolution.input_rows["status"].tolist() == [
        "matched",
        "matched",
        "invalid",
        "id_not_found",
        "not_selected_by_limit",
    ]
    first_raw_row = json.loads(resolution.input_rows.loc[0, "raw_json"])
    assert first_raw_row == {
        "Gaia": "9007199254740993",
        "note": "first",
        "weight": 1.5,
    }
    assert json.loads(resolution.input_rows.loc[2, "raw_json"])["Gaia"] is None

    # Duplicate successful requests remain duplicate sample rows in input order.
    assert resolution.members["input_row_id"].tolist() == [0, 1]
    assert resolution.members["Gaia"].tolist() == [
        "9007199254740993",
        "9007199254740993",
    ]
    assert "note" not in resolution.members.columns
    assert "weight" not in resolution.members.columns
    assert resolution.matches["status"].tolist() == resolution.input_rows[
        "status"
    ].tolist()
    assert resolution.matches.loc[0, "source_id"] == "9007199254740993"
    assert resolution.matches.loc[1, "match_candidate_count"] == 1
    assert pd.isna(resolution.matches.loc[3, "source_id"])
    assert resolution.matches.loc[3, "match_candidate_count"] == 0


def test_describe_input_contract_is_read_only_for_non_csv_routes(monkeypatch):
    from astro_pipeline import intake

    def forbidden_resolution(*args, **kwargs):
        raise AssertionError("contract preflight must not resolve or query")

    monkeypatch.setattr(intake, "_from_cone", forbidden_resolution)

    def forbidden_id_fetcher(source_ids: list[str]) -> pd.DataFrame:
        raise AssertionError(f"contract preflight called id_fetcher: {source_ids}")

    assert describe_input_contract(
        cone=(10.0, -20.0, 0.5),
        limit=7,
        id_fetcher=forbidden_id_fetcher,
    ) == {
        "release": "dr3",
        "input_mode": "cone",
        "limit": 7,
        "selector": {"ra_deg": 10.0, "dec_deg": -20.0, "radius_deg": 0.5},
    }
    assert describe_input_contract(name="TW Hya") == {
        "release": "dr3",
        "input_mode": "name",
        "limit": None,
        "selector": {"name": "TW Hya"},
    }


def test_resolve_members_dataframe_wrapper_keeps_repeated_id_rows(tmp_path):
    catalog = tmp_path / "ids.csv"
    catalog.write_text(
        "Gaia,label\n9007199254740993,a\n9007199254740993,b\n",
        encoding="utf-8",
    )

    def fetch_ids(source_ids: list[str]) -> pd.DataFrame:
        assert source_ids == ["9007199254740993"]
        return pd.DataFrame(
            {"source_id": ["9007199254740993"], "ra": [1.0], "dec": [2.0]}
        )

    members = resolve_members(catalog=catalog, id_fetcher=fetch_ids)

    assert members["input_row_id"].tolist() == [0, 1]
    assert members["Gaia"].tolist() == [
        "9007199254740993",
        "9007199254740993",
    ]
    assert "label" not in members.columns


def test_position_catalog_tracks_invalid_unmatched_and_limit_without_id_fetcher(
    monkeypatch, tmp_path
):
    from astro_pipeline import intake

    catalog = tmp_path / "positions.csv"
    catalog.write_text(
        "RAICRS,DEICRS,note\n"
        "10.0,20.0,matched\n"
        "not-ra,21.0,invalid\n"
        "12.0,22.0,unmatched\n"
        "13.0,23.0,excluded\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    class FakeGaiaSource:
        def __init__(self, *args, **kwargs):
            pass

        def crossmatch_positions(self, positions, **kwargs):
            captured["positions"] = positions.copy()
            captured["kwargs"] = kwargs
            return pd.DataFrame(
                {
                    "input_row_id": [0],
                    "source_id": ["1234567890123456789"],
                    "ra": [10.0],
                    "dec": [20.0],
                    "match_sep_arcsec": [0.25],
                    "match_candidate_count": [2],
                }
            )

    monkeypatch.setattr(intake, "GaiaSource", FakeGaiaSource)

    def forbidden_id_fetcher(source_ids: list[str]) -> pd.DataFrame:
        raise AssertionError(f"coordinate mode must not call id_fetcher: {source_ids}")

    resolution = resolve_members_with_provenance(
        catalog=catalog,
        limit=3,
        id_fetcher=forbidden_id_fetcher,
    )

    positions = captured["positions"]
    assert isinstance(positions, pd.DataFrame)
    assert positions["input_row_id"].tolist() == [0, 1, 2]
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["row_id_col"] == "input_row_id"
    assert kwargs["radius_arcsec"] == 1.0
    assert kwargs["batch_size"] == 25

    assert resolution.input_rows["status"].tolist() == [
        "matched",
        "invalid",
        "unmatched",
        "not_selected_by_limit",
    ]
    assert resolution.matches["status"].tolist() == resolution.input_rows[
        "status"
    ].tolist()
    assert resolution.matches.loc[0, "match_method"] == "position"
    assert resolution.matches.loc[0, "match_sep_arcsec"] == 0.25
    assert resolution.matches.loc[0, "match_candidate_count"] == 2
    assert resolution.matches.loc[2, "match_candidate_count"] == 0
    assert pd.isna(resolution.matches.loc[1, "match_method"])

    assert resolution.members["input_row_id"].tolist() == [0]
    assert resolution.members["Gaia"].tolist() == ["1234567890123456789"]
    assert "note" not in resolution.members.columns
    assert json.loads(resolution.input_rows.loc[3, "raw_json"])["note"] == "excluded"
    assert resolution.contract["selector"] == {
        "csv_sha256": hashlib.sha256(catalog.read_bytes()).hexdigest(),
        "column_mapping": {"ra": "RAICRS", "dec": "DEICRS"},
        "match_radius_arcsec": 1.0,
    }
    assert describe_input_contract(catalog=catalog, limit=3) == resolution.contract
    assert resolution.resolved_input["catalog_kind"] == "position"


def test_provenance_api_returns_all_unmatched_rows_without_raising(
    monkeypatch, tmp_path
):
    from astro_pipeline import intake

    catalog = tmp_path / "unmatched.csv"
    catalog.write_text("RAICRS,DEICRS\n10.0,20.0\n", encoding="utf-8")

    class FakeGaiaSource:
        def __init__(self, *args, **kwargs):
            pass

        def crossmatch_positions(self, positions, **kwargs):
            return pd.DataFrame(
                columns=[
                    "input_row_id",
                    "source_id",
                    "match_sep_arcsec",
                    "match_candidate_count",
                ]
            )

    monkeypatch.setattr(intake, "GaiaSource", FakeGaiaSource)

    resolution = resolve_members_with_provenance(catalog=catalog)

    assert resolution.members.empty
    assert resolution.input_rows["status"].tolist() == ["unmatched"]
    assert resolution.matches["status"].tolist() == ["unmatched"]
