"""Runtime capability discovery for Gaia TAP and DataLink services."""
from __future__ import annotations

import ast
import io
import re
from importlib.metadata import distribution
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

import httpx
import pandas as pd
from astropy.io.votable import parse_single_table

from astro_pipeline.gaia_releases import (
    GaiaReleaseProfile,
    get_gaia_release_profile,
    validate_gaia_schema,
)

QueryRunner = Callable[[str], pd.DataFrame]
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
QUALIFIED_IDENTIFIER = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$"
)
DATALINK_LINKS_URL = "https://gea.esac.esa.int/data-server/datalink/links"


def probe_gaia_release(
    release: str,
    query_runner: QueryRunner,
    *,
    schema_override: Optional[str] = None,
) -> dict[str, Any]:
    """Probe TAP_SCHEMA without assuming that a provisional schema exists."""
    profile = get_gaia_release_profile(release)
    result: dict[str, Any] = {
        "release": profile.key,
        "profile": profile.to_dict(),
        "checked_at": _utc_now(),
        "archive_reachable": False,
        "published_in_tap": False,
        "resolved_schema": None,
        "tables": [],
        "expected_tables": [
            {"table": name, "available": False}
            for name in profile.expected_source_tables
        ],
        "warnings": [],
    }
    try:
        schemas = _normalize_columns(query_runner(_schemas_adql()))
        result["archive_reachable"] = True
    except Exception as exc:
        result["error"] = str(exc)
        result["warnings"].append(
            "TAP_SCHEMA could not be queried; profile data are local hints only"
        )
        return result

    schema = _select_schema(profile, schemas, schema_override=schema_override)
    result["schemas_seen"] = _records(schemas)
    if schema is None:
        result["warnings"].append(
            f"No TAP schema matching {profile.display_name} is currently discoverable"
        )
        return result

    result["resolved_schema"] = schema
    try:
        tables = _normalize_columns(query_runner(_tables_adql(schema)))
    except Exception as exc:
        result["error"] = str(exc)
        result["warnings"].append(
            f"Schema {schema!r} was found but its tables could not be listed"
        )
        return result

    table_names = {_unqualified_table_name(value) for value in _column(tables, "table_name")}
    result["published_in_tap"] = bool(table_names)
    result["tables"] = _records(tables)
    result["expected_tables"] = [
        {"table": name, "available": name.lower() in table_names}
        for name in profile.expected_source_tables
    ]
    if profile.status == "provisional":
        result["warnings"].append(
            "DR4 profile is provisional; discovered Archive metadata overrides local expectations"
        )
    return result


def describe_gaia_table(
    release: str,
    table: str,
    query_runner: QueryRunner,
    *,
    schema_override: Optional[str] = None,
) -> dict[str, Any]:
    """Return live TAP_SCHEMA column metadata for a release table."""
    profile = get_gaia_release_profile(release)
    schema, table_name = _resolve_qualified_table(
        profile,
        table,
        query_runner,
        schema_override=schema_override,
    )
    qualified = f"{schema}.{table_name}"
    columns = _normalize_columns(query_runner(_columns_adql(qualified)))
    return {
        "release": profile.key,
        "archive_tag": profile.archive_tag,
        "schema": schema,
        "table": table_name,
        "qualified_table": qualified,
        "n_columns": len(columns),
        "columns": _records(columns),
        "checked_at": _utc_now(),
    }


def _installed_datalink_constants() -> dict[str, set[str]]:
    # Importing astroquery.gaia instantiates its singleton and fetches server
    # notifications, even when only local capability declarations are needed.
    names = {"VALID_DATALINK_RETRIEVAL_TYPES", "VALID_LINKING_PARAMETERS"}
    try:
        source = distribution("astroquery").locate_file("astroquery/gaia/__init__.py")
        tree = ast.parse(source.read_text(encoding="utf-8"))
        conf = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Conf")
        values = {}
        for node in conf.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in names:
                    value = ast.literal_eval(node.value)
                    if isinstance(value, (list, tuple, set)) and all(isinstance(item, str) for item in value):
                        values[target.id] = {item.upper() for item in value}
        return values
    except (OSError, ValueError, TypeError, SyntaxError, StopIteration, ImportError):
        return {}


def list_gaia_datalink_products(
    release: str,
    *,
    source_id: Optional[str] = None,
    http_get: Optional[Callable[..., Any]] = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """List client-declared products and optionally source-specific live links."""
    profile = get_gaia_release_profile(release)
    constants = _installed_datalink_constants()
    client_products = constants.get("VALID_DATALINK_RETRIEVAL_TYPES")
    result: dict[str, Any] = {
        "release": profile.key,
        "archive_tag": profile.archive_tag,
        "profile_status": profile.status,
        "client_declared_products": [
            {
                "retrieval_type": product,
                "declared_by_profile": True,
                "accepted_by_installed_astroquery": (
                    product in client_products if client_products is not None else None
                ),
                "archive_confirmed_for_source": None,
            }
            for product in profile.datalink_products
        ],
        "formats": list(profile.datalink_formats),
        "data_structures": list(profile.datalink_structures),
        "linking_parameters": sorted(constants.get("VALID_LINKING_PARAMETERS", set())),
        "client_metadata_loaded": client_products is not None,
        "source_id": source_id,
        "live_links": [],
        "warnings": [
            "Client-declared products are compatibility hints, not proof that the Archive currently serves them"
        ],
    }
    if client_products is None:
        result["warnings"].append(
            "Installed astroquery declarations could not be read locally; client acceptance is unknown"
        )
    if source_id is None:
        return result
    source_id = str(source_id).strip()
    if not source_id.isdigit():
        raise ValueError("Gaia source_id must contain digits only")

    getter = http_get or httpx.get
    response = getter(
        DATALINK_LINKS_URL,
        params={"ID": source_id, "RELEASE": profile.archive_tag},
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    table = parse_single_table(io.BytesIO(response.content)).to_table(use_names_over_ids=True)
    frame = _normalize_columns(table.to_pandas())
    links = _records(frame)
    for link in links:
        access_url = str(link.get("access_url") or "")
        query = parse_qs(urlparse(access_url).query)
        retrieval = (query.get("RETRIEVAL_TYPE") or query.get("retrieval_type") or [""])[0]
        link["retrieval_type"] = str(retrieval).upper() or None
    confirmed = {str(link.get("retrieval_type") or "") for link in links}
    for product in result["client_declared_products"]:
        product["archive_confirmed_for_source"] = (
            product["retrieval_type"] in confirmed
        )
    result["live_links"] = links
    result["checked_at"] = _utc_now()
    return result


def _resolve_qualified_table(
    profile: GaiaReleaseProfile,
    table: str,
    query_runner: QueryRunner,
    *,
    schema_override: Optional[str],
) -> tuple[str, str]:
    table = str(table).strip()
    if "." in table:
        if not QUALIFIED_IDENTIFIER.fullmatch(table):
            raise ValueError(f"invalid qualified Gaia table: {table!r}")
        schema, table_name = table.split(".", maxsplit=1)
        return validate_gaia_schema(profile.key, schema), table_name
    if not IDENTIFIER.fullmatch(table):
        raise ValueError(f"invalid Gaia table name: {table!r}")
    schemas = _normalize_columns(query_runner(_schemas_adql()))
    schema = _select_schema(profile, schemas, schema_override=schema_override)
    if schema is None:
        raise LookupError(
            f"No TAP schema for {profile.display_name} is currently discoverable; "
            "do not guess a schema name"
        )
    return schema, table


def _select_schema(
    profile: GaiaReleaseProfile,
    schemas: pd.DataFrame,
    *,
    schema_override: Optional[str],
) -> Optional[str]:
    available = {str(value).strip().lower(): str(value).strip() for value in _column(schemas, "schema_name")}
    if schema_override:
        override = validate_gaia_schema(
            profile.key, _validate_identifier(schema_override, "schema")
        )
        return available.get(override.lower())
    candidates = []
    if profile.tap_schema:
        candidates.append(profile.tap_schema)
    candidates.extend(profile.schema_candidates)
    for candidate in candidates:
        if candidate.lower() in available:
            return available[candidate.lower()]
    # If the final Archive chooses a schema name not anticipated locally, use
    # its live description rather than adding another guessed alias.
    if "description" in schemas.columns and "schema_name" in schemas.columns:
        release_label = profile.display_name.lower()
        described = [
            str(row["schema_name"]).strip()
            for _, row in schemas.iterrows()
            if release_label in str(row.get("description", "")).lower()
        ]
        if len(described) == 1:
            return described[0]
    return None


def _schemas_adql() -> str:
    return (
        "SELECT schema_name, description FROM TAP_SCHEMA.schemas "
        "ORDER BY schema_name ASC"
    )


def _tables_adql(schema: str) -> str:
    schema = _validate_identifier(schema, "schema")
    return (
        "SELECT schema_name, table_name, description FROM TAP_SCHEMA.tables "
        f"WHERE schema_name = '{schema}' ORDER BY table_name ASC"
    )


def _columns_adql(qualified_table: str) -> str:
    if not QUALIFIED_IDENTIFIER.fullmatch(qualified_table):
        raise ValueError(f"invalid qualified Gaia table: {qualified_table!r}")
    return (
        "SELECT table_name, column_name, datatype, arraysize, unit, ucd, description "
        "FROM TAP_SCHEMA.columns "
        f"WHERE table_name = '{qualified_table}' ORDER BY column_name ASC"
    )


def _validate_identifier(value: str, kind: str) -> str:
    value = str(value).strip()
    if not IDENTIFIER.fullmatch(value):
        raise ValueError(f"invalid Gaia {kind}: {value!r}")
    return value


def _normalize_columns(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out.columns = [str(column).strip().lower() for column in out.columns]
    return out


def _column(frame: pd.DataFrame, name: str) -> list[Any]:
    if name not in frame.columns:
        return []
    return frame[name].dropna().tolist()


def _unqualified_table_name(value: Any) -> str:
    return str(value).strip().split(".")[-1].lower()


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    clean = frame.where(pd.notna(frame), None)
    return clean.to_dict(orient="records")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
