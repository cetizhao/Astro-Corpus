"""Resolve heterogeneous inputs into a unified Gaia members DataFrame.

``resolve_members()`` remains the compatibility entry point and returns only a
canonical members :class:`~pandas.DataFrame`.  The provenance-aware
``resolve_members_with_provenance()`` additionally preserves an uploaded CSV's
immutable bytes, every original row, and the row-to-Gaia match outcome.

Exactly one input mode is accepted:

* ``cone=(ra, dec, radius_deg)`` — geometric Gaia cone search
* ``catalog=path`` — CSV with Gaia source IDs or coordinates to cross-match
* ``name="TW Hya"`` — SIMBAD name resolution followed by a Gaia ID lookup
* ``region=("TWA", 10.0)`` — named center followed by a geometric Gaia cone

``region`` is geometric only; it does not infer physical membership in the
named association or cluster.  Published member-list discovery lives in
``astro_pipeline.membership``.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from astro_pipeline.config import PipelineConfig
from astro_pipeline.models.query import ConeSearchQuery, SourceIdQuery
from astro_pipeline.sources.gaia import GaiaSource

logger = logging.getLogger(__name__)

# Map Gaia native column names -> TWA-pipeline-canonical names.  Only fields
# downstream currently reads are renamed; QC extras keep native names.
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

# Gaia quality-control fields surfaced alongside the basics so downstream
# consumers can decide filtering themselves.
QC_EXTRA_COLUMNS: list[str] = [
    "phot_bp_rp_excess_factor",
    "astrometric_excess_noise",
    "visibility_periods_used",
    "astrometric_n_good_obs_al",
    "duplicated_source",
]

INPUT_ROW_COLUMNS = ["input_row_id", "ordinal", "status", "raw_json"]
MATCH_COLUMNS = [
    "input_row_id",
    "source_id",
    "status",
    "match_method",
    "match_sep_arcsec",
    "match_candidate_count",
]
CATALOG_MATCH_RADIUS_ARCSEC = 1.0

IdFetcher = Callable[[list[str]], pd.DataFrame]


def _empty_input_rows() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "input_row_id": pd.Series(dtype="int64"),
            "ordinal": pd.Series(dtype="int64"),
            "status": pd.Series(dtype="string"),
            "raw_json": pd.Series(dtype="string"),
        }
    )


def _empty_matches() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "input_row_id": pd.Series(dtype="int64"),
            "source_id": pd.Series(dtype="string"),
            "status": pd.Series(dtype="string"),
            "match_method": pd.Series(dtype="string"),
            "match_sep_arcsec": pd.Series(dtype="Float64"),
            "match_candidate_count": pd.Series(dtype="Int64"),
        }
    )


@dataclass
class InputResolution:
    """Members plus lossless input-row and match provenance.

    ``input_rows`` and ``matches`` are empty for simple non-CSV selectors.
    ``raw_input`` is populated only for a CSV upload, and ``original_name`` is
    its basename.  The basename is deliberately outside ``contract`` so two
    byte-identical files with different local names have the same semantic
    request contract.
    """

    members: pd.DataFrame
    input_rows: pd.DataFrame = field(default_factory=_empty_input_rows)
    matches: pd.DataFrame = field(default_factory=_empty_matches)
    contract: dict[str, Any] = field(default_factory=dict)
    resolved_input: dict[str, Any] = field(default_factory=dict)
    raw_input: bytes | None = None
    original_name: str | None = None


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
    id_fetcher: Optional[IdFetcher] = None,
) -> pd.DataFrame:
    """Resolve exactly one input mode into a canonical members DataFrame.

    This compatibility wrapper intentionally returns only ``members``.  Use
    :func:`resolve_members_with_provenance` when original CSV rows and match
    outcomes must be persisted.  ``id_fetcher`` may satisfy explicit Gaia-ID
    astrometry lookups; it is never consulted for cone, region, or coordinate
    cross-match completeness.
    """
    resolution = resolve_members_with_provenance(
        cone=cone,
        catalog=catalog,
        name=name,
        region=region,
        catalog_gaia_col=catalog_gaia_col,
        catalog_ra_col=catalog_ra_col,
        catalog_dec_col=catalog_dec_col,
        limit=limit,
        id_fetcher=id_fetcher,
    )
    # Preserve the historical position-catalog behavior for callers of the
    # DataFrame-only API.  The provenance-aware API returns the empty members
    # frame together with row-level invalid/unmatched statuses instead.
    if (
        catalog is not None
        and resolution.resolved_input.get("catalog_kind") == "position"
        and resolution.members.empty
    ):
        raise LookupError("No uploaded catalogue rows matched Gaia within 1 arcsec")
    return resolution.members


def describe_input_contract(
    *,
    cone: Optional[tuple[float, float, float]] = None,
    catalog: Optional[str | Path] = None,
    name: Optional[str] = None,
    region: Optional[tuple[str, float]] = None,
    catalog_gaia_col: str = "Gaia",
    catalog_ra_col: str = "RAICRS",
    catalog_dec_col: str = "DEICRS",
    limit: Optional[int] = None,
    id_fetcher: Optional[IdFetcher] = None,
) -> dict[str, Any]:
    """Describe the immutable selection contract without resolving members.

    This preflight performs no SIMBAD or Gaia calls.  For CSV input it reads the
    bytes to compute their SHA-256 and inspects only the header to determine the
    source-ID or coordinate mapping.  ``id_fetcher`` is accepted solely so this
    helper has the same input signature as the rich resolver; it is never
    called.  The original CSV basename remains separate provenance on
    :class:`InputResolution` and is deliberately absent from this contract.
    """
    del id_fetcher
    input_mode = _input_mode(
        cone=cone,
        catalog=catalog,
        name=name,
        region=region,
    )
    release = _configured_release()

    if input_mode == "cone":
        assert cone is not None
        ra, dec, radius = cone
        selector = {
            "ra_deg": float(ra),
            "dec_deg": float(dec),
            "radius_deg": float(radius),
        }
    elif input_mode == "catalog":
        assert catalog is not None
        raw_input = Path(catalog).read_bytes()
        columns = list(_read_catalog_header(raw_input).columns)
        selector, _ = _catalog_selector(
            raw_input,
            columns,
            gaia_col=catalog_gaia_col,
            ra_col=catalog_ra_col,
            dec_col=catalog_dec_col,
        )
    elif input_mode == "name":
        assert name is not None
        selector = {"name": name}
    elif input_mode == "region":
        assert region is not None
        center, radius = region
        selector = {"center_name": center, "radius_deg": float(radius)}
    else:
        raise AssertionError("unreachable")

    return _input_contract(release, input_mode, limit, selector)


def resolve_members_with_provenance(
    *,
    cone: Optional[tuple[float, float, float]] = None,
    catalog: Optional[str | Path] = None,
    name: Optional[str] = None,
    region: Optional[tuple[str, float]] = None,
    catalog_gaia_col: str = "Gaia",
    catalog_ra_col: str = "RAICRS",
    catalog_dec_col: str = "DEICRS",
    limit: Optional[int] = None,
    id_fetcher: Optional[IdFetcher] = None,
) -> InputResolution:
    """Resolve one selector while retaining complete input provenance.

    CSV rows are numbered before ``limit`` is applied.  Every row receives one
    of ``matched``, ``unmatched``, ``invalid``, ``id_not_found``, or
    ``not_selected_by_limit``.  Repeated successful input rows remain repeated
    in ``members`` and carry their stable ``input_row_id``.  User columns are
    retained only in ``input_rows.raw_json`` (and the immutable ``raw_input``),
    not promoted into canonical Gaia attributes.
    """
    _input_mode(cone=cone, catalog=catalog, name=name, region=region)
    release = _configured_release()

    if cone is not None:
        ra, dec, radius = cone
        selector = {
            "ra_deg": float(ra),
            "dec_deg": float(dec),
            "radius_deg": float(radius),
        }
        return InputResolution(
            members=_from_cone(ra, dec, radius, limit=limit),
            contract=_input_contract(release, "cone", limit, selector),
            resolved_input=dict(selector),
        )

    if catalog is not None:
        return _resolve_catalog_with_provenance(
            catalog,
            catalog_gaia_col,
            catalog_ra_col,
            catalog_dec_col,
            limit=limit,
            release=release,
            id_fetcher=id_fetcher,
        )

    if name is not None:
        members, details = _resolve_name(name, id_fetcher=id_fetcher)
        return InputResolution(
            members=members,
            contract=_input_contract(release, "name", limit, {"name": name}),
            resolved_input=details,
        )

    if region is not None:
        center, radius = region
        members, details = _resolve_region(center, radius, limit=limit)
        return InputResolution(
            members=members,
            contract=_input_contract(
                release,
                "region",
                limit,
                {"center_name": center, "radius_deg": float(radius)},
            ),
            resolved_input=details,
        )

    raise AssertionError("unreachable")


# ------------------------------------------------------------------ input modes
def _from_cone(
    ra: float, dec: float, radius_deg: float, *, limit: Optional[int]
) -> pd.DataFrame:
    cfg = PipelineConfig.load()
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
            len(df),
            limit,
            limit,
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
    id_fetcher: Optional[IdFetcher] = None,
) -> pd.DataFrame:
    """Backward-compatible private helper returning only resolved members."""
    return _resolve_catalog_with_provenance(
        path,
        gaia_col,
        ra_col,
        dec_col,
        limit=limit,
        release=_configured_release(),
        id_fetcher=id_fetcher,
    ).members


def _resolve_catalog_with_provenance(
    path: str | Path,
    gaia_col: str,
    ra_col: str,
    dec_col: str,
    *,
    limit: Optional[int],
    release: str,
    id_fetcher: Optional[IdFetcher],
) -> InputResolution:
    catalog_path = Path(path)
    raw_input = catalog_path.read_bytes()
    frame = _read_catalog_csv(raw_input, gaia_col=gaia_col)
    logger.info("Loaded %d rows from %s", len(frame), catalog_path)

    selector, catalog_kind = _catalog_selector(
        raw_input,
        list(frame.columns),
        gaia_col=gaia_col,
        ra_col=ra_col,
        dec_col=dec_col,
    )
    if catalog_kind == "source_id":
        logger.info("Column '%s' found; fetching Gaia fields by source_id", gaia_col)
        members, input_rows, matches = _resolve_id_catalog_rows(
            frame,
            gaia_col=gaia_col,
            limit=limit,
            id_fetcher=id_fetcher,
        )
    elif catalog_kind == "position":
        logger.info(
            "No '%s'; cross-matching by position (%s, %s)",
            gaia_col,
            ra_col,
            dec_col,
        )
        members, input_rows, matches = _resolve_position_catalog_rows(
            frame,
            ra_col=ra_col,
            dec_col=dec_col,
            limit=limit,
        )
    else:
        raise AssertionError("unreachable")

    resolved_input = {
        "catalog_kind": catalog_kind,
        **selector,
    }

    return InputResolution(
        members=members,
        input_rows=input_rows,
        matches=matches,
        contract=_input_contract(release, "catalog", limit, selector),
        resolved_input=resolved_input,
        raw_input=raw_input,
        original_name=catalog_path.name,
    )


def _from_name(name: str, *, id_fetcher: Optional[IdFetcher] = None) -> pd.DataFrame:
    """SIMBAD resolves name -> Gaia source_id -> fetch Gaia fields."""
    members, _ = _resolve_name(name, id_fetcher=id_fetcher)
    return members


def _resolve_name(
    name: str, *, id_fetcher: Optional[IdFetcher]
) -> tuple[pd.DataFrame, dict[str, Any]]:
    from astroquery.simbad import Simbad

    simbad = Simbad()
    res = simbad.query_object(name)
    if res is None or len(res) == 0:
        raise LookupError(f"SIMBAD has no object for name: {name!r}")
    res_df = res.to_pandas()
    main_id = str(res_df["main_id"].iloc[0])
    logger.info("SIMBAD %r -> main_id=%r", name, main_id)

    # Find the configured release's DR3 identifier among the object's aliases.
    # Ordinary intake remains DR3-only until canonical mappings for later
    # releases are explicitly activated.
    res2 = simbad.query_objectids(main_id)
    gaia_ids: list[str] = []
    if res2 is not None and "id" in res2.colnames:
        for value in res2["id"]:
            text = str(value).strip()
            if text.startswith("Gaia DR3 "):
                gaia_ids.append(text.removeprefix("Gaia DR3 ").strip())
    if not gaia_ids:
        raise LookupError(
            f"SIMBAD has no Gaia DR3 id for {name!r} (main_id={main_id})"
        )
    gaia_id = gaia_ids[0]
    logger.info("SIMBAD %r -> Gaia DR3 %s", name, gaia_id)
    return _fetch_explicit_ids([gaia_id], id_fetcher=id_fetcher), {
        "name": name,
        "simbad_main_id": main_id,
        "source_id": gaia_id,
    }


def _from_region(
    center_name: str, radius_deg: float, *, limit: Optional[int]
) -> pd.DataFrame:
    """SIMBAD center -> geometric Gaia cone (field sources, not members)."""
    members, _ = _resolve_region(center_name, radius_deg, limit=limit)
    return members


def _resolve_region(
    center_name: str, radius_deg: float, *, limit: Optional[int]
) -> tuple[pd.DataFrame, dict[str, Any]]:
    from astroquery.simbad import Simbad

    simbad = Simbad()
    res = simbad.query_object(center_name)
    if res is None or len(res) == 0:
        raise LookupError(f"SIMBAD has no object for region center {center_name!r}")
    res_df = res.to_pandas()
    # SIMBAD's ra/dec columns are ICRS degrees.
    ra = float(res_df["ra"].iloc[0])
    dec = float(res_df["dec"].iloc[0])
    logger.info(
        "Region %r -> center (%.4f, %.4f); cone radius %.2f deg",
        center_name,
        ra,
        dec,
        radius_deg,
    )
    return _from_cone(ra, dec, radius_deg, limit=limit), {
        "center_name": center_name,
        "ra_deg": ra,
        "dec_deg": dec,
        "radius_deg": float(radius_deg),
    }


# ------------------------------------------------------------------ CSV rows
def _resolve_id_catalog_rows(
    frame: pd.DataFrame,
    *,
    gaia_col: str,
    limit: Optional[int],
    id_fetcher: Optional[IdFetcher],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    row_ids = list(range(len(frame)))
    selected = _selected_row_mask(len(frame), limit)
    normalized_ids = [_normalise_source_id(value) for value in frame[gaia_col]]
    statuses = ["not_selected_by_limit"] * len(frame)

    requested_ids: list[str] = []
    for row_id, is_selected in enumerate(selected):
        if not is_selected:
            continue
        source_id = normalized_ids[row_id]
        if source_id is None:
            statuses[row_id] = "invalid"
            continue
        statuses[row_id] = "id_not_found"
        requested_ids.append(source_id)

    # Query each semantic ID once, then map results back to every original row.
    unique_ids = list(dict.fromkeys(requested_ids))
    canonical = _fetch_explicit_ids(unique_ids, id_fetcher=id_fetcher)
    canonical = canonical.drop(columns=["input_row_id"], errors="ignore").copy()

    canonical_for_join = canonical.iloc[0:0].copy()
    if "Gaia" in canonical.columns:
        canonical["_intake_source_id"] = canonical["Gaia"].map(
            _normalise_source_id
        )
        canonical_for_join = (
            canonical.loc[canonical["_intake_source_id"].notna()]
            .drop_duplicates("_intake_source_id", keep="first")
            .copy()
        )
    elif not canonical.empty:
        raise ValueError("ID fetcher result must include 'source_id' or canonical 'Gaia'")

    found_ids = set(canonical_for_join.get("_intake_source_id", pd.Series(dtype=str)))
    request_rows: list[dict[str, Any]] = []
    for row_id, source_id in zip(row_ids, normalized_ids):
        if selected[row_id] and source_id is not None and source_id in found_ids:
            statuses[row_id] = "matched"
            request_rows.append(
                {"input_row_id": row_id, "_intake_source_id": source_id}
            )

    canonical_columns = [
        column
        for column in canonical_for_join.columns
        if column != "_intake_source_id"
    ]
    if request_rows and not canonical_for_join.empty:
        members = pd.DataFrame(request_rows).merge(
            canonical_for_join,
            on="_intake_source_id",
            how="inner",
            sort=False,
            validate="many_to_one",
        )
        members = members.drop(columns=["_intake_source_id"])
        members = members[["input_row_id", *canonical_columns]]
        members = members.sort_values("input_row_id", kind="stable").reset_index(
            drop=True
        )
    else:
        members = pd.DataFrame(columns=["input_row_id", *canonical_columns])

    input_rows = _make_input_rows(frame, statuses)
    match_records: list[dict[str, Any]] = []
    for row_id, status, source_id in zip(row_ids, statuses, normalized_ids):
        matched = status == "matched"
        attempted = status in {"matched", "id_not_found"}
        match_records.append(
            {
                "input_row_id": row_id,
                "source_id": source_id if matched else None,
                "status": status,
                "match_method": "source_id" if attempted else None,
                "match_sep_arcsec": None,
                "match_candidate_count": 1 if matched else (0 if attempted else None),
            }
        )
    return members, input_rows, _make_matches(match_records)


def _resolve_position_catalog_rows(
    frame: pd.DataFrame,
    *,
    ra_col: str,
    dec_col: str,
    limit: Optional[int],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    logger.warning(
        "Position-only catalog cross-match (no Gaia source_id column) is less "
        "exercised than the Gaia-id path. Using 1 arcsec nearest-neighbour match; "
        "verify the matched count matches expectations. If your sources have "
        "unusual proper motions or are in dense fields, consider pre-cross-matching "
        "with the Gaia Archive and providing a `Gaia` column instead."
    )

    selected = _selected_row_mask(len(frame), limit)
    valid_coordinates = _valid_coordinate_mask(frame, ra_col, dec_col)
    work = frame.loc[selected, [ra_col, dec_col]].copy()
    work.insert(0, "input_row_id", frame.index[selected].astype(int))

    cfg = PipelineConfig.load()
    src = GaiaSource(cfg.gaia, cfg.network)
    native = src.crossmatch_positions(
        work,
        ra_col=ra_col,
        dec_col=dec_col,
        row_id_col="input_row_id",
        radius_arcsec=CATALOG_MATCH_RADIUS_ARCSEC,
        batch_size=25,
        extra_columns=QC_EXTRA_COLUMNS,
    )
    canonical = _canonicalize(native)
    if "input_row_id" not in canonical.columns:
        canonical["input_row_id"] = pd.Series(dtype="int64")
    canonical["input_row_id"] = pd.to_numeric(
        canonical["input_row_id"], errors="coerce"
    ).astype("Int64")
    valid_selected_ids = {
        row_id
        for row_id, (is_selected, is_valid) in enumerate(
            zip(selected, valid_coordinates)
        )
        if is_selected and is_valid
    }
    canonical = canonical.loc[
        canonical["input_row_id"].isin(valid_selected_ids)
    ].copy()
    canonical = (
        canonical.sort_values("input_row_id", kind="stable")
        .drop_duplicates("input_row_id", keep="first")
        .reset_index(drop=True)
    )
    if not canonical.empty:
        canonical["input_row_id"] = canonical["input_row_id"].astype("int64")

    matched_by_row: dict[int, pd.Series] = {
        int(row["input_row_id"]): row for _, row in canonical.iterrows()
    }
    statuses: list[str] = []
    match_records: list[dict[str, Any]] = []
    for row_id, (is_selected, is_valid) in enumerate(
        zip(selected, valid_coordinates)
    ):
        if not is_selected:
            status = "not_selected_by_limit"
        elif not is_valid:
            status = "invalid"
        elif row_id in matched_by_row:
            status = "matched"
        else:
            status = "unmatched"
        statuses.append(status)

        matched = matched_by_row.get(row_id)
        attempted = status in {"matched", "unmatched"}
        source_id = (
            _normalise_source_id(matched.get("Gaia"))
            if matched is not None
            else None
        )
        match_records.append(
            {
                "input_row_id": row_id,
                "source_id": source_id if status == "matched" else None,
                "status": status,
                "match_method": "position" if attempted else None,
                "match_sep_arcsec": (
                    matched.get("match_sep_arcsec")
                    if matched is not None
                    else None
                ),
                "match_candidate_count": (
                    matched.get("match_candidate_count")
                    if matched is not None
                    else (0 if status == "unmatched" else None)
                ),
            }
        )

    members = canonical.copy()
    if "input_row_id" in members.columns:
        members = members[
            ["input_row_id", *[c for c in members.columns if c != "input_row_id"]]
        ]
    return members, _make_input_rows(frame, statuses), _make_matches(match_records)


# ------------------------------------------------------------------ helpers
def _input_mode(
    *,
    cone: Optional[tuple[float, float, float]],
    catalog: Optional[str | Path],
    name: Optional[str],
    region: Optional[tuple[str, float]],
) -> str:
    provided = [
        key
        for key, value in {
            "cone": cone,
            "catalog": catalog,
            "name": name,
            "region": region,
        }.items()
        if value is not None
    ]
    if len(provided) != 1:
        raise ValueError(
            f"Exactly one of cone/catalog/name/region required; got {provided or 'none'}."
        )
    return provided[0]


def _catalog_selector(
    raw_input: bytes,
    columns: list[str],
    *,
    gaia_col: str,
    ra_col: str,
    dec_col: str,
) -> tuple[dict[str, Any], str]:
    selector: dict[str, Any] = {
        "csv_sha256": hashlib.sha256(raw_input).hexdigest(),
    }
    if gaia_col in columns:
        selector["column_mapping"] = {"source_id": gaia_col}
        return selector, "source_id"
    if ra_col in columns and dec_col in columns:
        selector["column_mapping"] = {"ra": ra_col, "dec": dec_col}
        selector["match_radius_arcsec"] = CATALOG_MATCH_RADIUS_ARCSEC
        return selector, "position"
    raise ValueError(
        f"Catalog has neither '{gaia_col}' nor ('{ra_col}' + '{dec_col}'). "
        f"Available columns: {columns[:20]}"
    )


def _configured_release() -> str:
    return str(PipelineConfig.load().gaia.release)


def _input_contract(
    release: str,
    input_mode: str,
    limit: Optional[int],
    selector: dict[str, Any],
) -> dict[str, Any]:
    return {
        "release": release,
        "input_mode": input_mode,
        "limit": limit,
        "selector": selector,
    }


def _read_catalog_header(raw_input: bytes) -> pd.DataFrame:
    return pd.read_csv(io.BytesIO(raw_input), nrows=0)


def _read_catalog_csv(raw_input: bytes, *, gaia_col: str) -> pd.DataFrame:
    # Inspect the header first so only the Gaia ID column is forced to pandas'
    # nullable string dtype.  This avoids IEEE-754 rounding when IDs larger
    # than 2**53 share a column with nulls while retaining ordinary numeric
    # parsing for coordinate columns.
    header = _read_catalog_header(raw_input)
    dtype = {gaia_col: "string"} if gaia_col in header.columns else None
    return pd.read_csv(io.BytesIO(raw_input), dtype=dtype)


def _selected_row_mask(row_count: int, limit: Optional[int]) -> list[bool]:
    selected_count = row_count if limit is None else len(pd.RangeIndex(row_count)[:limit])
    return [row_id < selected_count for row_id in range(row_count)]


def _valid_coordinate_mask(
    frame: pd.DataFrame, ra_col: str, dec_col: str
) -> list[bool]:
    ra = pd.to_numeric(frame[ra_col], errors="coerce")
    dec = pd.to_numeric(frame[dec_col], errors="coerce")

    def finite(value: Any) -> bool:
        try:
            return bool(pd.notna(value) and math.isfinite(float(value)))
        except (TypeError, ValueError):
            return False

    return [
        finite(ra_value)
        and finite(dec_value)
        and 0.0 <= float(ra_value) <= 360.0
        and -90.0 <= float(dec_value) <= 90.0
        for ra_value, dec_value in zip(ra, dec)
    ]


def _normalise_source_id(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    text = str(value).strip()
    return (text.lstrip("0") or "0") if text and text.isascii() and text.isdigit() else None


def _fetch_explicit_ids(
    source_ids: list[str], *, id_fetcher: Optional[IdFetcher]
) -> pd.DataFrame:
    if not source_ids:
        return _canonicalize(
            pd.DataFrame(columns=[*CANONICAL_RENAME, *QC_EXTRA_COLUMNS])
        )
    result = id_fetcher(source_ids) if id_fetcher is not None else _fetch_gaia_by_ids(source_ids)
    if not isinstance(result, pd.DataFrame):
        data = getattr(result, "data", None)
        if not isinstance(data, pd.DataFrame):
            raise TypeError("id_fetcher must return a pandas DataFrame")
        result = data
    return _canonicalize(result)


def _fetch_gaia_by_ids(
    source_ids: list[str], *, limit: Optional[int] = None
) -> pd.DataFrame:
    cfg = PipelineConfig.load()
    src = GaiaSource(cfg.gaia, cfg.network)
    res = src._query_by_ids(
        SourceIdQuery(source_ids=source_ids, limit=limit),
        extra_columns=QC_EXTRA_COLUMNS,
    )
    return _canonicalize(res.data)


def _crossmatch_to_gaia(
    df: pd.DataFrame, ra_col: str, dec_col: str, *, limit: Optional[int]
) -> pd.DataFrame:
    """Backward-compatible private position-catalog helper."""
    members, _, _ = _resolve_position_catalog_rows(
        df, ra_col=ra_col, dec_col=dec_col, limit=limit
    )
    if members.empty:
        raise LookupError("No uploaded catalogue rows matched Gaia within 1 arcsec")
    return members.drop(columns=["input_row_id"], errors="ignore")


def _make_input_rows(frame: pd.DataFrame, statuses: list[str]) -> pd.DataFrame:
    raw_json = [_row_to_json(row) for _, row in frame.iterrows()]
    return pd.DataFrame(
        {
            "input_row_id": pd.Series(range(len(frame)), dtype="int64"),
            "ordinal": pd.Series(range(len(frame)), dtype="int64"),
            "status": pd.Series(statuses, dtype="string"),
            "raw_json": pd.Series(raw_json, dtype="string"),
        },
        columns=INPUT_ROW_COLUMNS,
    )


def _make_matches(records: list[dict[str, Any]]) -> pd.DataFrame:
    if not records:
        return _empty_matches()
    frame = pd.DataFrame.from_records(records, columns=MATCH_COLUMNS)
    frame["input_row_id"] = frame["input_row_id"].astype("int64")
    frame["source_id"] = frame["source_id"].astype("string")
    frame["status"] = frame["status"].astype("string")
    frame["match_method"] = frame["match_method"].astype("string")
    frame["match_sep_arcsec"] = pd.to_numeric(
        frame["match_sep_arcsec"], errors="coerce"
    ).astype("Float64")
    frame["match_candidate_count"] = pd.to_numeric(
        frame["match_candidate_count"], errors="coerce"
    ).astype("Int64")
    return frame


def _row_to_json(row: pd.Series) -> str:
    payload = {str(column): _json_scalar(value) for column, value in row.items()}
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_scalar(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _canonicalize(df: pd.DataFrame) -> pd.DataFrame:
    """Rename Gaia native columns to canonical names; ensure Gaia is str.

    Even empty DataFrames are renamed so they can be concatenated with
    populated ones downstream.
    """
    df = df.rename(columns=CANONICAL_RENAME).copy()
    if "Gaia" in df.columns:
        df["Gaia"] = df["Gaia"].astype(str)
    return df
