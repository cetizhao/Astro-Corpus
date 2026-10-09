"""Offline tests for Gaia release profiles and live-capability discovery."""
from __future__ import annotations

import io

import pandas as pd
import pytest
from astropy.table import Table

from astro_pipeline.gaia_archive import (
    describe_gaia_table,
    list_gaia_datalink_products,
    probe_gaia_release,
)
from astro_pipeline.gaia_releases import get_gaia_release_profile


def test_dr3_profile_is_stable_and_dr4_is_deliberately_provisional():
    dr3 = get_gaia_release_profile("Gaia DR3")
    dr4 = get_gaia_release_profile("gaiadr4")

    assert dr3.key == "dr3"
    assert dr3.tap_schema == "gaiadr3"
    assert dr3.source_id_partition_confirmed is True
    assert dr4.key == "dr4"
    assert dr4.reference_epoch == 2017.5
    assert dr4.tap_schema is None
    assert dr4.source_id_partition is None
    assert dr4.source_id_partition_confirmed is False
    assert "all_source_astrometry" in dr4.expected_source_tables
    assert "EPOCH_ASTROMETRY" in dr4.datalink_products


def test_probe_discovers_dr4_schema_and_expected_tables():
    queries = []

    def fake_query(adql: str) -> pd.DataFrame:
        queries.append(adql)
        if "TAP_SCHEMA.schemas" in adql:
            return pd.DataFrame(
                {
                    "schema_name": ["gaiadr3", "gaiadr4"],
                    "description": ["Gaia DR3", "Gaia DR4"],
                }
            )
        return pd.DataFrame(
            {
                "schema_name": ["gaiadr4", "gaiadr4"],
                "table_name": [
                    "gaiadr4.gaia_source",
                    "gaiadr4.all_source_astrometry",
                ],
                "description": ["quality subset", "all astrometry"],
            }
        )

    result = probe_gaia_release("dr4", fake_query)

    assert result["archive_reachable"] is True
    assert result["published_in_tap"] is True
    assert result["resolved_schema"] == "gaiadr4"
    availability = {
        item["table"]: item["available"] for item in result["expected_tables"]
    }
    assert availability["gaia_source"] is True
    assert availability["all_source_astrometry"] is True
    assert availability["all_source_photometry"] is False
    assert any("schema_name = 'gaiadr4'" in query for query in queries)


def test_probe_does_not_guess_unpublished_dr4_schema():
    def fake_query(adql: str) -> pd.DataFrame:
        assert "TAP_SCHEMA.schemas" in adql
        return pd.DataFrame(
            {"schema_name": ["gaiadr3"], "description": ["Gaia DR3"]}
        )

    result = probe_gaia_release("dr4", fake_query)
    assert result["archive_reachable"] is True
    assert result["published_in_tap"] is False
    assert result["resolved_schema"] is None
    assert "No TAP schema" in result["warnings"][0]


def test_probe_can_use_unique_live_release_description_for_unexpected_schema_name():
    def fake_query(adql: str) -> pd.DataFrame:
        if "TAP_SCHEMA.schemas" in adql:
            return pd.DataFrame(
                {
                    "schema_name": ["future_gaia_catalogue"],
                    "description": ["Official Gaia DR4 source tables"],
                }
            )
        assert "schema_name = 'future_gaia_catalogue'" in adql
        return pd.DataFrame(
            {
                "schema_name": ["future_gaia_catalogue"],
                "table_name": ["future_gaia_catalogue.gaia_source"],
                "description": ["main source"],
            }
        )

    result = probe_gaia_release("dr4", fake_query)
    assert result["resolved_schema"] == "future_gaia_catalogue"
    assert result["published_in_tap"] is True


def test_describe_table_uses_discovered_schema():
    def fake_query(adql: str) -> pd.DataFrame:
        if "TAP_SCHEMA.schemas" in adql:
            return pd.DataFrame(
                {"schema_name": ["gaiadr4"], "description": ["Gaia DR4"]}
            )
        assert "table_name = 'gaiadr4.gaia_source'" in adql
        return pd.DataFrame(
            {
                "table_name": ["gaiadr4.gaia_source"],
                "column_name": ["source_id"],
                "datatype": ["long"],
                "arraysize": [None],
                "unit": [None],
                "ucd": ["meta.id"],
                "description": ["source identifier"],
            }
        )

    result = describe_gaia_table("dr4", "gaia_source", fake_query)
    assert result["qualified_table"] == "gaiadr4.gaia_source"
    assert result["columns"][0]["column_name"] == "source_id"

    with pytest.raises(ValueError, match="belongs to Gaia DR3"):
        describe_gaia_table("dr4", "gaiadr3.gaia_source", fake_query)


def test_local_datalink_hints_do_not_import_networked_gaia_client(monkeypatch):
    import builtins

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.startswith("astroquery.gaia"):
            raise AssertionError("Local capability discovery must not import the Gaia singleton")
        return original_import(name, *args, **kwargs)

    def no_http(*args, **kwargs):
        raise AssertionError("No source ID means no live DataLink query")

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    result = list_gaia_datalink_products("dr4", http_get=no_http)
    assert result["client_metadata_loaded"] is True
    assert "SOURCE_ID" in result["linking_parameters"]
    assert all(item["archive_confirmed_for_source"] is None for item in result["client_declared_products"])


def test_unreadable_client_declarations_remain_unknown(monkeypatch, tmp_path):
    from astro_pipeline import gaia_archive

    source = tmp_path / "__init__.py"
    source.write_text("class Conf:\n    VALID_DATALINK_RETRIEVAL_TYPES = unknown_function()\n")

    class LocalDistribution:
        def locate_file(self, path):
            return source

    monkeypatch.setattr(gaia_archive, "distribution", lambda name: LocalDistribution())
    result = list_gaia_datalink_products("dr4")
    assert result["client_metadata_loaded"] is False
    assert all(item["accepted_by_installed_astroquery"] is None for item in result["client_declared_products"])
    assert "unknown" in result["warnings"][-1]


def test_datalink_product_list_separates_client_hint_from_live_confirmation():
    table = Table(
        {
            "access_url": [
                "https://gea.esac.esa.int/data-server/data?"
                "RETRIEVAL_TYPE=EPOCH_ASTROMETRY&ID=123"
            ],
            "description": ["epoch astrometry"],
            "content_type": ["application/fits"],
            "semantics": ["#this"],
        }
    )
    buffer = io.BytesIO()
    table.write(buffer, format="votable")

    class FakeResponse:
        content = buffer.getvalue()

        def raise_for_status(self):
            return None

    seen = {}

    def fake_get(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return FakeResponse()

    result = list_gaia_datalink_products(
        "dr4", source_id="123", http_get=fake_get
    )
    products = {
        item["retrieval_type"]: item
        for item in result["client_declared_products"]
    }
    assert products["EPOCH_ASTROMETRY"]["accepted_by_installed_astroquery"] is True
    assert products["EPOCH_ASTROMETRY"]["archive_confirmed_for_source"] is True
    assert products["EPOCH_IMAGE"]["archive_confirmed_for_source"] is False
    assert result["live_links"][0]["retrieval_type"] == "EPOCH_ASTROMETRY"
    assert seen["params"]["RELEASE"] == "Gaia DR4"
