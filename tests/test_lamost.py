import pytest
from tests.conftest import get_config
from astro_pipeline.models.query import ConeSearchQuery
from astro_pipeline.sources.lamost import LamostSource


@pytest.fixture
def config():
    return get_config()


@pytest.fixture
def lamost(config):
    return LamostSource(config.lamost, config.network)


@pytest.mark.network
def test_lamost_query(lamost, config, tmp_path):
    query = ConeSearchQuery(
        ra=56.75, dec=24.12, radius_deg=2.0,
        limit=10,
    )
    result = lamost.query_catalog(query, output_path=tmp_path / "test_lamost.parquet")
    assert result.row_count > 0
    assert "obsid" in result.data.columns
    assert result.output_path.exists()
