from pathlib import Path

import pytest

from astro_pipeline.config import PipelineConfig

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


@pytest.fixture(autouse=True)
def isolate_shared_library(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRO_PIPELINE_LIBRARY_DIR", str(tmp_path / "_library"))


@pytest.fixture
def make_symlink():
    """Keep link tests runnable on Windows accounts with symlink privileges."""
    def create(link, target, *, target_is_directory=False):
        try:
            link.symlink_to(target, target_is_directory=target_is_directory)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                pytest.skip("this Windows account cannot create symbolic links")
            raise
    return create


def get_config() -> PipelineConfig:
    benchmark_path = CONFIG_DIR / "benchmark.yaml"
    if benchmark_path.exists():
        return PipelineConfig.from_yaml(benchmark_path)
    return PipelineConfig()
