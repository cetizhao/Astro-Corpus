from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel


class NetworkConfig(BaseModel):
    retry_max_attempts: int = 5
    retry_base_delay_seconds: float = 2.0
    retry_max_delay_seconds: float = 120.0
    request_timeout_seconds: int = 300
    download_chunk_size: int = 65536


class OutputConfig(BaseModel):
    base_dir: Path = Path("./data")
    catalogs_dir: Path = Path("./data/catalogs")
    spectra_dir: Path = Path("./data/spectra")
    checkpoints_dir: Path = Path("./data/checkpoints")

    def ensure_dirs(self) -> None:
        for d in [self.base_dir, self.catalogs_dir, self.spectra_dir, self.checkpoints_dir]:
            d.mkdir(parents=True, exist_ok=True)


class GaiaConfig(BaseModel):
    tap_url: str = "https://gea.esac.esa.int/tap-server"
    db_schema: str = "gaiadr3"
    default_query_timeout: int = 600
    username: str = ""
    password: str = ""
    default_columns: list[str] = [
        "source_id", "ra", "dec", "parallax", "parallax_error",
        "pmra", "pmdec", "phot_g_mean_mag", "phot_bp_mean_mag",
        "phot_rp_mean_mag", "radial_velocity", "ruwe",
    ]


class LamostConfig(BaseModel):
    base_url: str = "http://www.lamost.org/dr11/v2.0"
    spectrum_base_url: str = "https://www.lamost.org/dr11/v2.0/spectrum/fits/"
    vizier_catalog_lrs: str = "V/162/dr11l"
    vizier_catalog_mrs: str = "V/162/dr11m"


class SdssConfig(BaseModel):
    skyserver_url: str = "https://skyserver.sdss.org/dr17"
    sas_base_url: str = "https://data.sdss.org/sas/dr17"
    data_release: int = 17


class PipelineConfig(BaseModel):
    output: OutputConfig = OutputConfig()
    logging: dict = {"level": "INFO"}
    network: NetworkConfig = NetworkConfig()
    gaia: GaiaConfig = GaiaConfig()
    lamost: LamostConfig = LamostConfig()
    sdss: SdssConfig = SdssConfig()

    @classmethod
    def from_yaml(cls, path: Path) -> PipelineConfig:
        with open(path) as f:
            raw = yaml.safe_load(f)
        return cls(**raw)

    @classmethod
    def load(cls, config_dir: Path | str = "config") -> PipelineConfig:
        """Load default.yaml, then overlay local.yaml if it exists.

        ``local.yaml`` is gitignored and intended for credentials / personal
        overrides. Missing files fall back to bare defaults.
        """
        config_dir = Path(config_dir)
        default_path = config_dir / "default.yaml"
        local_path = config_dir / "local.yaml"

        if default_path.exists():
            cfg = cls.from_yaml(default_path)
        else:
            cfg = cls()

        if local_path.exists():
            with open(local_path) as f:
                local_raw = yaml.safe_load(f) or {}
            # Deep-merge top-level sections (gaia/lamost/sdss/network/output).
            for key, value in local_raw.items():
                current = getattr(cfg, key, None)
                if isinstance(current, BaseModel) and isinstance(value, dict):
                    setattr(cfg, key, current.__class__(**{**current.model_dump(), **value}))
                else:
                    setattr(cfg, key, value)
        return cfg
