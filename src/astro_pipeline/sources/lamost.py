from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Sequence

import httpx
import pandas as pd
from astropy.coordinates import SkyCoord
from astropy import units as u
from astroquery.vizier import Vizier

from astro_pipeline.config import LamostConfig, NetworkConfig
from astro_pipeline.io.writer import DownloadCheckpoint, save_catalog_parquet
from astro_pipeline.models.query import ConeSearchQuery, ParameterQuery
from astro_pipeline.models.results import CatalogResult, SpectrumResult
from astro_pipeline.retry import make_retry_decorator
from astro_pipeline.sources.base import CatalogSource


class LamostSource(CatalogSource):
    def __init__(self, config: LamostConfig, network: NetworkConfig) -> None:
        super().__init__(config, network)
        self._lamost_config = config
        self._retry = make_retry_decorator(network)

    def get_source_name(self) -> str:
        return "LAMOST DR11"

    def query_catalog(
        self,
        query: ConeSearchQuery | ParameterQuery,
        output_path: Optional[Path] = None,
    ) -> CatalogResult:
        v = Vizier(columns=["*"], row_limit=query.limit or -1)
        catalog = self._lamost_config.vizier_catalog_lrs

        if isinstance(query, ConeSearchQuery):
            center = SkyCoord(ra=query.ra * u.deg, dec=query.dec * u.deg, frame="icrs")
            self._logger.info(
                "LAMOST cone search: ra=%.2f dec=%.2f r=%.2f deg",
                query.ra, query.dec, query.radius_deg,
            )

            @self._retry
            def _do_query():
                return v.query_region(
                    center,
                    radius=query.radius_deg * u.deg,
                    catalog=catalog,
                )

            result = _do_query()
        else:
            raise NotImplementedError("LAMOST parameter queries via VizieR not yet supported")

        if result is None or len(result) == 0:
            self._logger.warning("LAMOST query returned no results")
            df = pd.DataFrame()
        else:
            table = result[0]
            df = table.to_pandas()
            # Deduplicate columns (VizieR may return same-named cols from joined tables)
            seen: set[str] = set()
            dedup_cols: list[str] = []
            for i, c in enumerate(df.columns):
                lower = c.lower()
                if lower in seen:
                    dedup_cols.append(f"{lower}_{i}")
                else:
                    dedup_cols.append(lower)
                    seen.add(lower)
            df.columns = dedup_cols

        self._logger.info("LAMOST returned %d rows", len(df))

        if output_path:
            save_catalog_parquet(df, output_path)

        return CatalogResult(
            source_name=self.get_source_name(),
            data=df,
            row_count=len(df),
            query_description=f"cone({query.ra}, {query.dec}, {query.radius_deg})",
            output_path=output_path,
        )

    def download_spectra(
        self,
        identifiers: Sequence[str] | pd.DataFrame,
        output_dir: Path,
        resume: bool = True,
    ) -> list[SpectrumResult]:
        if isinstance(identifiers, pd.DataFrame):
            if "obsid" in identifiers.columns:
                ids = [str(o) for o in identifiers["obsid"].tolist()]
            else:
                raise ValueError("DataFrame must have 'obsid' column")
        else:
            ids = [str(o) for o in identifiers]

        output_dir.mkdir(parents=True, exist_ok=True)
        checkpoint = DownloadCheckpoint(
            output_dir / "checkpoint.json", source_name=self.get_source_name()
        )

        if resume:
            ids = [id_ for id_ in ids if not checkpoint.is_completed(id_)]
            ids = self._filter_downloaded(ids, output_dir)

        if not ids:
            self._logger.info("All LAMOST spectra already downloaded")
            return []

        self._logger.info("Downloading %d LAMOST spectra", len(ids))
        results: list[SpectrumResult] = []
        timeout = self._network.request_timeout_seconds

        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            for obsid in ids:
                out_path = output_dir / f"lamost_spec_{obsid}.fits"
                try:
                    url = f"{self._lamost_config.spectrum_base_url}{obsid}"
                    self._logger.debug("Fetching %s", url)

                    @self._retry
                    def _fetch(url=url):
                        resp = client.get(url)
                        resp.raise_for_status()
                        return resp

                    resp = _fetch()
                    out_path.write_bytes(resp.content)
                    checkpoint.mark_completed(obsid)
                    results.append(
                        SpectrumResult(source_id=obsid, path=out_path, success=True)
                    )
                except Exception as e:
                    self._logger.warning("Failed to download LAMOST %s: %s", obsid, e)
                    checkpoint.mark_failed(obsid)
                    results.append(
                        SpectrumResult(
                            source_id=obsid, path=out_path, success=False, error=str(e)
                        )
                    )

        checkpoint.save()
        self._logger.info(
            "LAMOST spectra: %d success, %d failed",
            sum(1 for r in results if r.success),
            sum(1 for r in results if not r.success),
        )
        return results
