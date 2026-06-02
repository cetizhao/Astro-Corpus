from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, Sequence

import pandas as pd

from astro_pipeline.config import NetworkConfig
from astro_pipeline.models.query import ConeSearchQuery, ParameterQuery
from astro_pipeline.models.results import CatalogResult, SpectrumResult


class CatalogSource(ABC):
    def __init__(self, config_section, network_config: NetworkConfig) -> None:
        self._config = config_section
        self._network = network_config
        self._logger = logging.getLogger(self.__class__.__name__)

    @abstractmethod
    def get_source_name(self) -> str: ...

    @abstractmethod
    def query_catalog(
        self,
        query: ConeSearchQuery | ParameterQuery,
        output_path: Optional[Path] = None,
    ) -> CatalogResult: ...

    @abstractmethod
    def download_spectra(
        self,
        identifiers: Sequence[str] | pd.DataFrame,
        output_dir: Path,
        resume: bool = True,
    ) -> list[SpectrumResult]: ...

    def _filter_downloaded(self, identifiers: Sequence[str], output_dir: Path) -> list[str]:
        existing = {p.stem for p in output_dir.glob("*.fits")} if output_dir.exists() else set()
        return [id_ for id_ in identifiers if id_ not in existing]
