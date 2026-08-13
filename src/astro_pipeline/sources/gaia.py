from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Callable, Optional, Sequence

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
        # Explicit TOP n in the SQL. Gaia.ROW_LIMIT (a module global) is
        # advisory and not reliably applied by all astroquery versions; an
        # explicit TOP makes the cap deterministic. Without it, cone queries
        # over dense fields (e.g. 1 deg at Taurus) return the FULL cone.
        limit_clause = f" TOP {query.limit}" if query.limit else ""
        adql = f"SELECT{limit_clause} {columns} FROM {self._gaia_config.db_schema}.gaia_source"

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
        # TAP astrometry queries work anonymously; no login needed.
        # Login is only required for DataLink (download_spectra).

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
            result = self._query_by_ids(query, None, extra_columns=["has_xp_sampled"])
            df = result.data
            if "has_xp_sampled" in df.columns:
                mask = df["has_xp_sampled"].astype(str).str.lower().isin({"true", "1"})
                df = df.loc[mask].reset_index(drop=True)
            if output_path:
                save_catalog_parquet(df, output_path)
            return CatalogResult(
                source_name=self.get_source_name() + " (XP)",
                data=df,
                row_count=len(df),
                query_description=result.query_description,
                output_path=output_path,
            )
        return self._query_xp_by_cone(query, output_path)

    def query_xp_availability(
        self,
        source_ids: Sequence[str],
        output_path: Optional[Path] = None,
        *,
        batch_size: int = 2000,
    ) -> CatalogResult:
        """Return XP Sampled/Continuous availability for Gaia DR3 IDs.

        Queries are anonymous and batched so a corpus with thousands of IDs
        does not produce an oversized ADQL ``IN`` clause.  The output keeps
        the caller's first-seen ID order and includes ``gaia_source_found`` so
        a missing/obsolete ID is distinguishable from a source that exists but
        has no XP product.
        """
        if batch_size < 1 or batch_size > 5000:
            raise ValueError("batch_size must be between 1 and 5000")
        ids: list[str] = []
        seen: set[str] = set()
        for raw_id in source_ids:
            source_id = str(raw_id).strip()
            if not source_id or source_id in seen:
                continue
            if not source_id.isdigit():
                raise ValueError("Gaia source IDs must contain digits only")
            seen.add(source_id)
            ids.append(source_id)

        availability_columns = ["has_xp_sampled", "has_xp_continuous"]
        frames: list[pd.DataFrame] = []
        for start in range(0, len(ids), batch_size):
            result = self._query_by_ids(
                SourceIdQuery(source_ids=ids[start : start + batch_size]),
                extra_columns=availability_columns,
            )
            frames.append(result.data)

        queried = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        base = pd.DataFrame({"source_id": ids})
        if queried.empty:
            out = base.copy()
            out["has_xp_sampled"] = False
            out["has_xp_continuous"] = False
            out["gaia_source_found"] = False
        else:
            queried = queried.copy()
            queried["source_id"] = queried["source_id"].astype(str)
            keep = ["source_id", *availability_columns]
            out = base.merge(queried[keep], on="source_id", how="left", validate="one_to_one")
            out["gaia_source_found"] = out[availability_columns].notna().any(axis=1)
            for column in availability_columns:
                out[column] = (
                    out[column]
                    .astype(str)
                    .str.strip()
                    .str.lower()
                    .isin({"true", "1"})
                )

        if output_path:
            save_catalog_parquet(out, output_path)
        return CatalogResult(
            source_name=self.get_source_name() + " (XP availability)",
            data=out,
            row_count=len(out),
            query_description=f"XP availability for {len(ids)} Gaia DR3 source IDs",
            output_path=output_path,
        )

    def _query_xp_by_cone(
        self,
        query: ConeSearchQuery | ParameterQuery,
        output_path: Optional[Path] = None,
    ) -> CatalogResult:
        """Query only sources that have XP (BP/RP) sampled spectra."""
        # TAP query; anonymous access is fine.
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
        # TAP query; anonymous access is fine.
        cols = list(self._gaia_config.default_columns)
        if extra_columns:
            cols.extend(extra_columns)
        columns = ", ".join(cols)

        ids = [str(sid).strip() for sid in query.source_ids if str(sid).strip()]
        if not ids:
            return CatalogResult(
                source_name=self.get_source_name(),
                data=pd.DataFrame(columns=cols),
                row_count=0,
                query_description="empty source ID query",
                output_path=output_path,
            )
        if any(not sid.isdigit() for sid in ids):
            raise ValueError("Gaia source IDs must contain digits only")

        id_list = ", ".join(ids)
        limit_clause = f" TOP {query.limit}" if query.limit else ""
        adql = (
            f"SELECT{limit_clause} {columns} "
            f"FROM {self._gaia_config.db_schema}.gaia_source "
            f"WHERE source_id IN ({id_list})"
        )

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

    def crossmatch_positions(
        self,
        positions: pd.DataFrame,
        *,
        ra_col: str,
        dec_col: str,
        row_id_col: str = "membership_row_id",
        radius_arcsec: float = 3.0,
        batch_size: int = 25,
        extra_columns: list[str] | None = None,
    ) -> pd.DataFrame:
        """Cross-match many positions to Gaia DR3 with batched small cones.

        Each batch is one ordinary anonymous ADQL query containing a union of
        small positional predicates.  Returned Gaia candidates are assigned
        to the input coordinates locally.  This is intentionally different
        from a single large cone query: a large cone can be truncated before
        all input positions are represented, while Gaia TAP-upload jobs can
        remain queued for minutes on a busy service.  The nearest Gaia source
        is returned per input row together with the angular separation and
        number of candidates in that row's match circle.

        Unmatched input rows are omitted from the returned frame; callers can
        recover them by joining on ``row_id_col``.
        """
        if radius_arcsec <= 0 or radius_arcsec > 60:
            raise ValueError("radius_arcsec must be in the interval (0, 60]")
        if batch_size < 1 or batch_size > 100:
            raise ValueError("batch_size must be between 1 and 100")
        missing = [c for c in (ra_col, dec_col) if c not in positions.columns]
        if missing:
            raise ValueError(f"Missing coordinate columns: {missing}")

        work = positions.copy()
        if row_id_col not in work.columns:
            work[row_id_col] = range(len(work))
        if work[row_id_col].duplicated().any():
            raise ValueError(f"{row_id_col!r} must be unique")

        work[ra_col] = pd.to_numeric(work[ra_col], errors="coerce")
        work[dec_col] = pd.to_numeric(work[dec_col], errors="coerce")
        finite = work[ra_col].map(math.isfinite) & work[dec_col].map(math.isfinite)
        valid = finite & work[ra_col].between(0, 360, inclusive="both") & work[
            dec_col
        ].between(-90, 90, inclusive="both")
        if (~valid).any():
            self._logger.warning(
                "Skipping %d catalogue rows with invalid coordinates",
                int((~valid).sum()),
            )
        work = work.loc[valid, [row_id_col, ra_col, dec_col]].reset_index(drop=True)
        if work.empty:
            return pd.DataFrame(
                columns=[
                    row_id_col,
                    "source_id",
                    "match_sep_arcsec",
                    "match_candidate_count",
                ]
            )

        cols = list(self._gaia_config.default_columns)
        if extra_columns:
            cols.extend(c for c in extra_columns if c not in cols)
        gaia_select = ", ".join(f"g.{col} AS {col}" for col in cols)
        radius_deg = radius_arcsec / 3600.0
        frames: list[pd.DataFrame] = []

        for start in range(0, len(work), batch_size):
            batch = work.iloc[start : start + batch_size].copy()
            predicates = []
            for _, row in batch.iterrows():
                predicates.append(
                    "1=CONTAINS(POINT('ICRS', g.ra, g.dec), "
                    f"CIRCLE('ICRS', {float(row[ra_col]):.12f}, "
                    f"{float(row[dec_col]):.12f}, {radius_deg:.12f}))"
                )
            adql = (
                f"SELECT {gaia_select} "
                f"FROM {self._gaia_config.db_schema}.gaia_source AS g "
                "WHERE " + " OR ".join(f"({predicate})" for predicate in predicates)
            )
            self._logger.info(
                "Gaia cone-union cross-match batch %d/%d (%d positions)",
                start // batch_size + 1,
                (len(work) + batch_size - 1) // batch_size,
                len(batch),
            )

            @self._retry
            def _do_query() -> pd.DataFrame:
                try:
                    job = Gaia.launch_job_async(
                        adql,
                        dump_to_file=False,
                        output_format="votable",
                    )
                    return job.get_results().to_pandas()
                except Exception as exc:
                    self._logger.warning(
                        "Async cone-union cross-match failed (%s), trying sync", exc
                    )
                    job = Gaia.launch_job(
                        adql,
                        output_format="votable",
                    )
                    return job.get_results().to_pandas()

            candidates = _do_query()
            if candidates.empty:
                continue

            from astropy import units as u
            from astropy.coordinates import SkyCoord

            input_coords = SkyCoord(
                ra=batch[ra_col].to_numpy(dtype=float) * u.deg,
                dec=batch[dec_col].to_numpy(dtype=float) * u.deg,
            )
            candidate_coords = SkyCoord(
                ra=pd.to_numeric(candidates["ra"]).to_numpy() * u.deg,
                dec=pd.to_numeric(candidates["dec"]).to_numpy() * u.deg,
            )
            # search_around_sky returns every pair within the requested radius.
            # Calling it as candidate.search_around_sky(input) yields indices in
            # input first and candidate second (Astropy's documented ordering).
            input_idx, candidate_idx, separations, _ = candidate_coords.search_around_sky(
                input_coords, radius_arcsec * u.arcsec
            )
            if len(input_idx) == 0:
                continue
            matched = candidates.iloc[candidate_idx].copy().reset_index(drop=True)
            matched[row_id_col] = batch.iloc[input_idx][row_id_col].to_numpy()
            matched["match_sep_arcsec"] = separations.arcsec
            frames.append(matched)

        if not frames:
            return pd.DataFrame(
                columns=[
                    row_id_col,
                    *cols,
                    "match_sep_arcsec",
                    "match_candidate_count",
                ]
            )

        candidates = pd.concat(frames, ignore_index=True)
        candidates["match_candidate_count"] = candidates.groupby(row_id_col)[
            row_id_col
        ].transform("size")
        nearest = (
            candidates.sort_values([row_id_col, "match_sep_arcsec", "source_id"])
            .drop_duplicates(row_id_col, keep="first")
            .reset_index(drop=True)
        )
        self._logger.info(
            "Gaia position cross-match: %d/%d rows matched within %.2f arcsec; "
            "%d rows had multiple candidates",
            nearest[row_id_col].nunique(),
            len(work),
            radius_arcsec,
            int((nearest["match_candidate_count"] > 1).sum()),
        )
        return nearest

    def download_spectra(
        self,
        identifiers: Sequence[str] | pd.DataFrame,
        output_dir: Path,
        resume: bool = True,
        retrieval_type: str = "XP_SAMPLED",
        batch_size: int | None = None,
        progress_callback: Optional[Callable[[dict], None]] = None,
    ) -> list[SpectrumResult]:
        retrieval_type = retrieval_type.upper()
        if retrieval_type not in {"XP_SAMPLED", "XP_CONTINUOUS"}:
            raise ValueError(f"Unsupported Gaia XP retrieval type: {retrieval_type}")
        if batch_size is None:
            batch_size = 5 if retrieval_type == "XP_CONTINUOUS" else 5000
        if batch_size < 1 or batch_size > 5000:
            raise ValueError("batch_size must be between 1 and Gaia DataLink's 5000-source limit")

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

        total_pending = len(ids)
        if not ids:
            self._logger.info("All Gaia spectra already downloaded")
            if progress_callback:
                progress_callback(
                    {
                        "retrieval_type": retrieval_type,
                        "pending_at_start": 0,
                        "processed": 0,
                        "completed_this_run": 0,
                        "failed_this_run": 0,
                        "done": True,
                    }
                )
            return []

        self._logger.info("Downloading %d Gaia %s spectra via DataLink", len(ids), retrieval_type)
        results: list[SpectrumResult] = []

        for i in range(0, len(ids), batch_size):
            batch = ids[i : i + batch_size]
            self._logger.info(
                "Gaia %s batch %d/%d (%d sources)",
                retrieval_type,
                i // batch_size + 1,
                (len(ids) + batch_size - 1) // batch_size,
                len(batch),
            )

            @self._retry
            def _fetch_batch(batch_ids=batch) -> dict:
                return Gaia.load_data(
                    ids=batch_ids,
                    data_release="Gaia DR3",
                    data_structure="RAW",
                    retrieval_type=retrieval_type,
                    format="fits",
                )

            try:
                dl_results = _fetch_batch()
                returned_ids: set[str] = set()
                for _key, tables in (dl_results or {}).items():
                    for table in tables:
                        from astropy.table import Table

                        if not isinstance(table, Table):
                            table = Table(table)
                        names = {name.lower(): name for name in table.colnames}
                        sid_col = names.get("source_id") or names.get("sourceid")
                        if not sid_col:
                            self._logger.warning(
                                "Gaia XP table has no source_id column: %s", table.colnames,
                            )
                            continue
                        for row_index in range(len(table)):
                            source_id = str(table[sid_col][row_index])
                            out_path = output_dir / f"{source_id}.fits"
                            try:
                                table[row_index : row_index + 1].write(
                                    str(out_path), format="fits", overwrite=True,
                                )
                                returned_ids.add(source_id)
                                checkpoint.mark_completed(source_id)
                                results.append(
                                    SpectrumResult(source_id=source_id, path=out_path, success=True)
                                )
                            except Exception as e:
                                self._logger.warning(
                                    "Failed to save Gaia spectrum %s: %s", source_id, e,
                                )
                                checkpoint.mark_failed(source_id)
                                results.append(
                                    SpectrumResult(
                                        source_id=source_id, path=out_path,
                                        success=False, error=str(e),
                                    )
                                )

                for source_id in set(batch) - returned_ids:
                    checkpoint.mark_failed(source_id)
                    results.append(
                        SpectrumResult(
                            source_id=source_id,
                            path=output_dir / f"{source_id}.fits",
                            success=False,
                            error=f"Gaia DataLink returned no {retrieval_type} product",
                        )
                    )
                checkpoint.save()
            except Exception as e:
                self._logger.error("Gaia DataLink batch failed: %s", e)
                for id_ in batch:
                    checkpoint.mark_failed(id_)
                    results.append(
                        SpectrumResult(
                            source_id=id_,
                            path=output_dir / f"{id_}.fits",
                            success=False, error=str(e),
                        )
                    )
                checkpoint.save()

            if progress_callback:
                processed = min(i + len(batch), total_pending)
                progress_callback(
                    {
                        "retrieval_type": retrieval_type,
                        "pending_at_start": total_pending,
                        "processed": processed,
                        "completed_this_run": sum(1 for r in results if r.success),
                        "failed_this_run": sum(1 for r in results if not r.success),
                        "done": processed >= total_pending,
                    }
                )

        checkpoint.save()
        self._logger.info(
            "Gaia spectra: %d success, %d failed",
            sum(1 for r in results if r.success),
            sum(1 for r in results if not r.success),
        )
        return results
