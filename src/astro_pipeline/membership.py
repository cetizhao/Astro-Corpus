"""Discover published membership catalogues and turn one into a Gaia corpus.

The existing ``region`` intake mode answers a geometric question: *which Gaia
sources lie inside this cone?*  It cannot establish physical membership in a
cluster or association.  This module implements the separate, literature-led
workflow needed for that question:

1. search VizieR for published membership catalogues;
2. preview the candidate tables and their scientific columns;
3. fetch the explicitly selected table without applying hidden quality cuts;
4. cross-match its rows to Gaia DR3 while retaining catalogue provenance.

VizieR is used as the catalogue registry and data service.  The original table
is always saved alongside the Gaia-normalised ``members`` table so downstream
users can audit membership probabilities, flags, populations and citations.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import quote

import pandas as pd
from astroquery.vizier import Vizier

from astro_pipeline.config import PipelineConfig
from astro_pipeline.intake import QC_EXTRA_COLUMNS, _canonicalize
from astro_pipeline.models.query import SourceIdQuery
from astro_pipeline.manifest import CorpusManifest, validate_label
from astro_pipeline.sources.gaia import GaiaSource

logger = logging.getLogger(__name__)

VIZIER_SOURCE_URL = "https://vizier.cds.unistra.fr/viz-bin/VizieR?-source={}"


@dataclass
class MembershipResolution:
    """A selected literature table and its Gaia DR3 cross-match."""

    catalog_id: str
    table_id: str
    table_description: str
    catalog_title: str
    original: pd.DataFrame
    members: pd.DataFrame
    unmatched: pd.DataFrame
    match_method: str
    match_radius_arcsec: Optional[float]


def search_membership_catalogs(target: str, max_results: int = 10) -> dict[str, Any]:
    """Search VizieR for published member lists for ``target``.

    The call intentionally returns catalogue-level candidates only.  It is a
    quick discovery pass; callers should use :func:`preview_membership_catalog`
    before selecting a table for download.
    """
    target = target.strip()
    if not target:
        raise ValueError("target must not be empty")
    if max_results < 1 or max_results > 50:
        raise ValueError("max_results must be between 1 and 50")

    queries = [
        f"{target} membership",
        f"{target} members",
        f"{target} census",
        f"{target} candidate members",
    ]
    merged: dict[str, dict[str, Any]] = {}
    scan_limit = min(100, max(20, max_results * 4))

    for query_index, query in enumerate(queries):
        try:
            found = Vizier.find_catalogs(query, max_catalogs=scan_limit)
        except Exception as exc:
            logger.warning("VizieR catalogue search failed for %r: %s", query, exc)
            continue
        for rank, (catalog_id, resource) in enumerate(found.items()):
            title = str(getattr(resource, "description", "") or catalog_id).strip()
            score, signals = _membership_score(
                target=target,
                catalog_id=catalog_id,
                title=title,
                query_index=query_index,
                rank=rank,
            )
            row = merged.setdefault(
                catalog_id,
                {
                    "catalog_id": catalog_id,
                    "title": title,
                    "provider": "CDS VizieR",
                    "url": VIZIER_SOURCE_URL.format(quote(catalog_id, safe="/")),
                    "score": score,
                    "relevance_signals": signals,
                    "matched_queries": [],
                },
            )
            row["score"] = max(int(row["score"]), score)
            row["relevance_signals"] = sorted(
                set(row["relevance_signals"]) | set(signals)
            )
            row["matched_queries"].append(query)

    candidates = sorted(
        merged.values(),
        key=lambda item: (-int(item["score"]), str(item["catalog_id"])),
    )[:max_results]
    return {
        "target": target,
        "query_kind": "published_membership_catalogues",
        "provider": "CDS VizieR",
        "n_candidates": len(candidates),
        "candidates": candidates,
        "next_step": (
            "Preview one or more catalog_id values, inspect table descriptions "
            "and membership columns, then explicitly choose a table_id."
        ),
        "warning": (
            "A catalogue candidate is not automatically authoritative for every "
            "science case. The pipeline does not silently filter membership "
            "probabilities or candidate flags."
        ),
    }


def preview_membership_catalog(
    catalog_id: str,
    *,
    table_id: Optional[str] = None,
    sample_size: int = 8,
) -> dict[str, Any]:
    """Return table structure and sample rows for a VizieR catalogue."""
    catalog_id = _validate_vizier_id(catalog_id, "catalog_id")
    if table_id is not None:
        table_id = _validate_vizier_id(table_id, "table_id")
    if sample_size < 1 or sample_size > 50:
        raise ValueError("sample_size must be between 1 and 50")

    vizier = Vizier(columns=["**"], row_limit=sample_size)
    tables = vizier.get_catalogs(catalog_id)
    if not tables:
        raise LookupError(f"VizieR returned no tables for {catalog_id!r}")

    metadata = _catalog_metadata(vizier, catalog_id)
    selected = _select_tables(tables, table_id=table_id, require_one=False)
    previews: list[dict[str, Any]] = []
    for current_id, table in selected:
        frame = _table_to_dataframe(table)
        detection = _detect_columns(table)
        role = _table_role(str(table.meta.get("description", "")))
        previews.append(
            {
                "table_id": current_id,
                "description": str(table.meta.get("description", "") or ""),
                "role": role,
                "recommended_member_table": role == "members",
                "sample_row_count": len(frame),
                "row_count": None,
                "row_count_note": (
                    "Preview is deliberately row-limited; exact count is recorded "
                    "when the selected table is downloaded."
                ),
                "columns": _column_metadata(table),
                **detection,
                "sample_rows": _json_records(frame),
            }
        )

    return {
        "catalog_id": catalog_id,
        "provider": "CDS VizieR",
        "url": VIZIER_SOURCE_URL.format(quote(catalog_id, safe="/")),
        **metadata,
        "n_tables": len(previews),
        "tables": previews,
        "selection_note": (
            "Prefer a table whose role is 'members' when the request is for "
            "established members. A 'candidates' table is a different sample."
        ),
    }


def resolve_membership_catalog(
    catalog_id: str,
    *,
    table_id: Optional[str] = None,
    limit: Optional[int] = None,
    match_radius_arcsec: float = 3.0,
    match_batch_size: int = 25,
) -> MembershipResolution:
    """Fetch one selected membership table and cross-match it to Gaia DR3.

    ``limit`` is applied to catalogue rows *before* Gaia matching and is mainly
    intended for small tests.  No probability, quality, population or
    candidate/member filters are applied.
    """
    catalog_id = _validate_vizier_id(catalog_id, "catalog_id")
    if table_id is not None:
        table_id = _validate_vizier_id(table_id, "table_id")
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive or None")

    # Fetch the full selected VizieR table.  row_limit=-1 is important:
    # astroquery otherwise defaults to a small preview-sized result.
    vizier = Vizier(columns=["**"], row_limit=-1)
    tables = vizier.get_catalogs(catalog_id)
    if not tables:
        raise LookupError(f"VizieR returned no tables for {catalog_id!r}")
    selected_id, table = _select_tables(
        tables, table_id=table_id, require_one=True
    )[0]
    metadata = _catalog_metadata(vizier, catalog_id)
    description = str(table.meta.get("description", "") or "")
    original = _table_to_dataframe(table)
    if limit is not None:
        original = original.head(limit).copy()
    original.insert(0, "membership_catalog_row", range(len(original)))
    if original.empty:
        raise LookupError(f"Selected VizieR table {selected_id!r} is empty")

    detection = _detect_columns(table)
    gaia_dr3_columns = detection["gaia_dr3_id_columns"]
    coordinate_pair = detection["coordinate_columns"]
    config = PipelineConfig.load()
    gaia = GaiaSource(config.gaia, config.network)

    if gaia_dr3_columns:
        source_col = gaia_dr3_columns[0]
        members, unmatched = _match_by_gaia_id(
            original,
            gaia=gaia,
            source_col=source_col,
            coordinate_pair=coordinate_pair,
        )
        match_method = f"gaia_dr3_source_id:{source_col}"
        radius: Optional[float] = None
    elif coordinate_pair:
        members, unmatched = _match_by_position(
            original,
            gaia=gaia,
            ra_col=coordinate_pair[0],
            dec_col=coordinate_pair[1],
            radius_arcsec=match_radius_arcsec,
            batch_size=match_batch_size,
        )
        match_method = "gaia_dr3_position"
        radius = match_radius_arcsec
    else:
        raise ValueError(
            f"Table {selected_id!r} has neither a Gaia DR3 source-id column "
            "nor a recognized RA/Dec pair; inspect preview columns and provide "
            "a different member table."
        )

    provenance = {
        "membership_catalog_id": catalog_id,
        "membership_table_id": selected_id,
        "membership_table_description": description,
        "membership_catalog_title": str(metadata.get("title", "")),
        "membership_match_method": match_method,
        "membership_match_radius_arcsec": radius,
    }
    for key, value in provenance.items():
        members[key] = value
        unmatched[key] = value

    return MembershipResolution(
        catalog_id=catalog_id,
        table_id=selected_id,
        table_description=description,
        catalog_title=str(metadata.get("title", "")),
        original=original,
        members=members,
        unmatched=unmatched,
        match_method=match_method,
        match_radius_arcsec=radius,
    )


def build_membership_corpus(
    *,
    label: str,
    out_dir: Path | str,
    catalog_id: str,
    table_id: Optional[str] = None,
    limit: Optional[int] = None,
    match_radius_arcsec: float = 3.0,
    match_batch_size: int = 25,
    skip_simbad: bool = False,
    sleep_seconds: float = 0.3,
    enrich: bool = True,
    xp_sampled: bool = False,
    xp_continuous: bool = False,
) -> dict[str, Any]:
    """Resolve a selected member table, save provenance, and build a corpus."""
    label = validate_label(label)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    limit_text = "all" if limit is None else str(limit)
    input_summary = (
        f"VizieR catalog={catalog_id}, table={table_id or 'auto'}, "
        f"limit={limit_text}, match_radius={match_radius_arcsec}arcsec"
    )
    manifest = CorpusManifest.open_or_create(
        out_dir / "manifest.json",
        label=label,
        input_mode="membership_catalog",
        input_summary=input_summary,
        requested_products={
            "members": True,
            "membership_catalog": True,
            "simbad_papers": not skip_simbad,
            "visualization": not skip_simbad,
            "gaia_xp_sampled": xp_sampled,
            "gaia_xp_continuous": xp_continuous,
        },
    )
    manifest.update_stage("resolve_members", status="running", completed=0)
    try:
        resolution = resolve_membership_catalog(
            catalog_id,
            table_id=table_id,
            limit=limit,
            match_radius_arcsec=match_radius_arcsec,
            match_batch_size=match_batch_size,
        )
    except Exception as exc:
        manifest.update_stage("resolve_members", status="failed", error=str(exc))
        manifest.add_error(f"membership catalogue resolution failed: {exc}")
        manifest.finish("failed")
        raise
    manifest.update_stage(
        "resolve_members",
        status="completed",
        total=len(resolution.original),
        completed=len(resolution.members),
        unmatched=len(resolution.unmatched),
        catalog_id=resolution.catalog_id,
        table_id=resolution.table_id,
        match_method=resolution.match_method,
    )

    original_csv = out_dir / "membership_catalog.csv"
    original_parquet = out_dir / "membership_catalog.parquet"
    unmatched_csv = out_dir / "unmatched_members.csv"
    resolution.original.to_csv(original_csv, index=False)
    resolution.original.to_parquet(original_parquet, index=False)
    resolution.unmatched.to_csv(unmatched_csv, index=False)
    manifest.add_artifact(
        "membership_catalog_csv", original_csv, rows=len(resolution.original)
    )
    manifest.add_artifact(
        "membership_catalog_parquet", original_parquet, rows=len(resolution.original)
    )
    manifest.add_artifact(
        "unmatched_members_csv", unmatched_csv, rows=len(resolution.unmatched)
    )

    from astro_pipeline.corpus_builder import build_corpus_from_members, result_to_dict

    result = build_corpus_from_members(
        label=label,
        out_dir=out_dir,
        members=resolution.members,
        input_mode="membership_catalog",
        input_summary=input_summary,
        skip_simbad=skip_simbad,
        sleep_seconds=sleep_seconds,
        enrich=enrich,
        xp_sampled=xp_sampled,
        xp_continuous=xp_continuous,
        manifest=manifest,
    )
    output = result_to_dict(result)
    output.update(
        {
            "membership_catalog_id": resolution.catalog_id,
            "membership_table_id": resolution.table_id,
            "membership_table_description": resolution.table_description,
            "membership_catalog_title": resolution.catalog_title,
            "membership_match_method": resolution.match_method,
            "membership_match_radius_arcsec": resolution.match_radius_arcsec,
            "n_catalog_rows": len(resolution.original),
            "n_gaia_matched": len(resolution.members),
            "n_unmatched": len(resolution.unmatched),
            "membership_catalog_csv": str(original_csv.resolve()),
            "membership_catalog_parquet": str(original_parquet.resolve()),
            "unmatched_members_csv": str(unmatched_csv.resolve()),
            "filtering_applied": False,
        }
    )
    return output


def _match_by_gaia_id(
    original: pd.DataFrame,
    *,
    gaia: GaiaSource,
    source_col: str,
    coordinate_pair: Optional[list[str]],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    work = original.copy()
    work["_membership_gaia_id"] = work[source_col].map(_normalise_gaia_id)
    valid = work["_membership_gaia_id"].notna()
    ids = work.loc[valid, "_membership_gaia_id"].drop_duplicates().tolist()
    gaia_frames: list[pd.DataFrame] = []
    for start in range(0, len(ids), 2000):
        result = gaia._query_by_ids(
            SourceIdQuery(source_ids=ids[start : start + 2000]),
            extra_columns=QC_EXTRA_COLUMNS + ["has_xp_sampled", "has_xp_continuous"],
        )
        gaia_frames.append(result.data)
    native = pd.concat(gaia_frames, ignore_index=True) if gaia_frames else pd.DataFrame()
    canonical = _canonicalize(native)
    if canonical.empty:
        unmatched = work.copy()
        member = _prefix_member_columns(work.iloc[0:0])
        return _clean_internal(pd.concat([canonical, member], axis=1)), _clean_internal(unmatched)

    canonical["Gaia"] = canonical["Gaia"].map(_normalise_gaia_id)
    member = _prefix_member_columns(work)
    matched = member.merge(
        canonical,
        left_on="member__membership_gaia_id",
        right_on="Gaia",
        how="inner",
        validate="many_to_one",
    )
    matched = matched.rename(
        columns={"member_membership_catalog_row": "membership_catalog_row"}
    )
    matched_ids = set(matched["membership_catalog_row"].tolist())
    unmatched = work.loc[~work["membership_catalog_row"].isin(matched_ids)].copy()
    matched = _clean_internal(matched)
    matched["membership_match_candidate_count"] = 1

    if coordinate_pair and not matched.empty:
        from astropy import units as u
        from astropy.coordinates import SkyCoord

        ra_col, dec_col = coordinate_pair
        cat_coord = SkyCoord(
            pd.to_numeric(matched[f"member_{ra_col}"], errors="coerce").to_numpy() * u.deg,
            pd.to_numeric(matched[f"member_{dec_col}"], errors="coerce").to_numpy() * u.deg,
        )
        gaia_coord = SkyCoord(
            pd.to_numeric(matched["RAICRS"], errors="coerce").to_numpy() * u.deg,
            pd.to_numeric(matched["DEICRS"], errors="coerce").to_numpy() * u.deg,
        )
        matched["membership_match_sep_arcsec"] = cat_coord.separation(gaia_coord).arcsec
    else:
        matched["membership_match_sep_arcsec"] = pd.NA
    return matched, _clean_internal(unmatched)


def _match_by_position(
    original: pd.DataFrame,
    *,
    gaia: GaiaSource,
    ra_col: str,
    dec_col: str,
    radius_arcsec: float,
    batch_size: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    native = gaia.crossmatch_positions(
        original,
        ra_col=ra_col,
        dec_col=dec_col,
        row_id_col="membership_catalog_row",
        radius_arcsec=radius_arcsec,
        batch_size=batch_size,
        extra_columns=QC_EXTRA_COLUMNS + ["has_xp_sampled", "has_xp_continuous"],
    )
    canonical = _canonicalize(native)
    member = _prefix_member_columns(original)
    matched = canonical.merge(
        member,
        left_on="membership_catalog_row",
        right_on="member_membership_catalog_row",
        how="inner",
        validate="one_to_one",
    )
    matched = matched.drop(columns=["member_membership_catalog_row"])
    matched = matched.rename(
        columns={
            "match_sep_arcsec": "membership_match_sep_arcsec",
            "match_candidate_count": "membership_match_candidate_count",
        }
    )
    matched_rows = set(matched["membership_catalog_row"].tolist())
    unmatched = original.loc[
        ~original["membership_catalog_row"].isin(matched_rows)
    ].copy()
    return matched, unmatched


def _prefix_member_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Namespace every literature column so Gaia canonical names stay stable."""
    return frame.rename(columns={column: f"member_{column}" for column in frame.columns})


def _clean_internal(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.drop(
        columns=["_membership_gaia_id", "member__membership_gaia_id"],
        errors="ignore",
    ).reset_index(drop=True)


def _membership_score(
    *, target: str, catalog_id: str, title: str, query_index: int, rank: int
) -> tuple[int, list[str]]:
    text = f"{catalog_id} {title}".casefold()
    target_text = target.casefold()
    score = max(0, 12 - query_index * 2 - min(rank, 10))
    signals: list[str] = []
    if target_text in text:
        score += 35
        signals.append("target name")
    for term, weight, signal in (
        ("membership", 50, "membership"),
        ("members", 42, "members"),
        ("census", 38, "census"),
        ("candidate", 10, "candidate list"),
        ("gaia", 8, "Gaia-based"),
    ):
        if term in text:
            score += weight
            signals.append(signal)
    if "photometr" in text and not ({"membership", "members"} & set(signals)):
        score -= 10
    return score, signals


def _catalog_metadata(vizier: Vizier, catalog_id: str) -> dict[str, Any]:
    try:
        table = vizier.get_catalog_metadata(catalog=catalog_id)
    except Exception as exc:
        logger.warning("Could not fetch VizieR metadata for %s: %s", catalog_id, exc)
        return {}
    if table is None or len(table) == 0:
        return {}
    row = table.to_pandas().iloc[0].to_dict()
    return {str(key): _json_value(value) for key, value in row.items()}


def _iter_tables(tables: Any) -> list[tuple[str, Any]]:
    keys = list(tables.keys())
    if isinstance(tables, Mapping):
        return [(str(key), tables[key]) for key in keys]
    return [(str(key), table) for key, table in zip(keys, tables)]


def _select_tables(
    tables: Any, *, table_id: Optional[str], require_one: bool
) -> list[tuple[str, Any]]:
    items = _iter_tables(tables)
    if table_id:
        needle = table_id.casefold().rstrip("/")
        selected = [
            item
            for item in items
            if item[0].casefold().rstrip("/") == needle
            or item[0].casefold().rstrip("/").endswith("/" + needle)
        ]
        if not selected:
            available = ", ".join(key for key, _ in items)
            raise LookupError(
                f"VizieR table {table_id!r} not found; available: {available}"
            )
        return selected
    if not require_one:
        return items
    if len(items) == 1:
        return items
    established = [
        item
        for item in items
        if _table_role(str(item[1].meta.get("description", ""))) == "members"
    ]
    if len(established) == 1:
        return established
    available = ", ".join(
        f"{key} ({str(table.meta.get('description', ''))})" for key, table in items
    )
    raise ValueError(
        "The catalogue contains multiple plausible tables; preview it and pass "
        f"table_id explicitly. Available: {available}"
    )


def _table_role(description: str) -> str:
    text = description.casefold()
    if "candidate" in text:
        return "candidates"
    if "member" in text or "membership" in text:
        return "members"
    return "other"


def _detect_columns(table: Any) -> dict[str, Any]:
    columns = list(table.colnames)
    descriptions = {
        name: str(getattr(table[name], "description", "") or "") for name in columns
    }
    dr3: list[str] = []
    older_gaia: list[str] = []
    membership: list[str] = []
    for name in columns:
        normalized = _normalise_name(name)
        text = f"{name} {descriptions[name]}".casefold()
        is_id_like = "id" in normalized or "source" in normalized or "gaia" in normalized
        if is_id_like and ("gaia dr3" in text or "gaiadr3" in normalized):
            dr3.append(name)
        elif "gaia dr2" in text or "gaiadr2" in normalized or "edr3" in text:
            older_gaia.append(name)
        if any(
            token in text
            for token in (
                "membership",
                "member probability",
                "membership probability",
                "population",
                "candidate flag",
                "member flag",
            )
        ) or normalized in {"pmember", "membership", "member", "pop", "class"}:
            membership.append(name)

    coordinates = _find_coordinate_pair(columns)
    return {
        "gaia_dr3_id_columns": dr3,
        "older_gaia_id_columns": older_gaia,
        "coordinate_columns": list(coordinates) if coordinates else None,
        "membership_columns": membership,
        "gaia_match_ready": bool(dr3 or coordinates),
        "match_strategy": (
            "gaia_dr3_source_id"
            if dr3
            else "position_to_gaia_dr3"
            if coordinates
            else "unsupported_without_column_mapping"
        ),
    }


def _find_coordinate_pair(columns: Iterable[str]) -> Optional[tuple[str, str]]:
    by_normalized = {_normalise_name(column): column for column in columns}
    pairs = (
        ("raj2000", "dej2000"),
        ("raicrs", "deicrs"),
        ("raj", "dej"),
        ("ra", "dec"),
    )
    for ra_name, dec_name in pairs:
        if ra_name in by_normalized and dec_name in by_normalized:
            return by_normalized[ra_name], by_normalized[dec_name]
    return None


def _column_metadata(table: Any) -> list[dict[str, Any]]:
    out = []
    for name in table.colnames:
        column = table[name]
        out.append(
            {
                "name": name,
                "dtype": str(column.dtype),
                "unit": str(column.unit) if column.unit is not None else None,
                "description": str(column.description or ""),
            }
        )
    return out


def _table_to_dataframe(table: Any) -> pd.DataFrame:
    frame = table.to_pandas()
    for column in frame.columns:
        if frame[column].map(lambda value: isinstance(value, bytes)).any():
            frame[column] = frame[column].map(
                lambda value: value.decode("utf-8", errors="replace")
                if isinstance(value, bytes)
                else value
            )
    return frame


def _normalise_gaia_id(value: Any) -> Optional[str]:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    text = re.sub(r"^Gaia\s+DR3\s+", "", text, flags=re.IGNORECASE)
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    return text if text.isdigit() else None


def _normalise_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _validate_vizier_id(value: str, name: str) -> str:
    value = value.strip()
    if not value or not re.fullmatch(r"[A-Za-z0-9+_.-]+(?:/[A-Za-z0-9+_.-]+)*", value):
        raise ValueError(f"Invalid VizieR {name}: {value!r}")
    return value


def _json_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        {str(key): _json_value(value) for key, value in row.items()}
        for row in frame.to_dict(orient="records")
    ]


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        missing = pd.isna(value)
        if isinstance(missing, bool) and missing:
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except (TypeError, ValueError):
            pass
    return value


def resolution_metadata(resolution: MembershipResolution) -> dict[str, Any]:
    """Serialize non-tabular resolution metadata for manifests/tests."""
    raw = asdict(resolution)
    for key in ("original", "members", "unmatched"):
        raw.pop(key, None)
    return raw
