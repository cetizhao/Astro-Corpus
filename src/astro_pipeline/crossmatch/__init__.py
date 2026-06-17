from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd
from astropy import units as u
from astropy.coordinates import SkyCoord

logger = logging.getLogger(__name__)

GAIA_SOURCE_ID_COLS = ["gaia_source_id", "source_id", "dr3_source_id", "gaiadr3", "gaia_dr3", "gaiaid"]


def crossmatch_by_gaia_id(
    left_df: pd.DataFrame,
    right_df: pd.DataFrame,
    left_id_col: str = "source_id",
) -> pd.DataFrame:
    left = left_df.copy()
    left["_id"] = left[left_id_col].astype(str)

    right_id_col = None
    for candidate in GAIA_SOURCE_ID_COLS:
        if candidate in right_df.columns:
            right_id_col = candidate
            break
    if right_id_col is None:
        cols_lower = {c.lower(): c for c in right_df.columns}
        for candidate in GAIA_SOURCE_ID_COLS:
            if candidate in cols_lower:
                right_id_col = cols_lower[candidate]
                break
    if right_id_col is None:
        raise ValueError(
            f"Cannot find Gaia source_id column in right DataFrame. "
            f"Available columns: {list(right_df.columns)}"
        )

    right = right_df.copy()
    right["_id"] = right[right_id_col].astype(str)
    right = right.drop_duplicates(subset="_id", keep="first")

    merged = left.merge(right, on="_id", how="inner", suffixes=("", "_match"))
    merged = merged.drop(columns=["_id"])
    return merged


def crossmatch_by_position(
    left_df: pd.DataFrame,
    right_df: pd.DataFrame,
    left_ra_col: str = "ra",
    left_dec_col: str = "dec",
    right_ra_col: str = "ra",
    right_dec_col: str = "dec",
    max_sep_arcsec: float = 2.0,
) -> pd.DataFrame:
    left_coords = _coords_or_fallback(left_df, left_ra_col, left_dec_col)
    right_coords = _coords_or_fallback(right_df, right_ra_col, right_dec_col)

    idx_left, sep2d, _ = right_coords.match_to_catalog_sky(left_coords)
    sep_arcsec = sep2d.arcsec

    matched = right_df.iloc[idx_left].reset_index(drop=True)
    matched["_sep_arcsec"] = sep_arcsec

    result = pd.concat([left_df.reset_index(drop=True), matched], axis=1)
    result = result[result["_sep_arcsec"] <= max_sep_arcsec]
    result = result.drop(columns=["_sep_arcsec"])
    return result


def _coords_or_fallback(df: pd.DataFrame, ra_col: str, dec_col: str) -> SkyCoord:
    if ra_col in df.columns and dec_col in df.columns:
        return SkyCoord(ra=df[ra_col].values * u.deg, dec=df[dec_col].values * u.deg, frame="icrs")

    ra_variants = ["ra", "RA", "ra_icrs", "RAICRS", "_RA_icrs", "RAdeg_gdr3"]
    dec_variants = ["dec", "DEC", "de_icrs", "DEICRS", "_DE_icrs", "DEdeg_gdr3"]
    for r, d in zip(ra_variants, dec_variants):
        if r in df.columns and d in df.columns:
            return SkyCoord(ra=df[r].values * u.deg, dec=df[d].values * u.deg, frame="icrs")

    fallback_ra = next((c for c in df.columns if "ra" in c.lower() and "err" not in c.lower()), None)
    fallback_dec = next((c for c in df.columns if "dec" in c.lower() and "err" not in c.lower()), None)
    if fallback_ra and fallback_dec:
        return SkyCoord(ra=df[fallback_ra].values * u.deg, dec=df[fallback_dec].values * u.deg, frame="icrs")

    raise ValueError(f"Cannot find RA/Dec columns in DataFrame. Tried: {ra_col}, {dec_col} and common variants.")


def compute_coverage_center_radius(
    df: pd.DataFrame,
    ra_col: str = "ra",
    dec_col: str = "dec",
) -> tuple[float, float, float]:
    coords = _coords_or_fallback(df, ra_col, dec_col)
    ra_center = np.mean(coords.ra.deg)
    dec_center = np.mean(coords.dec.deg)
    center = SkyCoord(ra=ra_center * u.deg, dec=dec_center * u.deg, frame="icrs")
    seps = center.separation(coords)
    radius_deg = float(seps.max().deg) + 0.5
    return ra_center, dec_center, radius_deg
