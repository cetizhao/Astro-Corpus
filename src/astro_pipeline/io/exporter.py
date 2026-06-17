from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import pandas as pd

from astro_pipeline.models.paper import PaperEdgeTable, add_arxiv_ids_column, add_paper_columns

logger = logging.getLogger(__name__)

# Column names that (in priority order) hold a Gaia DR3 source_id in survey
# catalogs. The *Gaia* column is preferred because survey cross-match catalogs
# key sources on the Gaia id, not their internal obsid/specobjid.
_GAIA_ID_CANDIDATES = (
    "source_id", "gaia_source_id", "gaiadr3", "gaia_dr3", "dr3_source_id",
    "Gaia", "GAIA", "gaiaid",
)


def build_knowledge_table(
    catalog_df: pd.DataFrame,
    gaia_spectra_info: Optional[pd.DataFrame] = None,
    lamost_spectra_info: Optional[pd.DataFrame] = None,
    sdss_spectra_info: Optional[pd.DataFrame] = None,
    id_col: str = "source_id",
    gaia_id_col: str = "source_id",
    lamost_id_col: Optional[str] = None,
    sdss_id_col: Optional[str] = None,
    paper_edges: Optional[PaperEdgeTable] = None,
) -> pd.DataFrame:
    """Build a unified knowledge table with truthful per-source availability flags.

    Each survey catalog is joined to ``catalog_df`` via the survey's *Gaia*
    source_id column (``lamost_id_col`` / ``sdss_id_col``). If not supplied, the
    Gaia column is detected with :func:`find_gaia_id_col`. The previous
    implementation guessed the id column from ``[source_id, obsid, specobjid]``
    which silently broke membership flags (e.g. LAMOST's ``obsid`` is an 8-digit
    int while the catalog is keyed on the 19-digit Gaia id).
    """
    result = catalog_df.copy()

    result["has_gaia_xp"] = False
    result["has_lamost_spec"] = False
    result["has_sdss_spec"] = False

    # Gaia XP catalog is already keyed on source_id (== id_col / gaia_id_col).
    if gaia_spectra_info is not None and not gaia_spectra_info.empty:
        col = gaia_id_col if gaia_id_col in gaia_spectra_info.columns else find_gaia_id_col(gaia_spectra_info)
        if col:
            result["has_gaia_xp"] = _source_has_data(result[id_col], gaia_spectra_info[col])

    if lamost_spectra_info is not None and not lamost_spectra_info.empty:
        col = lamost_id_col or find_gaia_id_col(lamost_spectra_info)
        if col:
            result["has_lamost_spec"] = _source_has_data(result[id_col], lamost_spectra_info[col])

    if sdss_spectra_info is not None and not sdss_spectra_info.empty:
        col = sdss_id_col or find_gaia_id_col(sdss_spectra_info)
        if col:
            result["has_sdss_spec"] = _source_has_data(result[id_col], sdss_spectra_info[col])

    if paper_edges is not None and len(paper_edges) > 0:
        result = add_paper_columns(result, paper_edges, id_col=id_col)
    else:
        result["arxiv_ids"] = [[] for _ in range(len(result))]
        result["bibcodes"] = [[] for _ in range(len(result))]
        result["num_papers"] = 0

    return result


def _source_has_data(catalog_ids: pd.Series, survey_ids: pd.Series) -> pd.Series:
    """Boolean Series: which catalog_ids appear in survey_ids (string compare)."""
    survey_set = set(survey_ids.astype(str))
    return catalog_ids.astype(str).isin(survey_set)


def find_gaia_id_col(df: pd.DataFrame, explicit: Optional[str] = None) -> Optional[str]:
    """Find the Gaia DR3 source_id column in a survey catalog.

    ``explicit`` (if given and present) wins. Otherwise scan ``_GAIA_ID_CANDIDATES``
    then fall back to a case-insensitive match. Returns ``None`` if nothing
    plausible is found, so callers can degrade gracefully instead of matching
    against the wrong column.
    """
    if explicit and explicit in df.columns:
        return explicit
    cols_lower = {str(c).lower(): c for c in df.columns}
    for cand in _GAIA_ID_CANDIDATES:
        if cand in df.columns:
            return cand
        if cand.lower() in cols_lower:
            return cols_lower[cand.lower()]
    return None


def export_jsonl(df: pd.DataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    records = df.to_dict(orient="records")
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            cleaned = {k: _serialize_value(v) for k, v in record.items()}
            f.write(json.dumps(cleaned, ensure_ascii=False) + "\n")
    logger.info("Exported %d records to %s", len(records), path)
    return path


def _serialize_value(v):
    import numpy as np
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        if np.isnan(v):
            return None
        return float(v)
    if isinstance(v, np.ndarray):
        return v.tolist()
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    return v
