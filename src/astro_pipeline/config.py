from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

from astro_pipeline.gaia_releases import (
    normalize_gaia_release,
    validate_gaia_schema,
)


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
    release: str = "dr3"
    # Stable releases may pin a schema.  Provisional releases such as DR4
    # should set this to null and use TAP_SCHEMA runtime discovery.
    db_schema: str | None = None
    default_query_timeout: float = Field(default=600, gt=0, allow_inf_nan=False)
    metadata_timeout_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)
    username: str = ""
    password: str = ""
    default_columns: list[str] = [
        "source_id", "ra", "dec", "parallax", "parallax_error",
        "pmra", "pmdec", "phot_g_mean_mag", "phot_bp_mean_mag",
        "phot_rp_mean_mag", "radial_velocity", "ruwe",
    ]

    @model_validator(mode="after")
    def validate_release_schema(self) -> "GaiaConfig":
        self.release = normalize_gaia_release(self.release)
        if self.db_schema:
            self.db_schema = validate_gaia_schema(self.release, self.db_schema)
        return self


class LamostConfig(BaseModel):
    base_url: str = "http://www.lamost.org/dr11/v2.0"
    spectrum_base_url: str = "https://www.lamost.org/dr11/v2.0/spectrum/fits/"
    vizier_catalog_lrs: str = "V/162/dr11l"
    vizier_catalog_mrs: str = "V/162/dr11m"


class SdssConfig(BaseModel):
    skyserver_url: str = "https://skyserver.sdss.org/dr17"
    sas_base_url: str = "https://data.sdss.org/sas/dr17"
    data_release: int = 17


class LibraryConfig(BaseModel):
    enabled: bool = True
    root: Path | None = None
    busy_timeout_ms: int = Field(default=5000, ge=0)
    lease_seconds: float = Field(default=120, gt=0, allow_inf_nan=False)
    wait_timeout_seconds: float = Field(default=30, ge=0, allow_inf_nan=False)
    positive_ttl_seconds: float = Field(default=30 * 86400, gt=0, allow_inf_nan=False)
    negative_ttl_seconds: float = Field(default=86400, gt=0, allow_inf_nan=False)


class PipelineConfig(BaseModel):
    output: OutputConfig = OutputConfig()
    library: LibraryConfig = LibraryConfig()
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
                    merged = {**current.model_dump(), **value}
                    if key == "gaia" and "release" in value and "db_schema" not in value:
                        # A release switch must not inherit the previous
                        # release's pinned schema from default.yaml.
                        merged["db_schema"] = None
                    setattr(cfg, key, current.__class__(**merged))
                else:
                    setattr(cfg, key, value)
        return cfg
