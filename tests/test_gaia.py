import pytest
from astropy.table import Table
from tests.conftest import get_config
from astro_pipeline.models.query import ConeSearchQuery
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
