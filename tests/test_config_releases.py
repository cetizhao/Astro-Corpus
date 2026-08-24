"""Release/schema configuration safety tests."""
from __future__ import annotations

import pytest

from astro_pipeline.config import GaiaConfig, PipelineConfig


def test_bare_dr3_config_resolves_schema_from_release_profile():
    config = GaiaConfig()
    assert config.release == "dr3"
    assert config.db_schema is None


def test_explicit_cross_release_schema_is_rejected():
    with pytest.raises(ValueError, match="belongs to Gaia DR3"):
        GaiaConfig(release="dr4", db_schema="gaiadr3")


def test_local_release_switch_does_not_inherit_default_schema(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "default.yaml").write_text(
        "gaia:\n  release: dr3\n  db_schema: gaiadr3\n",
        encoding="utf-8",
    )
    (config_dir / "local.yaml").write_text(
        "gaia:\n  release: dr4\n",
        encoding="utf-8",
    )

    config = PipelineConfig.load(config_dir)
    assert config.gaia.release == "dr4"
    assert config.gaia.db_schema is None
