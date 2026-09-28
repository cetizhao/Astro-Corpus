"""Non-destructive library import, verification, reconciliation, and export."""
from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Mapping, Sequence
import uuid

from astro_pipeline.gaia_releases import (
    get_gaia_release_profile,
    normalize_gaia_release,
)
from astro_pipeline.library.files import (
    BlobIntegrityError,
    atomic_copy,
    atomic_json,
    sha256_file,
)
from astro_pipeline.library.identity import (
    identity_hash,
    json_value,
    normalize_identifier,
    product_item_key,
)
from astro_pipeline.library.query import Library
from astro_pipeline.library.store import LibraryBusyError, LibraryStore


_METADATA_SUFFIXES = frozenset({".csv", ".ecsv", ".json", ".parquet", ".txt", ".yaml", ".yml"})
_MEMBER_NAMES = (
    "members.parquet",
    "members.csv",
    "membership_catalog.parquet",
    "membership_catalog.csv",
)
_COMPATIBILITY_VIEWS = frozenset(
    {
        "manifest.json",
        "members.csv",
        "members.parquet",
        "input_rows.csv",
        "input_rows.parquet",
        "input_matches.csv",
        "input_matches.parquet",
        "paper_edges.parquet",
        "simbad_refs.parquet",
        "product_refs.json",
    }
)
_PRODUCT_CONTRACT_FIELDS = frozenset(
    {
        "release",
        "archive_tag",
        "retrieval_type",
        "data_structure",
        "output_format",
        "linking_parameter",
        "valid_data",
    }
)
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")


def _public_inventory(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {str(key): value for key, value in record.items() if not str(key).startswith("_")}
        for record in records
    ]


def _public_product_groups(
    source: Path, groups: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    return [
        {
            "contract": dict(group["contract"]),
            "identifier": group["identifier"],
            "artifacts": [
                {
                    "role": artifact["role"],
                    "slot": artifact["slot"],
                    "sha256": artifact["sha256"],
                    "source_relative_path": artifact["path"]
                    .relative_to(source)
                    .as_posix(),
                }
                for artifact in group["artifacts"]
            ],
        }
        for group in groups
    ]


def _category(relative_path: str, suffix: str) -> str:
    name = Path(relative_path).name.lower()
    if name == "manifest.json":
        return "manifest"
    if name in _MEMBER_NAMES or name.startswith("input_rows") or name.startswith("input_matches"):
        return "members"
    if name in {"paper_edges.parquet", "simbad_refs.parquet", "simbad_refs.checkpoint.json"}:
        return "paper_evidence"
    if "product_refs" in name and suffix == ".json":
        return "product_references"
    if name in {"product_request.json", "artifacts.json", "checkpoint.json"}:
        return "product_metadata"
    if suffix in {".fits", ".fit", ".vot", ".xml"}:
        return "product_candidate"
    if suffix in _METADATA_SUFFIXES:
        return "metadata"
    return "unknown"


def _inventory_file(path: Path, relative_path: str) -> dict[str, Any]:
    if path.is_symlink():
        return {
            "relative_path": relative_path,
            "kind": "symlink",
            "target": os.readlink(path),
            "managed_copy": False,
            "_source_path": path,
        }
    size = path.stat().st_size
    suffix = path.suffix.lower()
    category = _category(relative_path, suffix)
    return {
        "relative_path": relative_path,
        "kind": "file",
        "category": category,
        "size_bytes": size,
        "sha256": sha256_file(path),
        "managed_copy": category
        in {
            "manifest",
            "members",
            "paper_evidence",
            "product_references",
            "product_metadata",
            "metadata",
        },
        "_source_path": path,
    }


def inventory_corpus(
    source: Path | str,
    *,
    catalog: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Hash a legacy tree without writing to it or to the library."""
    source = Path(source).expanduser().resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"legacy corpus directory does not exist: {source}")
    records: list[dict[str, Any]] = []
    for root, directory_names, file_names in os.walk(source, followlinks=False):
        root_path = Path(root)
        directory_names[:] = sorted(directory_names)
        for name in sorted(file_names):
            path = root_path / name
            relative = path.relative_to(source).as_posix()
            records.append(_inventory_file(path, relative))
        for name in list(directory_names):
            path = root_path / name
            if path.is_symlink():
                relative = path.relative_to(source).as_posix()
                records.append(_inventory_file(path, relative))
                directory_names.remove(name)
    if catalog is not None:
        catalog_path = Path(catalog).expanduser().resolve()
        if not catalog_path.is_file():
            raise FileNotFoundError(f"catalog does not exist: {catalog_path}")
        try:
            catalog_path.relative_to(source)
        except ValueError:
            record = _inventory_file(
                catalog_path, f"@catalog/{catalog_path.name}"
            )
            record["category"] = "members"
            record["managed_copy"] = True
            records.append(record)
    return sorted(records, key=lambda record: str(record["relative_path"]))


def _manifest_releases(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {"gaia_release", "release"} and isinstance(item, str):
                try:
                    found.add(normalize_gaia_release(item))
                except ValueError:
                    pass
            found.update(_manifest_releases(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_manifest_releases(item))
    return found


def _read_manifest(source: Path) -> tuple[dict[str, Any] | None, list[str]]:
    path = source / "manifest.json"
    if not path.is_file():
        return None, []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"manifest.json is unreadable: {exc}"]
    if not isinstance(value, dict):
        return None, ["manifest.json is not a JSON object"]
    return value, []


def _member_path(
    source: Path,
    catalog: Path | str | None,
) -> Path | None:
    if catalog is not None:
        return Path(catalog).expanduser().resolve()
    for name in _MEMBER_NAMES:
        candidate = source / name
        if candidate.is_file():
            return candidate
    return None


def _read_member_table(path: Path | None):
    if path is None:
        return None
    import pandas as pd

    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    if path.suffix.lower() in {".csv", ".ecsv"}:
        columns = list(pd.read_csv(path, nrows=0).columns)
        id_names = {name for name in columns if str(name).lower() in {"source_id", "gaia", "gaia_id"}}
        dtype = {name: "string" for name in id_names}
        return pd.read_csv(path, dtype=dtype or None)
    return None


def _table_release_evidence(table) -> set[str]:
    if table is None:
        return set()
    found: set[str] = set()
    for column in table.columns:
        if str(column).lower() not in {"gaia_release", "release"}:
            continue
        for value in table[column].dropna().unique().tolist():
            try:
                found.add(normalize_gaia_release(str(value)))
            except ValueError:
                continue
    return found


def _source_column(table) -> str | None:
    if table is None:
        return None
    by_lower = {str(column).lower(): str(column) for column in table.columns}
    return (
        by_lower.get("source_id")
        or by_lower.get("gaia")
        or by_lower.get("gaia_id")
    )


def _effective_release(
    explicit: str | None,
    manifest_releases: set[str],
    table_releases: set[str],
) -> tuple[str | None, list[str]]:
    evidence = manifest_releases | table_releases
    errors: list[str] = []
    if explicit is not None:
        selected = normalize_gaia_release(explicit)
        conflicts = sorted(evidence.difference({selected}))
        if conflicts:
            errors.append(
                f"explicit release {selected} conflicts with corpus evidence: "
                + ", ".join(conflicts)
            )
        return selected, errors
    if len(evidence) == 1:
        return next(iter(evidence)), errors
    if len(evidence) > 1:
        errors.append("conflicting Gaia releases in legacy metadata: " + ", ".join(sorted(evidence)))
    return None, errors


def _import_analysis(
    source: Path | str,
    *,
    release: str | None = None,
    catalog: Path | str | None = None,
    label: str | None = None,
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source_path = Path(source).expanduser().resolve()
    inventory = inventory_corpus(source_path, catalog=catalog)
    manifest, warnings = _read_manifest(source_path)
    manifest_releases = _manifest_releases(manifest or {})
    member_path = _member_path(source_path, catalog)
    table = _read_member_table(member_path)
    table_releases = _table_release_evidence(table)
    effective_release, errors = _effective_release(
        release, manifest_releases, table_releases
    )
    source_column = _source_column(table)
    if table is not None and source_column is None:
        warnings.append(
            f"{member_path.name} has no source_id/Gaia/gaia_id column; inventory only"
        )
    if effective_release is None:
        warnings.append(
            "Gaia release is unknown; supply --release or explicit release metadata "
            "to index members"
        )
    effective_label = str(label or source_path.name)
    signature_contract = {
        "source_path": str(source_path),
        "label": effective_label,
        "release": effective_release,
        "catalog_path": str(member_path) if catalog is not None and member_path else None,
        "artifacts": [
            {
                "relative_path": record["relative_path"],
                "sha256": record.get("sha256"),
                "size_bytes": record.get("size_bytes"),
                "kind": record["kind"],
                "target": record.get("target"),
            }
            for record in inventory
        ],
        "options": json_value(dict(options or {})),
    }
    import_id = identity_hash(signature_contract, namespace="legacy-corpus-import")
    run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "astro-pipeline-import\0" + import_id))
    can_index = bool(table is not None and source_column and effective_release and not errors)
    return {
        "source_path": source_path,
        "label": effective_label,
        "inventory": inventory,
        "manifest": manifest,
        "manifest_releases": sorted(manifest_releases),
        "member_releases": sorted(table_releases),
        "member_path": member_path,
        "_member_table": table,
        "member_rows": 0 if table is None else int(len(table)),
        "source_column": source_column,
        "release": effective_release,
        "can_index_members": can_index,
        "warnings": warnings,
        "errors": errors,
        "signature_contract": signature_contract,
        "import_id": import_id,
        "run_id": run_id,
        "options": dict(options or {}),
    }


def plan_import(
    source: Path | str,
    *,
    release: str | None = None,
    catalog: Path | str | None = None,
    label: str | None = None,
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a hash-complete, read-only legacy import plan."""
    analysis = _import_analysis(
        source,
        release=release,
        catalog=catalog,
        label=label,
        options=options,
    )
    inventory = _public_inventory(analysis["inventory"])
    product_groups, product_skipped = _legacy_product_groups(
        analysis["source_path"]
    )
    eligible_products = []
    for group in product_groups:
        reason = None
        if analysis["release"] is None:
            reason = "corpus release is unknown; product metadata cannot supply it"
        elif group["contract"]["release"] != analysis["release"]:
            reason = "product release conflicts with corpus release"
        if reason is None:
            eligible_products.append(group)
        else:
            product_skipped.append(
                {
                    "path": group["source_directory"],
                    "identifier": group["identifier"],
                    "reason": reason,
                }
            )
    return {
        "mode": "dry_run",
        "writes_library": False,
        "modifies_source": False,
        "source_path": str(analysis["source_path"]),
        "label": analysis["label"],
        "release": analysis["release"],
        "release_evidence": {
            "manifest": analysis["manifest_releases"],
            "members": analysis["member_releases"],
        },
        "member_path": (
            str(analysis["member_path"]) if analysis["member_path"] else None
        ),
        "n_member_rows": analysis["member_rows"],
        "can_index_members": analysis["can_index_members"],
        "import_id": analysis["import_id"],
        "run_id": analysis["run_id"],
        "inventory": inventory,
        "n_inventory_files": sum(item["kind"] == "file" for item in inventory),
        "n_managed_copies": sum(bool(item.get("managed_copy")) for item in inventory),
        "copy_bytes": sum(
            int(item.get("size_bytes", 0))
            for item in inventory
            if item.get("managed_copy")
        ),
        "legacy_products": {
            "promotable": _public_product_groups(
                analysis["source_path"], eligible_products
            ),
            "inventory_only": product_skipped,
        },
        "warnings": analysis["warnings"],
        "errors": analysis["errors"],
        "next_step": "rerun with apply=True after reviewing this plan",
    }


def _member_records(analysis: Mapping[str, Any]) -> tuple[list[dict], list[dict], list[dict]]:
    table = analysis["_member_table"]
    if table is None:
        return [], [], []
    release = analysis["release"]
    source_column = analysis["source_column"]
    input_rows: list[dict[str, Any]] = []
    members: list[dict[str, Any]] = []
    matches: list[dict[str, Any]] = []
    release_columns = [
        str(column)
        for column in table.columns
        if str(column).lower() in {"gaia_release", "release"}
    ]
    for ordinal, (_, series) in enumerate(table.iterrows()):
        raw = {str(key): json_value(value) for key, value in series.to_dict().items()}
        input_row_id = f"legacy:{ordinal}"
        source_value = raw.get(source_column)
        row_release = release
        for column in release_columns:
            value = raw.get(column)
            if value not in (None, ""):
                row_release = str(value)
                break
        try:
            if source_value is None:
                raise ValueError("missing source identifier")
            from astro_pipeline.library.identity import source_identity

            normalized_release, normalized_id = source_identity(row_release, source_value)
        except (TypeError, ValueError):
            input_rows.append(
                {
                    "input_row_id": input_row_id,
                    "ordinal": ordinal,
                    "status": "invalid",
                    "raw": raw,
                }
            )
            continue
        input_rows.append(
            {
                "input_row_id": input_row_id,
                "ordinal": ordinal,
                "status": "matched",
                "raw": raw,
            }
        )
        member = {
            **raw,
            "source_id": normalized_id,
            "gaia_release": normalized_release,
            "member_ordinal": ordinal,
        }
        members.append(member)
        matches.append(
            {
                "member_ordinal": ordinal,
                "input_row_id": input_row_id,
                "match": {"method": "legacy_import", "verified": False},
            }
        )
    return members, input_rows, matches


def _complete_product_contract(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    contract = dict(value)
    if not _PRODUCT_CONTRACT_FIELDS.issubset(contract):
        return None
    try:
        contract["release"] = normalize_gaia_release(str(contract["release"]))
    except ValueError:
        return None
    normalized_text = {
        "archive_tag": str(contract["archive_tag"]).strip(),
        "retrieval_type": str(contract["retrieval_type"]).strip().upper(),
        "data_structure": str(contract["data_structure"]).strip().upper(),
        "output_format": str(contract["output_format"]).strip().lower(),
        "linking_parameter": str(contract["linking_parameter"]).strip().upper(),
    }
    if any(not value for value in normalized_text.values()):
        return None
    contract.update(normalized_text)
    profile = get_gaia_release_profile(contract["release"])
    if (
        contract["archive_tag"] != profile.archive_tag
        or contract["retrieval_type"] not in profile.datalink_products
        or contract["data_structure"] not in profile.datalink_structures
        or contract["output_format"] not in profile.datalink_formats
    ):
        return None
    if not isinstance(contract.get("valid_data"), bool):
        return None
    if str(contract["linking_parameter"]).strip().upper() not in {
        "SOURCE_ID",
        "TRANSIT_ID",
        "IMAGE_ID",
    }:
        return None
    return contract


def _legacy_artifact_path(
    source: Path, product_directory: Path, value: Any
) -> Path | None:
    if value in (None, ""):
        return None
    raw = Path(str(value)).expanduser()
    candidates = [raw] if raw.is_absolute() else [product_directory / raw, source / raw]
    if raw.is_absolute():
        candidates.extend(
            [
                product_directory / raw.name,
                product_directory / "archive_tables" / raw.name,
            ]
        )
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved.is_file() and resolved.is_relative_to(source):
            return resolved
    return None


def _artifact_matches_contract(
    record: Mapping[str, Any], contract: Mapping[str, Any]
) -> bool:
    try:
        release = normalize_gaia_release(str(record["release"]))
    except (KeyError, ValueError):
        return False
    actual = {
        "release": release,
        "archive_tag": str(record.get("archive_tag", "")).strip(),
        "retrieval_type": str(record.get("retrieval_type", "")).strip().upper(),
        "data_structure": str(record.get("data_structure", "")).strip().upper(),
        "output_format": str(record.get("output_format", "")).strip().lower(),
        "linking_parameter": str(record.get("linking_parameter", ""))
        .strip()
        .upper(),
    }
    return all(actual[field] == contract[field] for field in actual)


def _verified_legacy_checksum(path: Path, expected: str) -> bool:
    try:
        return bool(re.fullmatch(r"[0-9a-f]{64}", expected)) and sha256_file(
            path
        ) == expected
    except OSError:
        return False


def _legacy_table_payload(
    path: Path, output_format: str
) -> tuple[list[Any], list[Mapping[str, Any]]]:
    from astropy.table import Table

    formats = {
        "fits": "fits",
        "votable": "votable",
        "votable_plain": "votable",
        "csv": "ascii.csv",
        "ecsv": "ascii.ecsv",
    }
    table_format = formats.get(output_format)
    if table_format is None:
        raise ValueError(f"unsupported table format: {output_format}")
    tables: list[Any] = []
    metadata: list[Mapping[str, Any]] = []
    if output_format == "fits":
        from astropy.io import fits

        with fits.open(path, mode="readonly", memmap=False) as hdus:
            for index, hdu in enumerate(hdus):
                metadata.append(dict(hdu.header))
                if isinstance(hdu, (fits.BinTableHDU, fits.TableHDU)):
                    table = Table.read(path, format="fits", hdu=index)
                    tables.append(table)
                    metadata.append(dict(table.meta))
    else:
        table = Table.read(path, format=table_format)
        tables.append(table)
        metadata.append(dict(table.meta))
    if not tables:
        raise ValueError("artifact has no readable table")
    return tables, metadata


def _key_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _value_items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        return [value]
    if isinstance(value, Mapping):
        return list(value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    if hasattr(value, "tolist"):
        converted = value.tolist()
        return converted if isinstance(converted, list) else [converted]
    return [value]


def _identifier_text(value: Any) -> str | None:
    if type(value).__name__ in {"MaskedConstant", "NAType", "NaTType"}:
        return None
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="strict")
    text = str(value).strip()
    if not text or text in {"--", "nan", "None"}:
        return None
    return normalize_identifier(value)


def _legacy_table_identity_error(
    path: Path,
    contract: Mapping[str, Any],
    identifier: str,
    *,
    role: str,
) -> str | None:
    try:
        expected_identifier = normalize_identifier(identifier)
        tables, metadata = _legacy_table_payload(
            path, str(contract["output_format"])
        )
    except Exception as exc:
        return f"{role} artifact is unsupported or unparseable: {exc}"

    linking_parameter = str(contract["linking_parameter"])
    identifier_keys = {
        "SOURCE_ID": {"sourceid"},
        "TRANSIT_ID": {"transitid"},
        "IMAGE_ID": {"imageid", "sifobservationid"},
    }[linking_parameter]
    identifiers: set[str] = set()
    try:
        for table in tables:
            for column in table.colnames:
                if _key_token(column) not in identifier_keys:
                    continue
                for raw in table[column]:
                    normalized = _identifier_text(raw)
                    if normalized is not None:
                        identifiers.add(normalized)
        for item in metadata:
            for key, raw in item.items():
                if _key_token(key) not in identifier_keys:
                    continue
                for value in _value_items(raw):
                    normalized = _identifier_text(value)
                    if normalized is not None:
                        identifiers.add(normalized)
    except (TypeError, ValueError, UnicodeError) as exc:
        return f"{role} artifact has invalid {linking_parameter} metadata: {exc}"

    if not identifiers:
        return f"{role} artifact lacks {linking_parameter} content proof"
    if role == "normalized" and identifiers != {expected_identifier}:
        return (
            f"normalized artifact identifies {sorted(identifiers)}, "
            f"not only {expected_identifier}"
        )
    if role == "archive" and expected_identifier not in identifiers:
        return (
            f"archive artifact does not contain {linking_parameter} "
            f"{expected_identifier}"
        )

    release_keys = {
        "release",
        "datarelease",
        "gaiarelease",
        "gaiadatarelease",
        "gaiadr",
        "archivetag",
    }
    for item in metadata:
        for key, raw in item.items():
            if _key_token(key) not in release_keys:
                continue
            for value in _value_items(raw):
                if value is None or not str(value).strip():
                    continue
                try:
                    content_release = normalize_gaia_release(str(value))
                except ValueError:
                    return (
                        f"{role} artifact has unsupported explicit Gaia release "
                        f"metadata: {value!r}"
                    )
                if content_release != contract["release"]:
                    return (
                        f"{role} artifact release {content_release} conflicts with "
                        f"contract release {contract['release']}"
                    )
    return None


def _legacy_product_groups(
    source: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    groups: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for request_path in sorted(source.rglob("product_request.json")):
        directory = request_path.parent
        artifacts_path = directory / "artifacts.json"
        try:
            if not request_path.resolve(strict=True).is_relative_to(source):
                raise ValueError("product request escapes the legacy corpus")
            if not artifacts_path.resolve(strict=True).is_relative_to(source):
                raise ValueError("artifacts manifest escapes the legacy corpus")
            request_value = json.loads(request_path.read_text(encoding="utf-8"))
            artifact_value = json.loads(artifacts_path.read_text(encoding="utf-8"))
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            skipped.append({"path": str(directory), "reason": str(exc)})
            continue
        contract = _complete_product_contract(
            request_value.get("contract") if isinstance(request_value, Mapping) else None
        )
        records = (
            artifact_value.get("artifacts")
            if isinstance(artifact_value, Mapping)
            else None
        )
        if contract is None:
            skipped.append(
                {"path": str(directory), "reason": "incomplete product contract"}
            )
            continue
        if not isinstance(records, list):
            skipped.append(
                {"path": str(directory), "reason": "invalid artifacts manifest"}
            )
            continue
        by_identifier: dict[str, list[dict[str, Any]]] = {}
        invalid_identifiers: set[str] = set()
        for record_value in records:
            if not isinstance(record_value, Mapping) or record_value.get("success") is not True:
                continue
            record = dict(record_value)
            identifier = str(record.get("identifier", "")).strip()
            if not identifier or not _artifact_matches_contract(record, contract):
                invalid_identifiers.add(identifier)
                skipped.append(
                    {
                        "path": str(directory),
                        "identifier": identifier,
                        "reason": "artifact identity conflicts with product contract",
                    }
                )
                continue
            normalized = _legacy_artifact_path(
                source, directory, record.get("normalized_path")
            )
            checksum = str(record.get("checksum_sha256", "")).lower()
            if normalized is None or not _verified_legacy_checksum(normalized, checksum):
                invalid_identifiers.add(identifier)
                skipped.append(
                    {
                        "path": str(directory),
                        "identifier": identifier,
                        "reason": "unverifiable normalized artifact",
                    }
                )
                continue
            identity_error = _legacy_table_identity_error(
                normalized, contract, identifier, role="normalized"
            )
            if identity_error is not None:
                invalid_identifiers.add(identifier)
                skipped.append(
                    {
                        "path": str(normalized),
                        "identifier": identifier,
                        "reason": identity_error,
                    }
                )
                continue
            metadata_value = record.get("metadata")
            artifacts = [
                {
                    "role": "normalized",
                    "slot": str(record.get("table_key") or normalized.name),
                    "path": normalized,
                    "sha256": checksum,
                    "content_type": str(
                        record.get("content_type") or "application/octet-stream"
                    ),
                    "metadata": (
                        dict(metadata_value)
                        if isinstance(metadata_value, Mapping)
                        else {}
                    ),
                }
            ]
            archive_value = record.get("archive_table_path")
            if archive_value not in (None, ""):
                archive = _legacy_artifact_path(source, directory, archive_value)
                archive_checksum = str(
                    record.get("archive_checksum_sha256", "")
                ).lower()
                if archive is None or not _verified_legacy_checksum(
                    archive, archive_checksum
                ):
                    invalid_identifiers.add(identifier)
                    skipped.append(
                        {
                            "path": str(directory),
                            "identifier": identifier,
                            "reason": "unverifiable archive artifact",
                        }
                    )
                    continue
                identity_error = _legacy_table_identity_error(
                    archive, contract, identifier, role="archive"
                )
                if identity_error is not None:
                    invalid_identifiers.add(identifier)
                    skipped.append(
                        {
                            "path": str(archive),
                            "identifier": identifier,
                            "reason": identity_error,
                        }
                    )
                    continue
                artifacts.append(
                    {
                        "role": "archive",
                        "slot": str(record.get("table_key") or archive.name),
                        "path": archive,
                        "sha256": archive_checksum,
                        "content_type": str(
                            record.get("content_type") or "application/octet-stream"
                        ),
                        "metadata": {"preservation": "legacy_verified_copy"},
                    }
                )
            by_identifier.setdefault(identifier, []).extend(artifacts)
        for identifier, artifacts in by_identifier.items():
            if identifier in invalid_identifiers:
                continue
            occupied: set[tuple[str, str]] = set()
            for index, artifact in enumerate(artifacts):
                base = _safe_component(artifact["slot"] or index)
                slot = base
                suffix = 2
                while (artifact["role"], slot) in occupied:
                    slot = f"{base}-{suffix}"
                    suffix += 1
                artifact["slot"] = slot
                occupied.add((artifact["role"], slot))
            groups.append(
                {
                    "contract": contract,
                    "identifier": identifier,
                    "artifacts": artifacts,
                    "source_directory": str(directory),
                }
            )
    return groups, skipped


def _register_legacy_products(
    store: LibraryStore,
    groups: Sequence[Mapping[str, Any]],
    skipped: Sequence[Mapping[str, Any]],
    *,
    release: str | None,
    run_id: str,
) -> dict[str, Any]:
    result = {
        "registered": [],
        "reused": [],
        "skipped": [dict(item) for item in skipped],
    }
    for group_value in groups:
        group = dict(group_value)
        contract = dict(group["contract"])
        identifier = str(group["identifier"])
        if contract["release"] != release:
            result["skipped"].append(
                {
                    "path": group["source_directory"],
                    "identifier": identifier,
                    "reason": "product release conflicts with corpus release",
                }
            )
            continue
        changed = next(
            (
                artifact
                for artifact in group["artifacts"]
                if sha256_file(artifact["path"]) != artifact["sha256"]
            ),
            None,
        )
        if changed is not None:
            result["skipped"].append(
                {
                    "path": str(changed["path"]),
                    "identifier": identifier,
                    "reason": "product artifact checksum mismatch",
                }
            )
            continue
        try:
            item_key = product_item_key(contract, identifier)
        except ValueError as exc:
            result["skipped"].append(
                {
                    "path": group["source_directory"],
                    "identifier": identifier,
                    "reason": f"invalid product identifier: {exc}",
                }
            )
            continue
        expected = {
            (artifact["role"], artifact["slot"], artifact["sha256"])
            for artifact in group["artifacts"]
        }
        try:
            with store.hold_lease(
                "product", item_key, ttl=store.lease_seconds, wait_timeout=0
            ) as lease:
                existing = store.get_product(item_key, verify=False)
                if existing is not None:
                    actual = {
                        (
                            artifact["role"],
                            artifact["slot"],
                            artifact["blob"]["sha256"],
                        )
                        for artifact in existing["artifacts"]
                    }
                    if actual == expected:
                        try:
                            store.get_product(item_key, verify=True)
                        except BlobIntegrityError as exc:
                            result["skipped"].append(
                                {
                                    "identifier": identifier,
                                    "reason": f"matching active product is corrupt: {exc}",
                                }
                            )
                        else:
                            result["reused"].append(
                                {
                                    "item_key": item_key,
                                    "version_id": existing["version_id"],
                                    "identifier": identifier,
                                }
                            )
                    else:
                        result["skipped"].append(
                            {
                                "identifier": identifier,
                                "reason": "different active product already exists",
                            }
                        )
                    continue
                published = []
                for artifact in group["artifacts"]:
                    blob = store.blobs.publish(
                        artifact["path"],
                        extension=artifact["path"].suffix.lstrip(".") or "bin",
                        content_type=artifact["content_type"],
                    )
                    if blob.sha256 != artifact["sha256"]:
                        raise RuntimeError(
                            f"legacy product changed during import: {artifact['path']}"
                        )
                    published.append(
                        {
                            "role": artifact["role"],
                            "slot": artifact["slot"],
                            "blob": blob,
                            "metadata": artifact["metadata"],
                        }
                    )
                saved = store.save_product(
                    contract,
                    identifier,
                    published,
                    lease,
                    metadata={
                        "legacy_import": True,
                        "legacy_unverified_provenance": True,
                        "run_id": run_id,
                        "source_directory": group["source_directory"],
                    },
                )
        except LibraryBusyError:
            result["skipped"].append(
                {"identifier": identifier, "reason": "product item is busy"}
            )
            continue
        result["registered"].append(
            {
                "item_key": saved["item_key"],
                "version_id": saved["version_id"],
                "identifier": saved["identifier"],
            }
        )
    return result


def import_corpus(
    store: LibraryStore,
    source: Path | str,
    *,
    apply: bool = False,
    release: str | None = None,
    catalog: Path | str | None = None,
    label: str | None = None,
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Inventory or explicitly import a legacy corpus without mutating it."""
    if not apply:
        return plan_import(
            source,
            release=release,
            catalog=catalog,
            label=label,
            options=options,
        )
    analysis = _import_analysis(
        source,
        release=release,
        catalog=catalog,
        label=label,
        options=options,
    )
    if analysis["errors"]:
        raise ValueError("; ".join(analysis["errors"]))
    source_path: Path = analysis["source_path"]
    if store.root == source_path or store.root.is_relative_to(source_path):
        raise ValueError("library root must not be inside the imported corpus")
    product_groups, product_skipped = _legacy_product_groups(source_path)
    with store.hold_lease(
        "legacy_import",
        analysis["import_id"],
        ttl=store.lease_seconds,
        wait_timeout=store.wait_timeout_seconds,
    ) as lease:
        existing = store.read(
            "SELECT run_id, status, metadata_json FROM runs WHERE run_id = ?",
            (analysis["run_id"],),
        )
        if existing:
            return {
                **plan_import(
                    source,
                    release=release,
                    catalog=catalog,
                    label=label,
                    options=options,
                ),
                "mode": "apply",
                "writes_library": False,
                "idempotent": True,
                "status": existing[0]["status"],
            }
        existing_corpus = store.read(
            "SELECT provenance_status FROM corpora WHERE canonical_path = ?",
            (str(source_path),),
        )
        provenance_status = (
            existing_corpus[0]["provenance_status"]
            if existing_corpus
            else "legacy_unverified"
        )
        corpus_id = store.register_corpus(
            source_path,
            analysis["label"],
            provenance_status=provenance_status,
        )
        run_id = store.start_run(
            corpus_id,
            request_id=None,
            options={
                "legacy_import": True,
                "import_id": analysis["import_id"],
                "source_path": str(source_path),
                "release": analysis["release"],
                **analysis["options"],
            },
            run_id=analysis["run_id"],
        )
        managed: list[dict[str, Any]] = []
        try:
            for record in analysis["inventory"]:
                if not record.get("managed_copy") or record["kind"] != "file":
                    continue
                source_file: Path = record["_source_path"]
                content_type = mimetypes.guess_type(source_file.name)[0] or "application/octet-stream"
                blob = store.blobs.publish(source_file, content_type=content_type)
                if blob.sha256 != record["sha256"]:
                    raise RuntimeError(
                        f"legacy source changed during import: {record['relative_path']}"
                    )
                store.register_blob(blob)
                store.record_run_artifact(
                    run_id,
                    f"legacy:{record['category']}:{record['relative_path']}",
                    blob.relative_path,
                    blob.sha256,
                )
                managed.append(
                    {
                        "source_relative_path": record["relative_path"],
                        "category": record["category"],
                        "blob": blob.to_dict(),
                    }
                )
            counts = {
                "n_input_rows": 0,
                "n_sample_members": 0,
                "n_unique_sources": 0,
            }
            if analysis["can_index_members"]:
                members, input_rows, matches = _member_records(analysis)
                counts = store.index_members(
                    run_id,
                    members,
                    input_rows=input_rows,
                    matches=matches,
                    verified=False,
                    lease=lease,
                )
            products = _register_legacy_products(
                store,
                product_groups,
                product_skipped,
                release=analysis["release"],
                run_id=run_id,
            )
            metadata = {
                "legacy_unverified": True,
                "import_id": analysis["import_id"],
                "members_indexed": bool(analysis["can_index_members"]),
                "published": bool(analysis["can_index_members"]),
                "release": analysis["release"],
                "managed_artifacts": managed,
                "inventory": _public_inventory(analysis["inventory"]),
                "paper_evidence_only": any(
                    record.get("category") == "paper_evidence"
                    for record in analysis["inventory"]
                ),
                "legacy_products": products,
                **counts,
            }
            store.finish_run(run_id, "complete", metadata=metadata)
        except BaseException as exc:
            store.finish_run(
                run_id,
                "failed",
                error=exc,
                metadata={
                    "legacy_unverified": True,
                    "members_indexed": False,
                    "published": False,
                    "import_id": analysis["import_id"],
                },
            )
            raise
    return {
        "mode": "apply",
        "writes_library": True,
        "modifies_source": False,
        "idempotent": False,
        "source_path": str(source_path),
        "corpus_id": corpus_id,
        "run_id": run_id,
        "import_id": analysis["import_id"],
        "release": analysis["release"],
        "members_indexed": bool(analysis["can_index_members"]),
        "n_managed_copies": len(managed),
        "legacy_products": products,
        **counts,
        "warnings": analysis["warnings"],
    }


def _library_disk_residue(
    store: LibraryStore, registered_paths: set[str]
) -> tuple[list[str], list[str]]:
    orphan_objects: list[str] = []
    if store.blobs.objects.is_dir():
        for path in sorted(store.blobs.objects.rglob("*")):
            if path.is_file():
                relative = path.relative_to(store.root).as_posix()
                if relative not in registered_paths:
                    orphan_objects.append(relative)
    staging_files = (
        [
            path.relative_to(store.root).as_posix()
            for path in sorted(store.blobs.staging.rglob("*"))
            if path.is_file()
        ]
        if store.blobs.staging.is_dir()
        else []
    )
    return orphan_objects, staging_files


def _pending_publications(store: LibraryStore) -> list[dict[str, Any]]:
    """Return prepared runs whose compatibility-view publication is unfinished."""
    rows = store.read(
        "SELECT r.run_id, r.corpus_id, r.status, r.started_at, r.finished_at, "
        "r.error, r.metadata_json, c.label, c.canonical_path "
        "FROM runs AS r JOIN corpora AS c ON c.corpus_id = r.corpus_id "
        "ORDER BY r.started_at, r.run_id"
    )
    pending: list[dict[str, Any]] = []
    for row in rows:
        try:
            value = json.loads(row["metadata_json"])
        except (TypeError, json.JSONDecodeError):
            value = {}
        metadata = value if isinstance(value, dict) else {}
        published = metadata.get("published") is True
        view_pending = metadata.get("view_pending") is True
        prepared_unpublished = metadata.get("prepared") is True and not published
        publication_failed = (
            metadata.get("publication_error") is not None and not published
        )
        if not (view_pending or prepared_unpublished or publication_failed):
            continue
        pending.append(
            {
                "run_id": row["run_id"],
                "corpus_id": row["corpus_id"],
                "label": row["label"],
                "corpus_path": row["canonical_path"],
                "status": row["status"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "run_error": row["error"],
                "members_indexed": metadata.get("members_indexed"),
                "prepared": metadata.get("prepared"),
                "published": metadata.get("published"),
                "view_pending": metadata.get("view_pending"),
                "snapshot_path": metadata.get("snapshot_path"),
                "publication_error": metadata.get("publication_error"),
                "recovery": "diagnostic_only",
            }
        )
    return pending


def verify_library(store: LibraryStore) -> dict[str, Any]:
    """Verify SQLite and managed bytes without changing either."""
    if not store.exists:
        orphan_objects, staging_files = _library_disk_residue(store, set())
        return {
            "exists": False,
            "scope": "global_library",
            "ok": True,
            "clean": not orphan_objects and not staging_files,
            "integrity": [],
            "foreign_key_violations": [],
            "missing_blobs": [],
            "corrupt_blobs": [],
            "orphan_objects": orphan_objects,
            "staging_files": staging_files,
            "pending_publications": [],
            "pending_publication_recovery": "diagnostic_only",
        }
    integrity_rows = store.read("PRAGMA integrity_check")
    integrity = [str(next(iter(row.values()))) for row in integrity_rows]
    foreign_keys = store.read("PRAGMA foreign_key_check")
    blob_rows = store.read(
        "SELECT sha256, relative_path, size_bytes, content_type, extension FROM blobs "
        "ORDER BY sha256"
    )
    missing: list[dict[str, Any]] = []
    corrupt: list[dict[str, Any]] = []
    registered_paths: set[str] = set()
    for row in blob_rows:
        registered_paths.add(str(row["relative_path"]))
        try:
            store.blobs.verify(row)
        except (BlobIntegrityError, OSError) as exc:
            path = store.root / row["relative_path"]
            target = missing if not path.is_file() else corrupt
            target.append({"sha256": row["sha256"], "path": str(path), "error": str(exc)})
    orphan_objects, staging_files = _library_disk_residue(
        store, registered_paths
    )
    pending_publications = _pending_publications(store)
    ok = (
        integrity == ["ok"]
        and not foreign_keys
        and not missing
        and not corrupt
        and not pending_publications
    )
    return {
        "exists": True,
        "scope": "global_library",
        "ok": ok,
        "clean": ok and not orphan_objects and not staging_files,
        "status": store.status(),
        "integrity": integrity,
        "foreign_key_violations": foreign_keys,
        "n_registered_blobs": len(blob_rows),
        "missing_blobs": missing,
        "corrupt_blobs": corrupt,
        "orphan_objects": orphan_objects,
        "staging_files": staging_files,
        "pending_publications": pending_publications,
        "pending_publication_recovery": "diagnostic_only",
    }


def _artifact_source(store: LibraryStore, corpus_path: Path, path_value: str) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    library_candidate = store.root / path
    if library_candidate.exists():
        return library_candidate
    return corpus_path / path


def reconcile_library(
    store: LibraryStore,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Regenerate missing published views; report pending publication without repair."""
    if not store.exists:
        return {
            "exists": False,
            "apply": apply,
            "actions": [],
            "skipped": [],
            "pending_publications": [],
            "pending_publication_recovery": "diagnostic_only",
        }
    actions: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    pending_publications = _pending_publications(store)
    pending_by_corpus: dict[str, list[str]] = {}
    for pending in pending_publications:
        pending_by_corpus.setdefault(pending["corpus_id"], []).append(
            pending["run_id"]
        )
    for run in Library(store)._current_runs():
        if run["corpus_id"] in pending_by_corpus:
            skipped.append(
                {
                    "run_id": run["run_id"],
                    "corpus_id": run["corpus_id"],
                    "pending_run_ids": pending_by_corpus[run["corpus_id"]],
                    "reason": "corpus_has_pending_publication",
                }
            )
            continue
        if (
            run.get("provenance_status") != "verified"
            or run.get("metadata", {}).get("legacy_unverified") is True
        ):
            skipped.append(
                {
                    "run_id": run["run_id"],
                    "reason": "unverified_run_provenance",
                }
            )
            continue
        corpus_path = Path(run["path"])
        artifacts = store.read(
            "SELECT kind, path, sha256 FROM run_artifacts WHERE run_id = ?",
            (run["run_id"],),
        )
        for artifact in artifacts:
            source = _artifact_source(store, corpus_path, artifact["path"])
            basename = source.name
            if basename not in _COMPATIBILITY_VIEWS:
                continue
            destination = corpus_path / basename
            run_root = corpus_path / "runs" / run["run_id"]
            try:
                run_relative = source.relative_to(run_root)
            except ValueError:
                pass
            else:
                if run_relative.parts[0] != "inputs":
                    destination = corpus_path / run_relative
            if destination.exists():
                if destination.is_file() and sha256_file(destination) == artifact["sha256"]:
                    continue
                skipped.append(
                    {
                        "run_id": run["run_id"],
                        "destination": str(destination),
                        "reason": "existing_divergent_view",
                    }
                )
                continue
            if not source.is_file() or sha256_file(source) != artifact["sha256"]:
                skipped.append(
                    {
                        "run_id": run["run_id"],
                        "source": str(source),
                        "reason": "unverified_run_artifact",
                    }
                )
                continue
            action = {
                "run_id": run["run_id"],
                "source": str(source),
                "destination": str(destination),
                "sha256": artifact["sha256"],
            }
            actions.append(action)
            if apply:
                atomic_copy(source, destination)
    return {
        "exists": True,
        "apply": apply,
        "writes": len(actions) if apply else 0,
        "actions": actions,
        "skipped": skipped,
        "pending_publications": pending_publications,
        "pending_publication_recovery": "diagnostic_only",
    }


def _overlaps(first: Path, second: Path) -> bool:
    return (
        first == second
        or first.is_relative_to(second)
        or second.is_relative_to(first)
    )


def _resolve_corpus(store: LibraryStore, source: Path | str) -> dict[str, Any]:
    raw = str(source)
    path = Path(raw).expanduser()
    if path.is_dir():
        canonical = str(path.resolve())
        rows = store.read(
            "SELECT * FROM corpora WHERE canonical_path = ?", (canonical,)
        )
        if rows:
            return rows[0]
        return {
            "corpus_id": None,
            "canonical_path": canonical,
            "label": path.name,
            "provenance_status": "unregistered",
        }
    rows = store.read(
        "SELECT * FROM corpora WHERE corpus_id = ? OR label = ? "
        "ORDER BY canonical_path",
        (raw, raw),
    )
    if not rows:
        raise FileNotFoundError(f"unknown corpus path, id, or label: {source}")
    ids = {row["corpus_id"] for row in rows}
    if len(ids) > 1 and all(row["corpus_id"] != raw for row in rows):
        raise ValueError(f"corpus label is ambiguous: {raw}")
    return next((row for row in rows if row["corpus_id"] == raw), rows[0])


def _selected_run(
    store: LibraryStore, corpus_id: str | None, run_id: str | None
) -> dict[str, Any] | None:
    if corpus_id is None:
        if run_id is not None:
            raise ValueError("--run-id requires a registered corpus")
        return None
    if run_id is not None:
        rows = store.read(
            "SELECT * FROM runs WHERE run_id = ? AND corpus_id = ?",
            (run_id, corpus_id),
        )
        if not rows:
            raise ValueError(f"run {run_id!r} does not belong to the corpus")
        return rows[0]
    current = [
        run
        for run in Library(store)._current_runs()
        if run["corpus_id"] == corpus_id
    ]
    return current[0] if current else None


def _copy_corpus_tree(source: Path, destination: Path, run_id: str | None) -> None:
    if run_id is None:
        shutil.copytree(source, destination, copy_function=shutil.copy2, symlinks=True)
        return
    runs_root = source / "runs"
    run_source = runs_root / run_id
    if not run_source.is_dir():
        raise FileNotFoundError(f"managed run snapshot is missing: {run_source}")

    managed_root_names = set(_COMPATIBILITY_VIEWS)
    if runs_root.is_dir():
        for candidate_run in runs_root.iterdir():
            if candidate_run.is_dir():
                managed_root_names.update(child.name for child in candidate_run.iterdir())

    destination.mkdir(parents=True)
    for child in source.iterdir():
        if child.name == "runs" or child.name in managed_root_names:
            continue
        target = destination / child.name
        if child.is_dir() and not child.is_symlink():
            shutil.copytree(child, target, copy_function=shutil.copy2, symlinks=True)
        elif child.is_symlink():
            target.symlink_to(os.readlink(child))
        else:
            shutil.copy2(child, target)

    run_destination = destination / "runs" / run_id
    shutil.copytree(
        run_source, run_destination, copy_function=shutil.copy2, symlinks=True
    )
    for snapshot_path in sorted(run_source.rglob("*")):
        if not snapshot_path.is_file() or snapshot_path.name.startswith("."):
            continue
        relative = snapshot_path.relative_to(run_source)
        if relative.parts[0] == "inputs" or snapshot_path.suffix == ".tmp":
            continue
        if snapshot_path.suffix.lower() not in {
            ".csv",
            ".parquet",
            ".json",
            ".html",
            ".png",
        }:
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snapshot_path, target)


def _registered_blob_targets(
    store: LibraryStore,
) -> dict[Path, dict[str, Any]]:
    if not store.exists:
        return {}
    objects_root = store.blobs.objects.resolve()
    targets: dict[Path, dict[str, Any]] = {}
    for row in store.read(
        "SELECT sha256, relative_path, size_bytes, content_type, extension FROM blobs"
    ):
        relative = Path(str(row["relative_path"]))
        if relative.is_absolute() or ".." in relative.parts:
            continue
        candidate = (store.root / relative).resolve(strict=False)
        if candidate.is_relative_to(objects_root):
            targets[candidate] = dict(row)
    return targets


def _materialize_standalone_symlinks(
    root: Path,
    *,
    source_root: Path,
    store: LibraryStore,
) -> None:
    """Copy only selected-tree links or verified registered library blobs."""
    root = root.resolve()
    source_root = source_root.resolve()
    registered_blobs = _registered_blob_targets(store)
    for path in sorted(root.rglob("*")):
        if not path.is_symlink():
            continue
        raw_target = Path(os.readlink(path))
        candidate = raw_target if raw_target.is_absolute() else path.parent / raw_target
        try:
            target = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"unsafe standalone export symlink: {path}") from exc

        copy_source: Path | None = None
        if target.is_relative_to(root):
            if target.is_file():
                copy_source = target
        elif target.is_relative_to(source_root):
            selected_path = root / target.relative_to(source_root)
            try:
                selected_target = selected_path.resolve(strict=False)
            except (OSError, RuntimeError):
                selected_target = selected_path
            if selected_target.is_relative_to(root) and selected_target.is_file():
                copy_source = selected_target
        else:
            blob = registered_blobs.get(target)
            if blob is not None:
                copy_source = store.blobs.verify(blob)

        if copy_source is None:
            raise ValueError(
                f"standalone export refuses external or directory symlink: {path}"
            )
        path.unlink()
        atomic_copy(copy_source, path)


def _safe_component(value: Any) -> str:
    cleaned = _SAFE_COMPONENT.sub("_", str(value)).strip("._")
    return cleaned or "item"


def _rewrite_export_manifests(
    root: Path,
    store: LibraryStore,
    *,
    selected_run_id: str | None,
    standalone: bool,
) -> list[str]:
    rewritten: list[str] = []
    selected_snapshot = (
        root / "runs" / selected_run_id / "manifest.json"
        if selected_run_id is not None
        else None
    )
    for path in sorted(root.rglob("manifest.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot rewrite export manifest {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"export manifest is not a JSON object: {path}")
        manifest = dict(value)
        if standalone:
            manifest["storage"] = {"kind": "standalone"}
        else:
            storage_value = manifest.get("storage")
            storage = dict(storage_value) if isinstance(storage_value, Mapping) else {}
            storage.pop("root", None)
            storage.update(
                {
                    "kind": "shared_library",
                    "library_root": str(store.root),
                    "relative_library_root": os.path.relpath(store.root, path.parent),
                    "index_path": "index.sqlite3",
                }
            )
            manifest["storage"] = storage
        if selected_run_id is not None and (
            path == root / "manifest.json" or path == selected_snapshot
        ):
            manifest["snapshot_run_id"] = selected_run_id
        elif selected_run_id is None and path == root / "manifest.json":
            manifest.pop("active_run_id", None)
            manifest.pop("current_run_id", None)
            manifest.pop("snapshot_run_id", None)
        atomic_json(path, manifest)
        rewritten.append(path.relative_to(root).as_posix())
    return rewritten


def _reference_documents(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*.json")
        if "product_refs" in path.name and path.is_file()
    )


def _copy_standalone_artifact(
    store: LibraryStore,
    export_root: Path,
    document_path: Path,
    contract: Mapping[str, Any],
    item: Mapping[str, Any],
    artifact: Mapping[str, Any],
) -> dict[str, Any]:
    blob = dict(artifact.get("blob") or {})
    source = store.blobs.verify(blob)
    identifier = str(item.get("identifier", ""))
    retrieval = str(contract.get("retrieval_type", "PRODUCT")).upper()
    extension = str(blob.get("extension") or source.suffix.lstrip(".") or "bin")
    role = str(artifact.get("role", "normalized"))
    slot = _safe_component(artifact.get("slot", role))
    safe_identifier = _safe_component(identifier)
    safe_extension = _safe_component(extension)
    if role == "normalized" and retrieval in {"XP_SAMPLED", "XP_CONTINUOUS"}:
        preferred = (
            Path(f"gaia_{retrieval.lower()}")
            / f"{safe_identifier}.{safe_extension}"
        )
    elif role == "archive":
        preferred = Path("archive_tables") / f"{blob['sha256']}.{safe_extension}"
    else:
        preferred = (
            Path("gaia_products")
            / _safe_component(retrieval.lower())
            / safe_identifier
            / f"{_safe_component(role)}-{slot}.{safe_extension}"
        )
    document_root = document_path.parent
    try:
        relative = (export_root / preferred).relative_to(document_root)
    except ValueError:
        relative = Path("standalone_artifacts") / f"{blob['sha256']}.{safe_extension}"
    destination = document_root / relative
    if destination.exists():
        if sha256_file(destination) != blob["sha256"]:
            raise BlobIntegrityError(f"standalone export collision: {destination}")
    else:
        atomic_copy(source, destination)
    local_blob = {
        **blob,
        "relative_path": relative.as_posix(),
        "size_bytes": destination.stat().st_size,
    }
    return {
        **dict(artifact),
        "blob": local_blob,
        "verified_path": relative.as_posix(),
        "storage_kind": "standalone",
    }


def _rewrite_product_document(
    document: Mapping[str, Any],
    *,
    document_path: Path,
    export_root: Path,
    store: LibraryStore,
    standalone: bool,
) -> dict[str, Any]:
    if document.get("kind") == "corpus_product_refs" or "products" in document:
        return {
            **dict(document),
            "products": [
                _rewrite_product_document(
                    product,
                    document_path=document_path,
                    export_root=export_root,
                    store=store,
                    standalone=standalone,
                )
                for product in document.get("products", [])
                if isinstance(product, Mapping)
            ],
        }
    contract = dict(document.get("contract") or {})
    ready_items: list[dict[str, Any]] = []
    for item_value in document.get("items", []):
        if not isinstance(item_value, Mapping):
            continue
        item = dict(item_value)
        if item.get("status") != "ready" or not item.get("version_id"):
            continue
        artifacts: list[dict[str, Any]] = []
        for artifact_value in item.get("artifacts", []):
            if not isinstance(artifact_value, Mapping):
                continue
            artifact = dict(artifact_value)
            if standalone:
                artifact = _copy_standalone_artifact(
                    store,
                    export_root,
                    document_path,
                    contract,
                    item,
                    artifact,
                )
            else:
                blob = dict(artifact.get("blob") or {})
                verified = store.blobs.verify(blob)
                artifact["verified_path"] = str(verified)
                artifact["storage_kind"] = "shared_library"
            artifacts.append(artifact)
        item["artifacts"] = artifacts
        ready_items.append(item)
    if standalone:
        storage = {"kind": "standalone", "root": "."}
    else:
        storage = {
            "kind": "shared_library",
            "library_root": str(store.root),
            "relative_library_root": os.path.relpath(store.root, document_path.parent),
            "index_path": "index.sqlite3",
        }
    return {
        **dict(document),
        "schema_version": str(document.get("schema_version") or "2.0"),
        "storage": storage,
        "contract": contract,
        "items": ready_items,
    }


def export_corpus(
    store: LibraryStore,
    source: Path | str,
    destination: Path | str,
    *,
    run_id: str | None = None,
    standalone: bool = False,
) -> dict[str, Any]:
    """Export a corpus to a new independent directory."""
    corpus = _resolve_corpus(store, source)
    source_path = Path(corpus["canonical_path"]).resolve()
    destination = Path(destination).expanduser().resolve()
    if not source_path.is_dir():
        raise FileNotFoundError(f"corpus directory is missing: {source_path}")
    if destination.exists():
        raise FileExistsError(destination)
    if _overlaps(source_path, destination):
        raise ValueError("source and destination directories must not overlap")
    selected_run = _selected_run(store, corpus.get("corpus_id"), run_id)
    selected_run_id = selected_run["run_id"] if selected_run else None
    snapshot_run_id = (
        selected_run_id
        if selected_run_id is not None
        and (source_path / "runs" / selected_run_id).is_dir()
        else None
    )
    if (
        selected_run_id is not None
        and snapshot_run_id is None
        and corpus.get("provenance_status") != "legacy_unverified"
    ):
        raise FileNotFoundError(
            f"managed run snapshot is missing: {source_path / 'runs' / selected_run_id}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.export-", dir=destination.parent)
    )
    copied_products: list[str] = []
    rewritten_manifests: list[str] = []
    try:
        # mkdtemp creates the directory; copy helper expects a new target.
        temporary.rmdir()
        _copy_corpus_tree(source_path, temporary, snapshot_run_id)
        if standalone:
            _materialize_standalone_symlinks(
                temporary, source_root=source_path, store=store
            )
        rewritten_manifests = _rewrite_export_manifests(
            temporary,
            store,
            selected_run_id=snapshot_run_id,
            standalone=standalone,
        )
        for reference_path in _reference_documents(temporary):
            document = json.loads(reference_path.read_text(encoding="utf-8"))
            rewritten = _rewrite_product_document(
                document,
                document_path=reference_path,
                export_root=temporary,
                store=store,
                standalone=standalone,
            )
            atomic_json(reference_path, rewritten)
            copied_products.append(reference_path.relative_to(temporary).as_posix())
        os.replace(temporary, destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {
        "source": str(source_path),
        "destination": str(destination),
        "corpus_id": corpus.get("corpus_id"),
        "run_id": snapshot_run_id,
        "standalone": standalone,
        "storage_kind": "standalone" if standalone else "shared_library",
        "manifest_documents": rewritten_manifests,
        "product_reference_documents": copied_products,
        "modifies_source": False,
        "overwrites_destination": False,
    }
