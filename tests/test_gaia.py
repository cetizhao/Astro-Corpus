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


def test_source_id_query_rejects_non_numeric_ids(gaia):
    with pytest.raises(ValueError, match="digits only"):
        gaia._query_by_ids(SourceIdQuery(source_ids=["123 OR 1=1"]))


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
