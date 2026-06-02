from pathlib import Path
from astro_pipeline.config import PipelineConfig

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def get_config() -> PipelineConfig:
    benchmark_path = CONFIG_DIR / "benchmark.yaml"
    if benchmark_path.exists():
        return PipelineConfig.from_yaml(benchmark_path)
    return PipelineConfig()
