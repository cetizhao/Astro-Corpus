import pytest
import pandas as pd
from astropy.table import Table
from tests.conftest import get_config
from astro_pipeline.models.query import ConeSearchQuery, SourceIdQuery
from astro_pipeline.sources.gaia import GaiaSource


@pytest.fixture
def config():
    return get_config()


@pytest.fixture
def gaia(config):
    return GaiaSource(config.gaia, config.network)


@pytest.mark.network
def test_gaia_query(gaia, config, tmp_path):
    query = ConeSearchQuery(
        ra=56.75, dec=24.12, radius_deg=0.5,
        limit=10,
    )
    result = gaia.query_catalog(query, output_path=tmp_path / "test_gaia.parquet")
    assert result.row_count > 0
    assert "source_id" in result.data.columns
    assert result.output_path.exists()


def test_continuous_download_defaults_to_small_batches(gaia, monkeypatch, tmp_path):
    calls = []

    def fake_load_data(*, ids, retrieval_type, **kwargs):
        calls.append((list(ids), retrieval_type))
        return {"raw": [Table({"source_id": [int(source_id) for source_id in ids]})]}

    monkeypatch.setattr("astro_pipeline.sources.gaia.Gaia.load_data", fake_load_data)
    ids = [str(1000 + index) for index in range(12)]

    results = gaia.download_spectra(
        ids,
        tmp_path,
        resume=False,
        retrieval_type="XP_CONTINUOUS",
    )

    assert [len(batch) for batch, _ in calls] == [5, 5, 2]
    assert all(retrieval_type == "XP_CONTINUOUS" for _, retrieval_type in calls)
    assert len(results) == 12
    assert all(result.success for result in results)


def test_source_id_adql_uses_top_not_non_adql_limit(gaia, monkeypatch):
    class FakeJob:
        def get_results(self):
            return Table({"source_id": [123]})

    seen = []

    def fake_async(adql, **kwargs):
        seen.append(adql)
        return FakeJob()

    monkeypatch.setattr("astro_pipeline.sources.gaia.Gaia.launch_job_async", fake_async)
    gaia._query_by_ids(SourceIdQuery(source_ids=["123"], limit=1))

    assert seen[0].startswith("SELECT TOP 1 ")
    assert " LIMIT " not in seen[0]
    assert "WHERE source_id IN (123)" in seen[0]
    assert seen[0].endswith("ORDER BY source_id ASC")


def test_limited_source_id_query_selects_first_input_ids(gaia, monkeypatch):
    class FakeJob:
        def get_results(self):
            return Table({"source_id": [100, 200]})

    seen = []
    monkeypatch.setattr(
        "astro_pipeline.sources.gaia.Gaia.launch_job_async",
        lambda adql, **kwargs: seen.append(adql) or FakeJob(),
    )

    gaia._query_by_ids(SourceIdQuery(source_ids=["200", "100", "999"], limit=2))

    assert seen[0].startswith("SELECT TOP 2 ")
    assert "WHERE source_id IN (200, 100)" in seen[0]
    assert "999" not in seen[0]


def test_limited_cone_query_has_deterministic_order(gaia):
    adql = gaia._build_adql(
        ConeSearchQuery(ra=10.0, dec=20.0, radius_deg=1.0, limit=200)
    )
    assert adql.startswith("SELECT TOP 200 ")
    assert adql.endswith("ORDER BY source_id ASC")


def test_limited_xp_cone_puts_filter_before_order(gaia, monkeypatch):
    class FakeJob:
        def get_results(self):
            return Table({"source_id": [], "has_xp_sampled": []})

    seen = []
    monkeypatch.setattr(
        "astro_pipeline.sources.gaia.Gaia.launch_job_async",
        lambda adql, **kwargs: seen.append(adql) or FakeJob(),
    )
    gaia.query_xp_sources(
        ConeSearchQuery(ra=10.0, dec=20.0, radius_deg=1.0, limit=10)
    )

    assert "has_xp_sampled = 'true' ORDER BY source_id ASC" in seen[0]


def test_source_id_query_rejects_non_numeric_ids(gaia):
    with pytest.raises(ValueError, match="digits only"):
        gaia._query_by_ids(SourceIdQuery(source_ids=["123 OR 1=1"]))


def test_custom_adql_does_not_fall_back_to_sync_tap(gaia, monkeypatch):
    def fail_async(*args, **kwargs):
        raise RuntimeError("async TAP unavailable")

    monkeypatch.setattr("astro_pipeline.sources.gaia.Gaia.launch_job_async", fail_async)
    monkeypatch.setattr(
        "astro_pipeline.sources.gaia.Gaia.launch_job",
        lambda *args, **kwargs: pytest.fail("must not use sync TAP for bulk pages"),
    )

    with pytest.raises(RuntimeError, match="async TAP unavailable"):
        gaia.execute_adql("SELECT TOP 2 source_id FROM gaiadr3.gaia_source")


def test_metadata_adql_is_sync_and_restricted_to_tap_schema(gaia, monkeypatch):
    import io

    buffer = io.BytesIO()
    Table({"schema_name": ["gaiadr3"]}).write(buffer, format="votable")
    calls = []

    class FakeResponse:
        content = buffer.getvalue()

        def raise_for_status(self):
            return None

    monkeypatch.setattr(
        "astro_pipeline.sources.gaia.httpx.post",
        lambda url, **kwargs: calls.append((url, kwargs)) or FakeResponse(),
    )
    result = gaia.execute_metadata_adql(
        "SELECT schema_name FROM TAP_SCHEMA.schemas"
    )
    assert result["schema_name"].tolist() == ["gaiadr3"]
    assert len(calls) == 1
    assert calls[0][0].endswith("/tap/sync")
    assert "TAP_SCHEMA.schemas" in calls[0][1]["data"]["QUERY"]
    assert calls[0][1]["timeout"] == 10.0

    with pytest.raises(ValueError, match="restricted"):
        gaia.execute_metadata_adql(
            "SELECT source_id FROM gaiadr3.gaia_source"
        )


def test_crossmatch_positions_keeps_nearest_candidate(gaia, monkeypatch):
    class FakeJob:
        def get_results(self):
            return Table(
                {
                    "source_id": [100, 101, 200],
                    "ra": [10.0002, 10.00005, 11.0],
                    "dec": [20.0, 20.0, 21.0],
                }
            )

    monkeypatch.setattr(
        "astro_pipeline.sources.gaia.Gaia.launch_job_async",
        lambda *args, **kwargs: FakeJob(),
    )
    positions = pd.DataFrame({"row": [0, 1], "ra0": [10.0, 11.0], "dec0": [20.0, 21.0]})
    result = gaia.crossmatch_positions(
        positions,
        ra_col="ra0",
        dec_col="dec0",
        row_id_col="row",
        radius_arcsec=3.0,
    )

    assert result["source_id"].tolist() == [101, 200]
    assert result["match_candidate_count"].tolist() == [2, 1]


def test_query_xp_availability_preserves_missing_source(gaia, monkeypatch):
    def fake_by_ids(query, output_path=None, extra_columns=None):
        frame = pd.DataFrame(
            {
                "source_id": [100],
                "has_xp_sampled": [True],
                "has_xp_continuous": [False],
            }
        )
        from astro_pipeline.models.results import CatalogResult

        return CatalogResult("Gaia", frame, 1)

    monkeypatch.setattr(gaia, "_query_by_ids", fake_by_ids)
    result = gaia.query_xp_availability(["100", "999"])

    assert result.data["source_id"].tolist() == ["100", "999"]
    assert result.data["gaia_source_found"].tolist() == [True, False]
    assert result.data["has_xp_sampled"].tolist() == [True, False]
