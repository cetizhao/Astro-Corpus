"""Offline tests for deterministic, resumable Gaia HEALPix downloads."""
from __future__ import annotations

import json
import re

import pandas as pd
import pytest

from astro_pipeline.bulk_gaia import (
    build_page_adql,
    bulk_status,
    create_bulk_plan,
    healpix_count,
    run_bulk_plan,
    source_id_bounds,
    source_id_divisor,
)
from scripts.gaia_bulk import _root


def test_official_source_id_healpix_divisors():
    assert source_id_divisor(12) == 34_359_738_368
    assert source_id_divisor(11) == 137_438_953_472
    assert healpix_count(0) == 12
    assert healpix_count(5) == 12_288
    assert source_id_bounds(0, 0) == (0, 2**59)


def test_create_plan_is_non_downloading_and_refuses_mixed_settings(tmp_path):
    result = create_bulk_plan(
        label="tiny",
        root=tmp_path / "tiny",
        healpix_level=5,
        pixel_start=10,
        pixel_stop=12,
        page_size=3,
    )
    plan = json.loads((tmp_path / "tiny" / "bulk_plan.json").read_text())

    assert result["status"] == "planned"
    assert plan["progress"]["chunks_total"] == 2
    assert not (tmp_path / "tiny" / "chunks").exists()

    with pytest.raises(ValueError, match="different settings"):
        create_bulk_plan(
            label="tiny",
            root=tmp_path / "tiny",
            healpix_level=5,
            pixel_start=10,
            pixel_stop=13,
            page_size=3,
        )


def test_page_adql_is_ordered_and_injection_safe():
    adql = build_page_adql(
        table="gaiadr3.gaia_source",
        columns=["source_id", "ra"],
        lower_source_id=100,
        upper_source_id=200,
        page_size=25,
    )
    assert adql.startswith("SELECT TOP 25 source_id, ra")
    assert "source_id >= 100" in adql
    assert "source_id < 200" in adql
    assert adql.endswith("ORDER BY source_id ASC")

    with pytest.raises(ValueError, match="Invalid Gaia column"):
        build_page_adql(
            table="gaiadr3.gaia_source",
            columns=["source_id; DROP TABLE x"],
            lower_source_id=100,
            upper_source_id=200,
            page_size=25,
        )


def test_run_plan_paginates_writes_parts_and_completes(tmp_path):
    create_bulk_plan(
        label="tiny",
        root=tmp_path / "tiny",
        healpix_level=12,
        pixel_start=0,
        pixel_stop=1,
        custom_columns=["source_id", "ra"],
        columns_mode="custom",
        page_size=2,
    )
    queries = []

    def fake_query(adql: str) -> pd.DataFrame:
        queries.append(adql)
        lower = int(re.search(r"source_id >= (\d+)", adql).group(1))
        if lower == 0:
            return pd.DataFrame({"source_id": [0, 1], "ra": [10.0, 10.1]})
        if lower == 2:
            return pd.DataFrame({"source_id": [2], "ra": [10.2]})
        return pd.DataFrame(columns=["source_id", "ra"])

    result = run_bulk_plan(
        tmp_path / "tiny" / "bulk_plan.json",
        max_chunks=1,
        query_runner=fake_query,
    )

    assert result["status"] == "completed"
    assert result["progress"]["rows_downloaded"] == 3
    assert result["progress"]["files_written"] == 2
    assert len(queries) == 3
    parts = sorted((tmp_path / "tiny" / "chunks" / "hp12_000000000").glob("*.parquet"))
    assert len(parts) == 2
    combined = pd.concat(pd.read_parquet(part) for part in parts)
    assert combined["source_id"].tolist() == [0, 1, 2]

    again = run_bulk_plan(
        tmp_path / "tiny" / "bulk_plan.json",
        max_chunks=1,
        query_runner=lambda _: pytest.fail("completed plan queried Gaia again"),
    )
    assert again["status"] == "completed"


def test_failed_chunk_is_checkpointed_and_retried(tmp_path):
    create_bulk_plan(
        label="retry",
        root=tmp_path / "retry",
        healpix_level=12,
        pixel_start=3,
        pixel_stop=4,
        page_size=2,
    )
    plan_path = tmp_path / "retry" / "bulk_plan.json"

    first = run_bulk_plan(
        plan_path,
        max_chunks=1,
        query_runner=lambda _: (_ for _ in ()).throw(RuntimeError("service busy")),
    )
    assert first["status"] == "partial"
    assert first["progress"]["failed_pixels"] == [3]

    lower, _ = source_id_bounds(12, 3)

    def recovered(adql: str) -> pd.DataFrame:
        cursor = int(re.search(r"source_id >= (\d+)", adql).group(1))
        if cursor == lower:
            return pd.DataFrame({"source_id": [lower], "ra": [1.0]})
        return pd.DataFrame(columns=["source_id", "ra"])

    second = run_bulk_plan(plan_path, max_chunks=1, query_runner=recovered)
    assert second["status"] == "completed"
    assert second["progress"]["failed_pixels"] == []
    assert bulk_status(plan_path)["percent_chunks_completed"] == 100.0


def test_short_page_does_not_end_chunk_until_empty_page(tmp_path):
    create_bulk_plan(
        label="shortpage",
        root=tmp_path / "shortpage",
        healpix_level=12,
        pixel_start=7,
        pixel_stop=8,
        page_size=3,
    )
    lower, _ = source_id_bounds(12, 7)
    cursors = []

    def short_pages(adql: str) -> pd.DataFrame:
        cursor = int(re.search(r"source_id >= (\d+)", adql).group(1))
        cursors.append(cursor)
        if cursor == lower:
            return pd.DataFrame({"source_id": [lower], "ra": [1.0]})
        if cursor == lower + 1:
            return pd.DataFrame({"source_id": [lower + 1], "ra": [1.1]})
        return pd.DataFrame(columns=["source_id", "ra"])

    result = run_bulk_plan(
        tmp_path / "shortpage" / "bulk_plan.json",
        max_chunks=1,
        query_runner=short_pages,
    )
    assert result["status"] == "completed"
    assert result["progress"]["rows_downloaded"] == 2
    assert cursors == [lower, lower + 1, lower + 2]


def test_retry_counts_pages_written_before_failure_once(tmp_path):
    create_bulk_plan(
        label="midpage",
        root=tmp_path / "midpage",
        healpix_level=12,
        pixel_start=5,
        pixel_stop=6,
        page_size=2,
    )
    plan_path = tmp_path / "midpage" / "bulk_plan.json"
    lower, _ = source_id_bounds(12, 5)
    calls = 0

    def flaky(adql: str) -> pd.DataFrame:
        nonlocal calls
        calls += 1
        if calls == 1:
            return pd.DataFrame({"source_id": [lower, lower + 1], "ra": [1.0, 1.1]})
        raise RuntimeError("failed after first page")

    first = run_bulk_plan(plan_path, max_chunks=1, query_runner=flaky)
    assert first["progress"]["rows_downloaded"] == 0

    def recovered(adql: str) -> pd.DataFrame:
        cursor = int(re.search(r"source_id >= (\d+)", adql).group(1))
        if cursor == lower + 2:
            return pd.DataFrame({"source_id": [lower + 2], "ra": [1.2]})
        assert cursor == lower + 3
        return pd.DataFrame(columns=["source_id", "ra"])

    second = run_bulk_plan(plan_path, max_chunks=1, query_runner=recovered)
    assert second["progress"]["rows_downloaded"] == 3
    assert second["progress"]["files_written"] == 2


@pytest.mark.parametrize("bad_label", ["../oops", "a/b", "", " has space"])
def test_bulk_label_cannot_escape_data_root(tmp_path, bad_label):
    with pytest.raises(ValueError, match="label"):
        create_bulk_plan(label=bad_label, root=tmp_path / "safe")


def test_cli_root_rejects_path_like_label():
    with pytest.raises(ValueError, match="label"):
        _root("../outside")


def test_bulk_plan_records_release_table_and_partition_contract(tmp_path):
    result = create_bulk_plan(
        label="release_aware",
        root=tmp_path / "release_aware",
        release="Gaia DR3",
        table="gaia_source",
        healpix_level=12,
        pixel_start=0,
        pixel_stop=1,
    )
    selection = result["selection"]
    assert selection["release"] == "dr3"
    assert selection["data_release"] == "Gaia DR3"
    assert selection["schema"] == "gaiadr3"
    assert selection["table"] == "gaiadr3.gaia_source"
    assert selection["partition_strategy"] == "source_id_healpix12"


def test_dr4_bulk_plan_is_disabled_until_partition_rule_is_confirmed(tmp_path):
    with pytest.raises(ValueError, match="not confirmed"):
        create_bulk_plan(
            label="dr4_future",
            root=tmp_path / "dr4_future",
            release="dr4",
            schema="gaiadr4",
            table="gaia_source",
            healpix_level=12,
            pixel_start=0,
            pixel_stop=1,
        )


def test_bulk_plan_rejects_schema_from_another_release(tmp_path):
    with pytest.raises(ValueError, match="belongs to Gaia DR4"):
        create_bulk_plan(
            label="wrong_schema",
            root=tmp_path / "wrong_schema",
            release="dr3",
            schema="gaiadr4",
            healpix_level=12,
            pixel_start=0,
            pixel_stop=1,
        )
