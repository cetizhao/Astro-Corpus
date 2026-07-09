"""Resolve heterogeneous inputs into a unified Gaia DR3 members DataFrame.

Single entry point ``resolve_members()`` accepts exactly one of:

  * ``cone=(ra, dec, radius_deg)`` — Gaia DR3 cone search
  * ``catalog=path`` — user CSV with a Gaia source_id column OR just RA/Dec
    (auto cross-matched to Gaia)
  * ``name="TW Hya"`` — single source resolved via SIMBAD
  * ``region=("TWA", 10.0)`` — region name + cone radius around SIMBAD center

The returned DataFrame has canonical column names (``Gaia``, ``RAICRS``,
``DEICRS``, ``Plx_gdr3``, ...) matching what the existing TWA pipeline
expects, so it can be passed directly to ``build_paper_edges()``,
``build_overview_html()``, ``export_jsonl.py``.

This is the substrate for the upcoming MCP server: each input mode is a
candidate MCP tool that produces a uniform downstream contract.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import pandas as pd

from astro_pipeline.config import PipelineConfig
from astro_pipeline.models.query import ConeSearchQuery, SourceIdQuery
from astro_pipeline.sources.gaia import GaiaSource

logger = logging.getLogger(__name__)

# Map Gaia DR3 native column names -> TWA-pipeline-canonical names.
# Only fields downstream currently reads are renamed; QC extras keep native names
# and downstream code can opt-in.
CANONICAL_RENAME: dict[str, str] = {
    "source_id": "Gaia",
    "ra": "RAICRS",
    "dec": "DEICRS",
    "parallax": "Plx_gdr3",
    "parallax_error": "e_Plx_gdr3",
    "pmra": "pmRA_gdr3",
    "pmdec": "pmDE_gdr3",
    "phot_g_mean_mag": "Gmag_gdr3",
    "phot_bp_mean_mag": "BPmag_gdr3",
    "phot_rp_mean_mag": "RPmag_gdr3",
    "radial_velocity": "RV_gdr3",
    "ruwe": "RUWE_gdr3",
}

# Gaia DR3 quality-control fields surfaced alongside the basics so downstream
# (export_jsonl, training consumers) can decide filtering themselves.
QC_EXTRA_COLUMNS: list[str] = [
    "phot_bp_rp_excess_factor",
    "astrometric_excess_noise",
    "visibility_periods_used",
    "astrometric_n_good_obs_al",
    "duplicated_source",
]


def resolve_members(
    *,
    cone: Optional[tuple[float, float, float]] = None,
    catalog: Optional[str | Path] = None,
    name: Optional[str] = None,
    region: Optional[tuple[str, float]] = None,
    catalog_gaia_col: str = "Gaia",
    catalog_ra_col: str = "RAICRS",
    catalog_dec_col: str = "DEICRS",
    limit: Optional[int] = None,
) -> pd.DataFrame:
    """Resolve exactly one input mode into a unified members DataFrame.

    Parameters
    ----------
    cone : (ra_deg, dec_deg, radius_deg)
    catalog : path to a CSV. If it has a Gaia source_id column (default
        ``"Gaia"``, configurable via ``catalog_gaia_col``), DR3 fields are
        fetched by id. Otherwise the catalog must have RA/Dec columns and is
        cross-matched to Gaia by position.
    name : SIMBAD-resolvable source name (e.g. ``"TW Hya"``).
    region : (center_name, radius_deg) -- SIMBAD resolves the center, then
        cone-search Gaia around it.
    limit : optional cap on rows returned. Cone/region queries can return
        thousands; this prevents accidental huge downloads.

    Returns
    -------
    DataFrame with canonical column names (see ``CANONICAL_RENAME``) plus any
    QC extras Gaia returned. Suitable for ``build_paper_edges`` /
    ``build_overview_html`` / ``export_jsonl.py``.
    """
    provided = [k for k, v in dict(cone=cone, catalog=catalog, name=name, region=region).items() if v is not None]
    if len(provided) != 1:
        raise ValueError(
            f"Exactly one of cone/catalog/name/region required; got {provided or 'none'}."
        )

    if cone is not None:
        ra, dec, radius = cone
        return _from_cone(ra, dec, radius, limit=limit)
    if catalog is not None:
        return _from_catalog(
            catalog, catalog_gaia_col, catalog_ra_col, catalog_dec_col, limit=limit
        )
    if name is not None:
        return _from_name(name)
    if region is not None:
        center, radius = region
        return _from_region(center, radius, limit=limit)
    raise AssertionError("unreachable")


# ------------------------------------------------------------------ input modes
def _from_cone(ra: float, dec: float, radius_deg: float, *, limit: Optional[int]) -> pd.DataFrame:
    cfg = PipelineConfig()
    src = GaiaSource(cfg.gaia, cfg.network)
    res = src.query_catalog(
        ConeSearchQuery(ra=ra, dec=dec, radius_deg=radius_deg, limit=limit),
    )
    df = _canonicalize(res.data)
    # Gaia astroquery's ROW_LIMIT is a global hint, not a hard cap; under
    # concurrent load it can be ignored. Truncate explicitly if needed.
    if limit is not None and len(df) > limit:
        logger.warning(
            "Cone search returned %d rows (>%d requested). Gaia's ROW_LIMIT is "
            "advisory and occasionally flaky; truncating to %d. For deterministic "
            "caps, filter downstream or use a smaller radius.",
            len(df), limit, limit,
        )
        df = df.head(limit).reset_index(drop=True)
    return df


def _from_catalog(
    path: str | Path,
    gaia_col: str,
    ra_col: str,
    dec_col: str,
    *,
    limit: Optional[int],
) -> pd.DataFrame:
    df = pd.read_csv(path)
    logger.info("Loaded %d rows from %s", len(df), path)

    if gaia_col in df.columns:
        logger.info("Column '%s' found; fetching DR3 fields by source_id", gaia_col)
        ids = [str(x) for x in df[gaia_col].tolist()]
        return _fetch_gaia_by_ids(ids, limit=limit)

    if ra_col in df.columns and dec_col in df.columns:
        logger.info("No '%s'; cross-matching by position (%s, %s)", gaia_col, ra_col, dec_col)
        return _crossmatch_to_gaia(df, ra_col, dec_col, limit=limit)

    raise ValueError(
        f"Catalog has neither '{gaia_col}' nor ('{ra_col}' + '{dec_col}'). "
        f"Available columns: {list(df.columns)[:20]}"
    )


def _from_name(name: str) -> pd.DataFrame:
    """SIMBAD resolves name -> Gaia DR3 source_id -> fetch DR3 fields."""
    from astroquery.simbad import Simbad

    simbad = Simbad()
    res = simbad.query_object(name)
    if res is None or len(res) == 0:
        raise LookupError(f"SIMBAD has no object for name: {name!r}")
    res_df = res.to_pandas()
    main_id = str(res_df["main_id"].iloc[0])
    logger.info("SIMBAD %r -> main_id=%r", name, main_id)

    # Find Gaia DR3 identifier among the object's aliases.
    res2 = simbad.query_objectids(main_id)
    gaia_ids: list[str] = []
    if res2 is not None and "id" in res2.colnames:
        for v in res2["id"]:
            s = str(v).strip()
            if s.startswith("Gaia DR3 "):
                gaia_ids.append(s.removeprefix("Gaia DR3 ").strip())
    if not gaia_ids:
        raise LookupError(f"SIMBAD has no Gaia DR3 id for {name!r} (main_id={main_id})")
    logger.info("SIMBAD %r -> Gaia DR3 %s", name, gaia_ids[0])
    return _fetch_gaia_by_ids(gaia_ids[:1])


def _from_region(center_name: str, radius_deg: float, *, limit: Optional[int]) -> pd.DataFrame:
    """SIMBAD resolves center -> cone search around (center_ra, center_dec)."""
    from astroquery.simbad import Simbad

    simbad = Simbad()
    res = simbad.query_object(center_name)
    if res is None or len(res) == 0:
        raise LookupError(f"SIMBAD has no object for region center {center_name!r}")
    res_df = res.to_pandas()
    # SIMBAD's ra/dec columns are ICRS degrees.
    ra = float(res_df["ra"].iloc[0])
    dec = float(res_df["dec"].iloc[0])
    logger.info("Region %r -> center (%.4f, %.4f); cone radius %.2f deg",
                center_name, ra, dec, radius_deg)
    return _from_cone(ra, dec, radius_deg, limit=limit)


# ------------------------------------------------------------------ helpers
def _fetch_gaia_by_ids(source_ids: list[str], *, limit: Optional[int] = None) -> pd.DataFrame:
    cfg = PipelineConfig()
    src = GaiaSource(cfg.gaia, cfg.network)
    res = src._query_by_ids(
        SourceIdQuery(source_ids=source_ids, limit=limit),
        extra_columns=QC_EXTRA_COLUMNS,
    )
    return _canonicalize(res.data)


def _crossmatch_to_gaia(
    df: pd.DataFrame, ra_col: str, dec_col: str, *, limit: Optional[int]
) -> pd.DataFrame:
    """Cross-match a position-only catalog to Gaia DR3.

    Strategy: compute the catalog's coverage center + radius, fire one Gaia cone
    query big enough to cover it, then nearest-neighbour match each user row to
    the Gaia sources. Tight 1 arcsec match window by default.
    """
    logger.warning(
        "Position-only catalog cross-match (no Gaia source_id column) is less "
        "exercised than the Gaia-id path. Using 1 arcsec nearest-neighbour match; "
        "verify the matched count matches expectations. If your sources have "
        "unusual proper motions or are in dense fields, consider pre-cross-matching "
        "with the Gaia Archive and providing a `Gaia` column instead."
    )

    from astropy import units as u
    from astropy.coordinates import SkyCoord

    from astro_pipeline.crossmatch import compute_coverage_center_radius

    center_ra, center_dec, radius_deg = compute_coverage_center_radius(df, ra_col, dec_col)
    logger.info("Coverage center (%.3f, %.3f), radius %.3f deg", center_ra, center_dec, radius_deg)

    cfg = PipelineConfig()
    src = GaiaSource(cfg.gaia, cfg.network)
    res = src.query_catalog(
        ConeSearchQuery(ra=center_ra, dec=center_dec, radius_deg=radius_deg + 0.05, limit=limit),
    )
    gaia_df = res.data
    if gaia_df.empty:
        raise LookupError("Gaia returned no sources in the catalog's coverage area")

    user_coords = SkyCoord(ra=df[ra_col].values * u.deg, dec=df[dec_col].values * u.deg)
    gaia_coords = SkyCoord(
        ra=gaia_df["ra"].values * u.deg, dec=gaia_df["dec"].values * u.deg
    )
    idx, sep2d, _ = user_coords.match_to_catalog_sky(gaia_coords)

    max_sep_arcsec = 1.0
    good = sep2d.arcsec < max_sep_arcsec
    logger.info("Matched %d / %d user rows within %.1f arcsec",
                int(good.sum()), len(df), max_sep_arcsec)

    matched = gaia_df.iloc[idx[good]].reset_index(drop=True)
    return _canonicalize(matched)


def _canonicalize(df: pd.DataFrame) -> pd.DataFrame:
    """Rename Gaia DR3 native columns to canonical names; ensure Gaia is str."""
    if df.empty:
        return df
    df = df.rename(columns=CANONICAL_RENAME).copy()
    if "Gaia" in df.columns:
        df["Gaia"] = df["Gaia"].astype(str)
    return df
