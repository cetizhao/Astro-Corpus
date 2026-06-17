import pytest
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
