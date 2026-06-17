from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from astropy import units as u
from astropy.coordinates import SkyCoord

from astro_pipeline.models.query import SourceIdQuery

logger = logging.getLogger(__name__)


class ExternalCatalog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.df = pd.read_csv(self.path)
        logger.info("Loaded %d sources from %s", len(self.df), self.path.name)

    @property
    def source_ids(self) -> list[str]:
        id_col = self._find_gaia_id_column()
        return [str(sid) for sid in self.df[id_col].tolist()]

    def to_source_id_query(self) -> SourceIdQuery:
        return SourceIdQuery(source_ids=self.source_ids)

    def compute_coverage(self) -> tuple[float, float, float]:
        ra_col, dec_col = self._find_coordinate_columns()
        if len(self.df) == 0:
            logger.warning("Empty catalog, coverage unknown")
            return 0.0, 0.0, 1.0
        coords = SkyCoord(
            ra=self.df[ra_col].values * u.deg,
            dec=self.df[dec_col].values * u.deg,
            frame="icrs",
        )
        ra_center = float(np.mean(coords.ra.deg))
        dec_center = float(np.mean(coords.dec.deg))
        center = SkyCoord(ra=ra_center * u.deg, dec=dec_center * u.deg, frame="icrs")
        seps = center.separation(coords)
        radius_deg = float(seps.max().deg) + 1.0
        logger.info(
            "Coverage: center=(%.3f, %.3f), radius=%.3f deg",
            ra_center, dec_center, radius_deg,
        )
        return ra_center, dec_center, radius_deg

    def filter_quality(
        self,
        max_ruwe: Optional[float] = 1.4,
        min_parallax_over_error: Optional[float] = 5.0,
    ) -> ExternalCatalog:
        df = self.df.copy()
        if max_ruwe is not None and "RUWE_gdr3" in df.columns:
            before = len(df)
            df = df[df["RUWE_gdr3"].astype(float) < max_ruwe]
            logger.info("RUWE filter: %d -> %d (max_ruwe=%.1f)", before, len(df), max_ruwe)
        if min_parallax_over_error is not None:
            plx_col = None
            plx_err_col = None
            exclude_patterns = ["corr", "pscol", "pm", "plxpm", "raplx", "deplx"]
            for c in df.columns:
                cl = c.lower()
                if any(x in cl for x in exclude_patterns):
                    continue
                if plx_col is None and "plx_" in cl and "e_plx" not in cl:
                    plx_col = c
                if plx_err_col is None and cl.startswith("e_plx"):
                    plx_err_col = c
            if plx_col is None and "Plx_gdr3" in df.columns:
                plx_col = "Plx_gdr3"
                plx_err_col = "e_Plx_gdr3"
            if plx_col and plx_err_col and plx_col in df.columns and plx_err_col in df.columns:
                before = len(df)
                ratio = df[plx_col].astype(float) / df[plx_err_col].astype(float)
                df = df[ratio > min_parallax_over_error]
                logger.info(
                    "Plx/err filter (%s/%s): %d -> %d (min_ratio=%.1f)",
                    plx_col, plx_err_col, before, len(df), min_parallax_over_error,
                )
        self.df = df
        return self

    def _find_gaia_id_column(self) -> str:
        candidates = ["Gaia", "source_id", "DR3Name_gdr3", "gaia_source_id"]
        for c in candidates:
            if c in self.df.columns:
                return c
        for c in self.df.columns:
            if "source" in c.lower() and "id" in c.lower():
                return c
        raise ValueError(f"Cannot find Gaia source_id column. Columns: {list(self.df.columns)}")

    def _find_coordinate_columns(self) -> tuple[str, str]:
        ra_candidates = ["RAICRS", "RAdeg_gdr3", "ra", "RA", "_RA_icrs"]
        dec_candidates = ["DEICRS", "DEdeg_gdr3", "dec", "DEC", "_DE_icrs"]
        for ra_c, dec_c in zip(ra_candidates, dec_candidates):
            if ra_c in self.df.columns and dec_c in self.df.columns:
                return ra_c, dec_c
        for c in self.df.columns:
            if "ra" in c.lower():
                return c, c.replace("RA", "DE").replace("ra", "de").replace("Ra", "De")
        raise ValueError(f"Cannot find coordinate columns. Columns: {list(self.df.columns)}")
