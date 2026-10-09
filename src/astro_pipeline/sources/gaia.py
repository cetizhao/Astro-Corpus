from __future__ import annotations

import logging
import math
import re
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional, Sequence

import pandas as pd

from astro_pipeline.config import GaiaConfig, NetworkConfig
from astro_pipeline.gaia_releases import get_gaia_release_profile
from astro_pipeline.gaia_tap import GaiaTapClient, SYNC_MAXREC
from astro_pipeline.io.writer import save_catalog_parquet
from astro_pipeline.models.query import ConeSearchQuery, ParameterQuery, SourceIdQuery
from astro_pipeline.models.results import CatalogResult, SpectrumResult
from astro_pipeline.retry import make_retry_decorator
from astro_pipeline.sources.base import CatalogSource

if TYPE_CHECKING:
    from astro_pipeline.gaia_datalink import GaiaDataLinkClient
    from astro_pipeline.gaia_products import GaiaArtifact, GaiaProductRequest
    from astro_pipeline.library.store import LibraryStore


class GaiaSource(CatalogSource):
    def __init__(
        self,
        config: GaiaConfig,
        network: NetworkConfig,
        *,
        library_store: LibraryStore | None = None,
        library_run_id: str | None = None,
        library_refresh: bool = False,
    ) -> None:
        super().__init__(config, network)
        self._gaia_config = config
        self._retry = make_retry_decorator(network)
        self._tap = GaiaTapClient(config, network)
        self._datalink_client: GaiaDataLinkClient | None = None
        self._logged_in = False
        self._release_profile = get_gaia_release_profile(config.release)
        self._db_schema = config.db_schema or self._release_profile.tap_schema
        self._library_store = library_store
        self._library_run_id = library_run_id
        self._library_refresh = bool(library_refresh)

    def _get_datalink_client(self) -> GaiaDataLinkClient:
        if self._datalink_client is None:
            from astro_pipeline.gaia_datalink import GaiaDataLinkClient

            self._datalink_client = GaiaDataLinkClient(
                self._network.request_timeout_seconds
            )
        return self._datalink_client

    def _ensure_login(self) -> None:
        if self._logged_in:
            return
        if self._gaia_config.username and self._gaia_config.password:
            self._logger.info("Logging in to Gaia archive with configured credentials")
            self._get_datalink_client().login(
                user=self._gaia_config.username,
                password=self._gaia_config.password,
            )
            self._logged_in = True
            self._logger.info("Gaia login successful")
        else:
            self._logger.info("No Gaia credentials configured, using anonymous access")

    def get_source_name(self) -> str:
        return self._release_profile.display_name

    @property
    def release_profile(self):
        return self._release_profile

    @property
    def db_schema(self) -> str | None:
        return self._db_schema

    def qualified_table(self, table: str | None = None) -> str:
        """Return a validated release-qualified table or fail without guessing."""
        table = table or self._release_profile.main_table
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
            raise ValueError(f"invalid Gaia table name: {table!r}")
        if not self._db_schema:
            raise LookupError(
                f"No TAP schema configured for {self._release_profile.display_name}; "
                "run probe_gaia_archive first and configure the discovered schema"
            )
        return f"{self._db_schema}.{table}"

    def execute_adql(self, adql: str) -> pd.DataFrame:
        """Execute a read-only Gaia TAP query anonymously and return a frame.

        This method is intentionally a Python-internal primitive; MCP does not
        expose arbitrary ADQL. Higher-level modules build validated queries on
        top of it (for example the HEALPix bulk downloader).
        """
        normalized = adql.lstrip().upper()
        if not normalized.startswith("SELECT"):
            raise ValueError("Only read-only SELECT ADQL queries are supported")
        self._logger.info("Gaia custom ADQL: %s", " ".join(adql.split())[:300])

        # Bulk pages must never fall back to a row-capped synchronous result.
        return self._tap.query(adql, asynchronous=True)

    def execute_metadata_adql(self, adql: str) -> pd.DataFrame:
        """Run a small synchronous TAP_SCHEMA discovery query.

        Discovery must not enter the long asynchronous queue used by catalogue
        and bulk jobs. This entry point is intentionally restricted to
        TAP_SCHEMA reads and is not exposed as arbitrary MCP ADQL.
        """
        normalized = " ".join(adql.split()).upper()
        if not normalized.startswith("SELECT") or "TAP_SCHEMA." not in normalized:
            raise ValueError("metadata ADQL is restricted to SELECT from TAP_SCHEMA")
        self._logger.info("Gaia TAP_SCHEMA ADQL: %s", normalized[:300])

        return self._tap.query(
            adql, timeout=self._gaia_config.metadata_timeout_seconds,
        )

    def _query_catalog_adql(self, adql: str, limit: int | None) -> pd.DataFrame:
        return self._tap.query(
            adql, asynchronous=not (limit is not None and 0 < limit <= SYNC_MAXREC),
        )

    def _build_adql(
        self,
        query: ConeSearchQuery | ParameterQuery,
        extra_columns: list[str] | None = None,
    ) -> str:
        cols = list(self._gaia_config.default_columns)
        if extra_columns:
            cols.extend(extra_columns)
        columns = ", ".join(cols)
        # An explicit TOP bounds the result independently of client defaults.
        limit_clause = f" TOP {query.limit}" if query.limit else ""
        adql = f"SELECT{limit_clause} {columns} FROM {self.qualified_table()}"

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
        # TOP without ORDER BY does not define which rows are returned. A
        # stable source-id order makes repeated limited corpus builds select
        # the same source set, which is required for safe checkpoint reuse.
        if query.limit:
            adql += " ORDER BY source_id ASC"
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
        self._logger.info("Gaia ADQL: %s", adql[:200])
        df = self._query_catalog_adql(adql, query.limit)
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
        """Return XP Sampled/Continuous availability for the configured release.

        Queries are anonymous and batched so a corpus with thousands of IDs
        does not produce an oversized ADQL ``IN`` clause.  The output keeps
        the caller's first-seen ID order and includes ``gaia_source_found`` so
        a missing/obsolete ID is distinguishable from a source that exists but
        has no XP product.
        """
        if not {"XP_SAMPLED", "XP_CONTINUOUS"}.issubset(
            set(self._release_profile.datalink_products)
        ):
            raise NotImplementedError(
                f"{self._release_profile.display_name} does not declare DR3-style "
                "XP_SAMPLED/XP_CONTINUOUS availability flags"
            )
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
            query_description=(
                f"XP availability for {len(ids)} "
                f"{self._release_profile.display_name} source IDs"
            ),
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
        order_clause = ""
        if " ORDER BY " in adql:
            adql, order_clause = adql.split(" ORDER BY ", maxsplit=1)
            order_clause = f" ORDER BY {order_clause}"
        if "WHERE" in adql:
            adql += " AND has_xp_sampled = 'true'"
        else:
            adql += " WHERE has_xp_sampled = 'true'"
        adql += order_clause

        self._logger.info("Gaia XP query: %s", adql[:200])
        df = self._query_catalog_adql(adql, query.limit)
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

        # For an uploaded ID list, limit means the first N caller-supplied
        # identifiers, not an arbitrary TOP N chosen by the TAP service.
        if query.limit:
            ids = ids[: query.limit]
        id_list = ", ".join(ids)
        limit_clause = f" TOP {len(ids)}" if query.limit else ""
        adql = (
            f"SELECT{limit_clause} {columns} "
            f"FROM {self.qualified_table()} "
            f"WHERE source_id IN ({id_list}) ORDER BY source_id ASC"
        )

        self._logger.info("Gaia source ID query: %d IDs", len(query.source_ids))

        df = self._query_catalog_adql(adql, len(ids))
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
                f"FROM {self.qualified_table()} AS g "
                "WHERE " + " OR ".join(f"({predicate})" for predicate in predicates)
            )
            self._logger.info(
                "Gaia cone-union cross-match batch %d/%d (%d positions)",
                start // batch_size + 1,
                (len(work) + batch_size - 1) // batch_size,
                len(batch),
            )

            candidates = self._tap.query(adql)
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
        """Backward-compatible DR3 XP wrapper around ``download_products``."""
        from astro_pipeline.gaia_products import GaiaProductRequest

        retrieval_type = retrieval_type.upper()
        if retrieval_type not in {"XP_SAMPLED", "XP_CONTINUOUS"}:
            raise ValueError(f"Unsupported Gaia XP retrieval type: {retrieval_type}")
        if batch_size is None:
            batch_size = 5 if retrieval_type == "XP_CONTINUOUS" else 50
        if batch_size < 1 or batch_size > 5000:
            raise ValueError("batch_size must be between 1 and Gaia DataLink's 5000-source limit")

        if isinstance(identifiers, pd.DataFrame):
            ids = [str(sid) for sid in identifiers["source_id"].tolist()]
        else:
            ids = [str(sid) for sid in identifiers]
        artifacts = self.download_products(
            GaiaProductRequest(
                release=self._release_profile.key,
                retrieval_type=retrieval_type,
                identifiers=ids,
                data_structure="RAW",
                output_format="fits",
                linking_parameter="SOURCE_ID",
                batch_size=batch_size,
            ),
            output_dir,
            resume=resume,
            progress_callback=progress_callback,
        )
        results = [
            SpectrumResult(
                source_id=artifact.identifier,
                path=(
                    Path(artifact.normalized_path)
                    if artifact.normalized_path
                    else Path(output_dir) / f"{artifact.identifier}.fits"
                ),
                success=artifact.success,
                error=artifact.error,
            )
            for artifact in artifacts
        ]
        self._logger.info(
            "Gaia spectra: %d success, %d failed",
            sum(1 for r in results if r.success),
            sum(1 for r in results if not r.success),
        )
        return results

    def _load_data(self, **kwargs):
        return self._get_datalink_client().load_data(**kwargs)

    def download_products(
        self,
        request: GaiaProductRequest,
        output_dir: Path | str,
        *,
        resume: bool = True,
        progress_callback: Optional[Callable[[dict], None]] = None,
    ) -> list[GaiaArtifact]:
        """Download any release-declared tabular DataLink product."""
        from astro_pipeline.gaia_products import download_gaia_product_request

        if request.release != self._release_profile.key:
            raise ValueError(
                f"request release {request.release!r} does not match GaiaSource "
                f"profile {self._release_profile.key!r}"
            )
        if request.retrieval_type == "CROWDED_FIELD_IMAGE":
            raise NotImplementedError(
                "CROWDED_FIELD_IMAGE contains a binary image payload that is not "
                "returned in astroquery's table dictionary; blob transport is deferred "
                "until the DR4 service is publicly testable"
            )
        @self._retry
        def load_data(**kwargs):
            # A shared-library hit never invokes this callback, so cached product
            # reads neither import astroquery's DataLink client nor attempt login.
            self._ensure_login()
            return self._load_data(**kwargs)

        return download_gaia_product_request(
            request,
            output_dir,
            resume=resume,
            loader=load_data,
            progress_callback=progress_callback,
            library=self._library_store,
            run_id=self._library_run_id,
            refresh=self._library_refresh,
        )
