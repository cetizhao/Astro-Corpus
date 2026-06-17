from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Sequence

import pandas as pd
from astropy import units as u
from astropy.coordinates import SkyCoord
from astroquery.sdss import SDSS

from astro_pipeline.config import SdssConfig, NetworkConfig
from astro_pipeline.io.writer import DownloadCheckpoint, save_catalog_parquet
from astro_pipeline.models.query import ConeSearchQuery, ParameterQuery
from astro_pipeline.models.results import CatalogResult, SpectrumResult
from astro_pipeline.retry import make_retry_decorator
from astro_pipeline.sources.base import CatalogSource

SDSS.ROW_LIMIT = -1


class SdssSource(CatalogSource):
    def __init__(self, config: SdssConfig, network: NetworkConfig) -> None:
        super().__init__(config, network)
        self._sdss_config = config
        self._retry = make_retry_decorator(network)

    def get_source_name(self) -> str:
        return "SDSS DR17"

    def query_catalog(
        self,
        query: ConeSearchQuery | ParameterQuery,
        output_path: Optional[Path] = None,
    ) -> CatalogResult:
        dr = self._sdss_config.data_release

        if isinstance(query, ConeSearchQuery):
            self._logger.info(
                "SDSS cone search: ra=%.2f dec=%.2f r=%.2f deg",
                query.ra, query.dec, query.radius_deg,
            )
            import math
            cos_dec = math.cos(math.radians(query.dec))
            r = query.radius_deg
            ra_min = round(query.ra - r / cos_dec, 4)
            ra_max = round(query.ra + r / cos_dec, 4)
            dec_min = round(query.dec - r, 4)
            dec_max = round(query.dec + r, 4)
            sql = (
                f"SELECT TOP {query.limit or 10} "
                f"p.ra, p.dec, p.objid, p.clean, "
                f"s.specobjid, s.plate, s.mjd, s.fiberID "
                f"FROM PhotoObj AS p "
                f"JOIN SpecObj AS s ON p.objid = s.bestobjid "
                f"WHERE "
                f"(p.ra BETWEEN {ra_min} AND {ra_max}) "
                f"AND (p.dec BETWEEN {dec_min} AND {dec_max}) "
                f"AND p.clean = 1"
            )
            for key, val in query.extra_conditions.items():
                if key.endswith("_max"):
                    col = key.removesuffix("_max")
                    sql += f" AND p.{col} <= {val}"
                elif key.endswith("_min"):
                    col = key.removesuffix("_min")
                    sql += f" AND p.{col} >= {val}"

            @self._retry
            def _do_query():
                return SDSS.query_sql(sql, data_release=dr)

            result = _do_query()
        elif isinstance(query, ParameterQuery):
            sql = self._build_sql(query)

            @self._retry
            def _do_query(sql=sql):
                return SDSS.query_sql(sql, data_release=dr)

            result = _do_query()
        else:
            raise ValueError(f"Unsupported query type: {type(query)}")

        if result is None:
            self._logger.warning("SDSS query returned no results")
            df = pd.DataFrame()
        else:
            _validate_sdss_result(result)
            df = result.to_pandas()
            df.columns = [c.lower() for c in df.columns]
            # A corrupt response (HTML error page) sometimes parses into a single
            # junk column; detect and reject so it never silently poisons the cache.
            if _looks_like_html_column(df):
                raise RuntimeError(
                    "SDSS query returned an HTML/error page instead of a VOTable "
                    "(SkyServer likely rejected the SQL). Not caching bad result."
                )

        self._logger.info("SDSS returned %d rows", len(df))

        if output_path:
            save_catalog_parquet(df, output_path)

        return CatalogResult(
            source_name=self.get_source_name(),
            data=df,
            row_count=len(df),
            query_description=f"cone({query.ra}, {query.dec}, {query.radius_deg})",
            output_path=output_path,
        )

    def _build_sql(self, query: ParameterQuery) -> str:
        top = f"TOP {query.limit} " if query.limit else ""
        select = f"SELECT {top}p.ra, p.dec, p.u, p.g, p.r, p.i, p.z, p.objid, "
        select += "s.specobjid, s.redshift, s.plate, s.mjd, s.fiberID, s.class "
        select += "FROM PhotoObj AS p JOIN SpecObj AS s ON p.objid = s.bestobjid"
        conditions = []
        for col, (op, val) in query.conditions.items():
            conditions.append(f"{col} {op} {val}")
        if conditions:
            select += " WHERE " + " AND ".join(conditions)
        if query.order_by:
            select += f" ORDER BY {query.order_by}"
        return select

    def download_spectra(
        self,
        identifiers: Sequence[str] | pd.DataFrame,
        output_dir: Path,
        resume: bool = True,
    ) -> list[SpectrumResult]:
        if not isinstance(identifiers, pd.DataFrame):
            raise ValueError("SDSS download requires a DataFrame with plate/mjd/fiberid columns")

        if "plate" not in identifiers.columns:
            raise ValueError("DataFrame must have 'plate', 'mjd', 'fiberid' columns")

        output_dir.mkdir(parents=True, exist_ok=True)
        checkpoint = DownloadCheckpoint(
            output_dir / "checkpoint.json", source_name=self.get_source_name()
        )
        dr = self._sdss_config.data_release
        results: list[SpectrumResult] = []

        for _, row in identifiers.iterrows():
            plate = int(row["plate"])
            mjd = int(row["mjd"])
            fiberid = int(row["fiberid"])
            spec_id = f"{plate}-{mjd}-{fiberid}"
            out_path = output_dir / f"sdss_spec_{spec_id}.fits"

            if resume and checkpoint.is_completed(spec_id):
                self._logger.debug("Skipping already downloaded SDSS %s", spec_id)
                continue

            try:
                self._logger.debug("Downloading SDSS spectrum %s", spec_id)

                @self._retry
                def _fetch(plate=plate, mjd=mjd, fiberid=fiberid):
                    return SDSS.get_spectra(
                        plate=plate, mjd=mjd, fiberID=fiberid,
                        data_release=dr,
                    )

                spectra = _fetch()
                if spectra:
                    spectra[0].writeto(str(out_path), overwrite=True)
                    checkpoint.mark_completed(spec_id)
                    results.append(
                        SpectrumResult(source_id=spec_id, path=out_path, success=True)
                    )
                else:
                    checkpoint.mark_failed(spec_id)
                    results.append(
                        SpectrumResult(
                            source_id=spec_id, path=out_path, success=False,
                            error="No spectrum returned",
                        )
                    )
            except Exception as e:
                self._logger.warning("Failed to download SDSS %s: %s", spec_id, e)
                checkpoint.mark_failed(spec_id)
                results.append(
                    SpectrumResult(
                        source_id=spec_id, path=out_path, success=False, error=str(e)
                    )
                )

        checkpoint.save()
        self._logger.info(
            "SDSS spectra: %d success, %d failed",
            sum(1 for r in results if r.success),
            sum(1 for r in results if not r.success),
        )
        return results


def _validate_sdss_result(result) -> None:
    """Reject obviously-bad SDSS query results (HTML error pages, empty tables)."""
    try:
        n_cols = len(result.colnames)
    except AttributeError:
        return
    if n_cols == 0:
        raise RuntimeError("SDSS query returned a table with no columns")


def _looks_like_html_column(df: pd.DataFrame) -> bool:
    """True when the 'parsed' result is actually an HTML error page.

    SkyServer can return an error/landing HTML page that astroquery parses into
    a single column whose value starts with ``<html`` or ``<!doctype``.
    """
    if len(df.columns) == 1:
        col = df.columns[0]
        val = str(df[col].iloc[0]).lstrip().lower() if len(df) > 0 else ""
        if val.startswith(("<html", "<!doctype", "<!DOCTYPE".lower())):
            return True
        if str(col).lstrip().lower().startswith("<html"):
            return True
    return False
