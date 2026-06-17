from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Sequence

import pandas as pd
from astroquery.gaia import Gaia

from astro_pipeline.config import GaiaConfig, NetworkConfig
from astro_pipeline.io.writer import DownloadCheckpoint, save_catalog_parquet
from astro_pipeline.models.query import ConeSearchQuery, ParameterQuery, SourceIdQuery
from astro_pipeline.models.results import CatalogResult, SpectrumResult
from astro_pipeline.retry import make_retry_decorator
from astro_pipeline.sources.base import CatalogSource

Gaia.MAIN_GAIA_TABLE = "gaiadr3.gaia_source"
Gaia.ROW_LIMIT = -1


class GaiaSource(CatalogSource):
    def __init__(self, config: GaiaConfig, network: NetworkConfig) -> None:
        super().__init__(config, network)
        self._gaia_config = config
        self._retry = make_retry_decorator(network)
        self._logged_in = False

    def _ensure_login(self) -> None:
        if self._logged_in:
            return
        if self._gaia_config.username and self._gaia_config.password:
            self._logger.info("Logging in to Gaia archive as %s", self._gaia_config.username)
            Gaia.login(
                user=self._gaia_config.username,
                password=self._gaia_config.password,
            )
            self._logged_in = True
            self._logger.info("Gaia login successful")
        else:
            self._logger.info("No Gaia credentials configured, using anonymous access")

    def get_source_name(self) -> str:
        return "Gaia DR3"

    def _build_adql(
        self,
        query: ConeSearchQuery | ParameterQuery,
        extra_columns: list[str] | None = None,
    ) -> str:
        cols = list(self._gaia_config.default_columns)
        if extra_columns:
            cols.extend(extra_columns)
        columns = ", ".join(cols)
        adql = f"SELECT {columns} FROM {self._gaia_config.db_schema}.gaia_source"

        conditions: list[str] = []
        if isinstance(query, ConeSearchQuery):
            conditions.append(
                f"1=CONTAINS("
                f"POINT('ICRS', ra, dec), "
                f"CIRCLE('ICRS', {query.ra}, {query.dec}, {query.radius_deg}))"
            )
            for key, val in query.extra_conditions.items():
                if key.endswith("_max"):
                    col = key.removesuffix("_max")
                    conditions.append(f"{col} <= {val}")
                elif key.endswith("_min"):
                    col = key.removesuffix("_min")
                    conditions.append(f"{col} >= {val}")
        elif isinstance(query, ParameterQuery):
            for col, (op, val) in query.conditions.items():
                conditions.append(f"{col} {op} {val}")

        if conditions:
            adql += " WHERE " + " AND ".join(conditions)
        return adql

    def query_catalog(
        self,
        query: ConeSearchQuery | ParameterQuery | SourceIdQuery,
        output_path: Optional[Path] = None,
    ) -> CatalogResult:
        self._ensure_login()

        if isinstance(query, SourceIdQuery):
            return self._query_by_ids(query, output_path, extra_columns=None)

        adql = self._build_adql(query)
        original_limit = Gaia.ROW_LIMIT
        if query.limit:
            Gaia.ROW_LIMIT = query.limit
        self._logger.info("Gaia ADQL (ROW_LIMIT=%s): %s", Gaia.ROW_LIMIT, adql[:200])

        @self._retry
        def _do_query() -> pd.DataFrame:
            try:
                job = Gaia.launch_job_async(
                    adql, dump_to_file=False, output_format="votable",
                )
                return job.get_results().to_pandas()
            except Exception as e:
                self._logger.warning("Async query failed (%s), trying sync", e)
                job = Gaia.launch_job(adql, output_format="votable")
                return job.get_results().to_pandas()

        df = _do_query()
        Gaia.ROW_LIMIT = original_limit
        self._logger.info("Gaia returned %d rows", len(df))

        if output_path:
            save_catalog_parquet(df, output_path)

        return CatalogResult(
            source_name=self.get_source_name(),
            data=df,
            row_count=len(df),
            query_description=adql,
            output_path=output_path,
        )

    def query_xp_sources(
        self,
        query: ConeSearchQuery | ParameterQuery | SourceIdQuery,
        output_path: Optional[Path] = None,
    ) -> CatalogResult:
        if isinstance(query, SourceIdQuery):
            return self._query_by_ids(query, output_path, extra_columns=["has_xp_sampled"])
        return self._query_xp_by_cone(query, output_path)

    def _query_xp_by_cone(
        self,
        query: ConeSearchQuery | ParameterQuery,
        output_path: Optional[Path] = None,
    ) -> CatalogResult:
        """Query only sources that have XP (BP/RP) sampled spectra."""
        self._ensure_login()
        adql = self._build_adql(query, extra_columns=["has_xp_sampled"])
        if "WHERE" in adql:
            adql += " AND has_xp_sampled = 'true'"
        else:
            adql += " WHERE has_xp_sampled = 'true'"

        original_limit = Gaia.ROW_LIMIT
        if query.limit:
            Gaia.ROW_LIMIT = query.limit
        self._logger.info("Gaia XP query (ROW_LIMIT=%s): %s", Gaia.ROW_LIMIT, adql[:200])

        @self._retry
        def _do_query() -> pd.DataFrame:
            try:
                job = Gaia.launch_job_async(
                    adql, dump_to_file=False, output_format="votable",
                )
                return job.get_results().to_pandas()
            except Exception as e:
                self._logger.warning("Async query failed (%s), trying sync", e)
                job = Gaia.launch_job(adql, output_format="votable")
                return job.get_results().to_pandas()

        df = _do_query()
        Gaia.ROW_LIMIT = original_limit
        self._logger.info("Gaia XP sources: %d rows", len(df))

        if output_path:
            save_catalog_parquet(df, output_path)

        return CatalogResult(
            source_name=self.get_source_name() + " (XP)",
            data=df,
            row_count=len(df),
            query_description=adql,
            output_path=output_path,
        )

    def _query_by_ids(
        self,
        query: SourceIdQuery,
        output_path: Optional[Path] = None,
        extra_columns: list[str] | None = None,
    ) -> CatalogResult:
        self._ensure_login()
        cols = list(self._gaia_config.default_columns)
        if extra_columns:
            cols.extend(extra_columns)
        columns = ", ".join(cols)

        id_list = ", ".join(str(sid) for sid in query.source_ids)
        adql = (
            f"SELECT {columns} FROM {self._gaia_config.db_schema}.gaia_source "
            f"WHERE source_id IN ({id_list})"
        )
        if query.limit:
            adql += f" LIMIT {query.limit}"

        self._logger.info("Gaia source ID query: %d IDs", len(query.source_ids))

        @self._retry
        def _do_query() -> pd.DataFrame:
            try:
                job = Gaia.launch_job_async(
                    adql, dump_to_file=False, output_format="votable",
                )
                return job.get_results().to_pandas()
            except Exception as e:
                self._logger.warning("Async ID query failed (%s), trying sync", e)
                job = Gaia.launch_job(adql, output_format="votable")
                return job.get_results().to_pandas()

        df = _do_query()
        self._logger.info("Gaia source ID query returned %d rows", len(df))

        if output_path:
            save_catalog_parquet(df, output_path)

        return CatalogResult(
            source_name=self.get_source_name(),
            data=df,
            row_count=len(df),
            query_description=adql[:200],
            output_path=output_path,
        )

    def download_spectra(
        self,
        identifiers: Sequence[str] | pd.DataFrame,
        output_dir: Path,
        resume: bool = True,
    ) -> list[SpectrumResult]:
        self._ensure_login()
        if isinstance(identifiers, pd.DataFrame):
            ids = [str(sid) for sid in identifiers["source_id"].tolist()]
        else:
            ids = [str(sid) for sid in identifiers]

        output_dir.mkdir(parents=True, exist_ok=True)
        checkpoint = DownloadCheckpoint(
            output_dir / "checkpoint.json", source_name=self.get_source_name()
        )

        if resume:
            ids = [id_ for id_ in ids if not checkpoint.is_completed(id_)]
            ids = self._filter_downloaded(ids, output_dir)

        if not ids:
            self._logger.info("All Gaia spectra already downloaded")
            return []

        self._logger.info("Downloading %d Gaia XP spectra via DataLink", len(ids))
        results: list[SpectrumResult] = []

        batch_size = 5000
        for i in range(0, len(ids), batch_size):
            batch = ids[i : i + batch_size]

            @self._retry
            def _fetch_batch(batch_ids=batch) -> dict:
                return Gaia.load_data(
                    ids=batch_ids,
                    data_release="DR3",
                    data_structure="RAW",
                    retrieval_type="XP_SAMPLED",
                    format="fits",
                )

            try:
                dl_results = _fetch_batch()
                for key, tables in dl_results.items():
                    for table in tables:
                        try:
                            from astropy.io import fits as afits

                            if hasattr(table, "data") and "source_id" in table.data.columns:
                                source_id = str(table.data["source_id"][0])
                            else:
                                source_id = key
                            out_path = output_dir / f"gaia_xp_{source_id}.fits"
                            if hasattr(table, "writeto"):
                                table.writeto(str(out_path), overwrite=True)
                            else:
                                afits.writeto(str(out_path), table.data, overwrite=True)
                            checkpoint.mark_completed(source_id)
                            results.append(
                                SpectrumResult(source_id=source_id, path=out_path, success=True)
                            )
                        except Exception as e:
                            self._logger.warning("Failed to save Gaia spectrum: %s", e)
                            results.append(
                                SpectrumResult(
                                    source_id=source_id, path=out_path,
                                    success=False, error=str(e),
                                )
                            )
            except Exception as e:
                self._logger.error("Gaia DataLink batch failed: %s", e)
                for id_ in batch:
                    checkpoint.mark_failed(id_)
                    results.append(
                        SpectrumResult(
                            source_id=id_,
                            path=output_dir / f"gaia_xp_{id_}.fits",
                            success=False, error=str(e),
                        )
                    )

        checkpoint.save()
        self._logger.info(
            "Gaia spectra: %d success, %d failed",
            sum(1 for r in results if r.success),
            sum(1 for r in results if not r.success),
        )
        return results
