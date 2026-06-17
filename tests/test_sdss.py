import pytest
from tests.conftest import get_config
from astro_pipeline.models.query import ConeSearchQuery
from astro_pipeline.sources.sdss import SdssSource


@pytest.fixture
def config():
    return get_config()


@pytest.fixture
def sdss(config):
    return SdssSource(config.sdss, config.network)


@pytest.mark.network
def test_sdss_query(sdss, config, tmp_path):
    query = ConeSearchQuery(
        ra=56.75, dec=24.12, radius_deg=1.0,
        limit=10,
    )
    result = sdss.query_catalog(query, output_path=tmp_path / "test_sdss.parquet")
    assert result.row_count > 0
    assert "ra" in result.data.columns
    assert result.output_path.exists()
