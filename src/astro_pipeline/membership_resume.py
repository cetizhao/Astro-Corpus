"""Validate and load a frozen, published membership selection for downstream work.

Only an explicitly named run in the *same* corpus is eligible. Neither the
mutable corpus root nor the latest global Gaia source table is selection data.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import numpy as np
import pandas as pd

from astro_pipeline.library.files import sha256_file
from astro_pipeline.library.identity import canonical_json, identity_hash
from astro_pipeline.manifest import read_manifest
from astro_pipeline.membership import (
    MembershipResolution, _find_coordinate_descriptor, _json_records,
    _normalise_gaia_id,
)

_SELECTION_FILES = (
    "manifest.json", "membership_catalog.csv", "membership_catalog.parquet",
    "membership_catalog_full.parquet", "unmatched_members.csv",
    "members.csv", "members.parquet", "input_rows.parquet", "input_matches.parquet",
    "unmatched_input_rows.parquet",
)
_UNMATCHED = {"unmatched", "invalid", "id_not_found"}


@dataclass
class FrozenMembershipSelection:
    resolution: MembershipResolution
    input_rows: pd.DataFrame
    matches: pd.DataFrame
    resolved_metadata: dict[str, Any]
    source_run_id: str
    source_files: dict[str, Path]
    source_hashes: dict[str, str]
    coordinate_corrected: bool


def _require(condition: bool, detail: str) -> None:
    if not condition:
        raise ValueError(f"Cannot resume membership selection: {detail}")


def _frame(path: Path, *, csv_strings: bool = False) -> pd.DataFrame:
    try:
        if path.suffix == ".parquet":
            return pd.read_parquet(path)
        return pd.read_csv(path, dtype=str, keep_default_na=False) if csv_strings else pd.read_csv(path)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError(f"Cannot read frozen membership artifact {path.name}") from exc


def _schema_descriptor(schema: Any) -> dict[str, Any] | None:
    _require(isinstance(schema, list) and bool(schema), "missing original VizieR table schema")
    columns = {}
    for entry in schema:
        _require(isinstance(entry, dict) and isinstance(entry.get("name"), str), "invalid table schema")
        name = entry["name"]
        _require(name not in columns, "duplicate table schema column")
        try:
            dtype = np.dtype(entry["dtype"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Cannot interpret original column dtype: {name}") from exc
        columns[name] = SimpleNamespace(
            dtype=dtype, unit=entry.get("unit"), description=entry.get("description") or "",
        )
    # _find_coordinate_descriptor uses only colnames and per-column metadata.
    class SchemaTable:
        colnames = list(columns)

        def __getitem__(self, name):
            return columns[name]

    return _find_coordinate_descriptor(SchemaTable())


def _correct_metadata(metadata: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    previous = metadata.get("coordinate_descriptor")
    reconstructed = _schema_descriptor(metadata.get("table_schema"))
    _require(previous is None or isinstance(previous, dict), "invalid coordinate descriptor")
    if previous is None:
        _require(reconstructed is None, "coordinate descriptor missing despite original schema")
        return dict(metadata), False
    _require(reconstructed is not None, "original schema cannot verify coordinates")
    for key, value in reconstructed.items():
        if key not in {"reference_epoch", "epoch_source"}:
            _require(previous.get(key) == value, f"coordinate {key} differs from original schema")
    changed = previous != reconstructed
    if changed:
        # The historical resolver mistook RAJ2000/DEJ2000 (an equinox) for
        # the observing epoch. Never preserve that inference as scientific fact.
        _require(
            previous.get("epoch_source") == "column_names"
            and previous.get("reference_epoch") == "J2000"
            and reconstructed["epoch_source"] == "not_declared"
            and reconstructed["reference_epoch"] is None,
            "coordinate epoch conflicts with original schema",
        )
    return {**metadata, "coordinate_descriptor": reconstructed}, changed


def load_published_membership_selection(
    store, *, out_dir: Path, label: str, run_id: str,
    catalog_id: str, table_id: str | None, limit: int | None,
    match_radius_arcsec: float,
) -> FrozenMembershipSelection:
    """Fail closed before creating a new run, then return immutable run data."""
    _require(isinstance(run_id, str), "run ID must be a UUID")
    try:
        _require(str(UUID(run_id)) == run_id, "run ID must be a canonical UUID")
    except (ValueError, AttributeError) as exc:
        raise ValueError("Cannot resume membership selection: invalid run UUID") from exc
    out_dir = Path(out_dir).resolve()
    run_root = out_dir / "runs"
    run_dir = run_root / run_id
    root_manifest = read_manifest(out_dir / "manifest.json")

    rows = store.read(
        "SELECT r.*, c.canonical_path, c.label, q.contract_json "
        "FROM runs AS r JOIN corpora AS c ON c.corpus_id = r.corpus_id "
        "JOIN requests AS q ON q.request_id = r.request_id "
        "WHERE r.run_id = ?", (run_id,),
    )
    _require(len(rows) == 1, "unknown or unregistered run")
    run = rows[0]
    _require(run["canonical_path"] == str(out_dir) and run["label"] == label,
             "source run belongs to another corpus")
    _require(not run_root.is_symlink(), "run directory must not be a symlink")
    _require(run_dir.is_dir() and run_dir.resolve() == run_dir, "immutable run directory is missing or redirected")
    _require(isinstance(root_manifest, dict), "corpus manifest missing")
    meta = json.loads(run["metadata_json"])
    _require(
        run["status"] in {"completed", "partial"}
        and run["finished_at"] is not None
        and meta.get("published") is True
        and meta.get("members_indexed") is True
        and meta.get("view_pending") is False
        and meta.get("snapshot_path") == str(run_dir),
        "run is not a finished, published immutable snapshot",
    )
    contract = json.loads(run["contract_json"])
    _require(
        contract.get("release") == "dr3"
        and contract.get("input_mode") == "membership_catalog"
        and contract.get("limit") == limit
        and isinstance(contract.get("selector"), dict),
        "release, input mode, or row limit differs from source run",
    )
    selector = contract["selector"]
    requested_table = (f"{catalog_id}/{table_id}" if table_id and "/" not in table_id else table_id)
    _require(selector.get("catalog_id") == catalog_id
             and isinstance(selector.get("table_id"), str)
             and (requested_table is None or requested_table == selector["table_id"])
             and selector.get("match_radius_arcsec") == float(match_radius_arcsec),
             "catalog, table, or match radius differs from source run")
    _require(
        root_manifest.get("corpus", {}).get("input_contract") == contract
        and root_manifest.get("corpus_id") == run["corpus_id"]
        and root_manifest.get("corpus", {}).get("label") == label,
        "current corpus contract differs from published selection",
    )
    records = store.read(
        "SELECT path, sha256, row_count FROM run_artifacts WHERE run_id = ?", (run_id,),
    )
    by_path: dict[Path, list[dict[str, Any]]] = {}
    for item in records:
        path = Path(item["path"])
        by_path.setdefault(path, []).append(item)
    paths = {}
    hashes = {}
    for name in _SELECTION_FILES:
        path = run_dir / name
        registered = by_path.get(path, [])
        _require(len(registered) == 1, f"missing or ambiguous registered {name}")
        _require(path.is_file() and not path.is_symlink() and path.resolve() == path,
                 f"missing or redirected immutable {name}")
        digest = registered[0]["sha256"]
        _require(sha256_file(path) == digest, f"checksum mismatch for {name}")
        paths[name] = path
        hashes[name] = digest
    try:
        snapshot = json.loads(paths["manifest.json"].read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("Cannot resume membership selection: invalid run manifest") from exc
    _require(
        snapshot.get("snapshot_run_id") == run_id
        and snapshot.get("corpus_id") == run["corpus_id"]
        and snapshot.get("request_id") == run["request_id"]
        and snapshot.get("status") == run["status"]
        and snapshot.get("storage", {}).get("kind") == "shared_library"
        and snapshot.get("corpus", {}).get("input_contract") == contract
        and snapshot.get("stages", {}).get("resolve_members", {}).get("status") == "completed"
        and snapshot.get("stages", {}).get("save_members", {}).get("status") == "completed",
        "immutable manifest has inconsistent run identity or selection stages",
    )
    inherited = snapshot.get("resolved_input")
    _require(isinstance(inherited, dict) and meta.get("resolved_input") == inherited,
             "selection metadata disagrees with indexed run")
    _require(inherited.get("catalog_id") == catalog_id
             and inherited.get("table_id") == selector["table_id"],
             "resolved catalogue identity disagrees with request")
    corrected, corrected_epoch = _correct_metadata(inherited)

    full = _frame(paths["membership_catalog_full.parquet"])
    original = _frame(paths["membership_catalog.parquet"])
    members = _frame(paths["members.parquet"])
    unmatched = _frame(paths["unmatched_members.csv"], csv_strings=True)
    input_rows = _frame(paths["input_rows.parquet"])
    matches = _frame(paths["input_matches.parquet"])
    unmatched_input = _frame(paths["unmatched_input_rows.parquet"])
    for name, frame in (
        ("membership_catalog_full.parquet", full),
        ("membership_catalog.parquet", original),
        ("unmatched_members.csv", unmatched),
        ("members.parquet", members),
        ("input_rows.parquet", input_rows),
        ("input_matches.parquet", matches),
        ("unmatched_input_rows.parquet", unmatched_input),
    ):
        _require(by_path[paths[name]][0]["row_count"] == len(frame),
                 f"registered row count differs for {name}")
    artifacts = snapshot.get("artifacts", {})
    for kind, name in (
        ("membership_catalog_csv", "membership_catalog.csv"),
        ("membership_catalog_parquet", "membership_catalog.parquet"),
        ("membership_catalog_full_parquet", "membership_catalog_full.parquet"),
        ("unmatched_members_csv", "unmatched_members.csv"),
        ("members_parquet", "members.parquet"),
        ("input_rows", "input_rows.parquet"),
        ("input_matches", "input_matches.parquet"),
    ):
        artifact = artifacts.get(kind, {})
        _require(artifact.get("path") == str(paths[name])
                 and artifact.get("sha256", hashes[name]) == hashes[name],
                 f"immutable manifest artifact differs for {name}")
    schema = inherited["table_schema"]
    _require(list(full.columns) == ["membership_catalog_row", *(entry["name"] for entry in schema)],
             "original table columns differ from VizieR schema")
    _require(
        identity_hash({"rows": _json_records(full), "schema": schema}, namespace="vizier-table")
        == inherited.get("table_content_hash"),
        "original table content hash mismatch",
    )
    _require(len(full) > 0 and full["membership_catalog_row"].tolist() == list(range(len(full))),
             "original table row identifiers or ordinals changed")
    selected_count = len(full) if limit is None else min(len(full), limit)
    _require(_json_records(original) == _json_records(full.head(selected_count)),
             "selected table does not match full table and row limit")
    # The CSVs are also immutable and registry-hashed; check their row identity
    # without coercing literature strings such as identifiers with leading zeros.
    original_csv = _frame(paths["membership_catalog.csv"], csv_strings=True)
    _require(len(original_csv) == selected_count
             and original_csv["membership_catalog_row"].tolist() == [str(i) for i in range(selected_count)],
             "selected CSV row identity changed")
    _require(len(input_rows) == len(matches) == len(full)
             and input_rows["input_row_id"].tolist() == list(range(len(full)))
             and input_rows["ordinal"].tolist() == list(range(len(full)))
             and matches["input_row_id"].tolist() == list(range(len(full))),
             "input/match row IDs or ordinals changed")
    _require(len(unmatched_input) == int(input_rows["status"].isin(_UNMATCHED).sum())
             and _json_records(unmatched_input) == _json_records(input_rows.loc[input_rows["status"].isin(_UNMATCHED)]),
             "unmatched input-row evidence differs")
    frozen_rows = _json_records(full)
    for index, (input_row, match_row) in enumerate(zip(_json_records(input_rows), _json_records(matches))):
        _require(json.loads(input_row["raw_json"]) == frozen_rows[index],
                 f"raw input differs at ordinal {index}")
        _require(input_row["status"] == match_row["status"]
                 and match_row["match_method"] == inherited["match_method"],
                 f"match status or method differs at ordinal {index}")
        if index >= selected_count:
            _require(input_row["status"] == "not_selected_by_limit", "unselected row has match outcome")
        else:
            _require(input_row["status"] in {"matched", *_UNMATCHED}, "selected row has no match outcome")
    matched_ids = [i for i, status in enumerate(input_rows["status"]) if status == "matched"]
    unmatched_ids = [i for i, status in enumerate(input_rows["status"]) if status in _UNMATCHED]
    member_rows = _json_records(members)
    _require("membership_catalog_row" in members and "Gaia" in members
             and members["membership_catalog_row"].tolist() == matched_ids
             and members["input_row_id"].tolist() == matched_ids,
             "matched occurrences or duplicate row ordinals differ")
    _require(unmatched["membership_catalog_row"].tolist() == [str(i) for i in unmatched_ids],
             "unmatched catalogue rows differ from match outcomes")
    for index, row in zip(matched_ids, member_rows):
        match_row = _json_records(matches.iloc[[index]])[0]
        _require(_normalise_gaia_id(row["Gaia"]) == _normalise_gaia_id(match_row["source_id"])
                 and canonical_json(row.get("membership_match_sep_arcsec")) == canonical_json(match_row["match_sep_arcsec"])
                 and canonical_json(row.get("membership_match_candidate_count")) == canonical_json(match_row["match_candidate_count"])
                 and row.get("membership_match_method") == inherited["match_method"],
                 f"frozen Gaia/match evidence differs at ordinal {index}")
        for key, value in frozen_rows[index].items():
            if key != "membership_catalog_row":
                _require(row.get(f"member_{key}") == value,
                         f"literature member value differs at ordinal {index}: {key}")
        previous_epoch = inherited["coordinate_descriptor"] if inherited["coordinate_descriptor"] else {}
        _require(row.get("membership_coordinate_epoch") == previous_epoch.get("reference_epoch")
                 and row.get("membership_coordinate_epoch_source") == previous_epoch.get("epoch_source"),
                 f"inherited coordinate epoch evidence differs at ordinal {index}")
    for index, row in zip(unmatched_ids, _json_records(unmatched)):
        previous_epoch = inherited["coordinate_descriptor"] if inherited["coordinate_descriptor"] else {}
        _require((row.get("membership_coordinate_epoch") or None) == previous_epoch.get("reference_epoch")
                 and (row.get("membership_coordinate_epoch_source") or None) == previous_epoch.get("epoch_source"),
                 f"inherited unmatched epoch evidence differs at ordinal {index}")
    _require(all(_normalise_gaia_id(v) is not None for v in members["Gaia"]), "invalid frozen source ID")
    _require("gaia_release" in members and set(members["gaia_release"].dropna()) == {"dr3"},
             "frozen members are not Gaia DR3")
    if corrected_epoch:
        members = members.copy()
        members["membership_coordinate_epoch"] = None
        members["membership_coordinate_epoch_source"] = "not_declared"
        unmatched = unmatched.copy()
        unmatched["membership_coordinate_epoch"] = ""
        unmatched["membership_coordinate_epoch_source"] = "not_declared"
    indexed_inputs = store.read(
        "SELECT input_row_id, ordinal, status, raw_json FROM input_rows WHERE run_id = ? ORDER BY ordinal",
        (run_id,),
    )
    indexed_members = store.read(
        "SELECT member_ordinal, release, source_id, input_row_id, match_json "
        "FROM sample_members WHERE run_id = ? ORDER BY member_ordinal", (run_id,),
    )
    _require(len(indexed_inputs) == len(full) and len(indexed_members) == len(matched_ids),
             "indexed row accounting differs from snapshot")
    for index, item in enumerate(indexed_inputs):
        # The index stores raw_json as a JSON-encoded *string*; the frozen
        # input_rows parquet holds that original string unchanged.
        _require(item["input_row_id"] == str(index) and item["ordinal"] == index
                 and item["status"] == input_rows.iloc[index]["status"]
                 and json.loads(item["raw_json"]) == input_rows.iloc[index]["raw_json"],
                 f"indexed input differs at ordinal {index}")
    for ordinal, (item, row, index) in enumerate(zip(indexed_members, member_rows, matched_ids)):
        evidence = json.loads(item["match_json"])
        expected_match = _json_records(matches.iloc[[index]])[0]
        _require(item["member_ordinal"] == ordinal and item["release"] == "dr3"
                 and item["source_id"] == _normalise_gaia_id(row["Gaia"])
                 and item["input_row_id"] == str(index)
                 and all(canonical_json(evidence.get(key)) == canonical_json(value)
                         for key, value in expected_match.items()),
                 f"indexed occurrence/match differs at ordinal {index}")
    _require(meta.get("n_input_rows") == len(full)
             and meta.get("n_sample_members") == len(matched_ids)
             and meta.get("n_unique_sources") == members["Gaia"].nunique(),
             "published run accounting differs from snapshot")
    _require(snapshot.get("stages", {}).get("resolve_members", {}).get("unmatched") == len(unmatched),
             "resolved unmatched count differs")
    correction = (
        "RAJ2000/DEJ2000 denote equinox, not observing epoch; original schema "
        "does not declare an epoch. Match separations are inherited, not recomputed."
        if corrected_epoch else None
    )
    corrected["selection_resume"] = {
        "source_run_id": run_id,
        "source_snapshot_manifest_sha256": hashes["manifest.json"],
        "inherited_resolved_input": inherited,
        "coordinate_interpretation": correction or "verified against original table schema; unchanged",
    }
    return FrozenMembershipSelection(
        resolution=MembershipResolution(
            catalog_id=catalog_id, table_id=selector["table_id"],
            table_description=inherited["table_description"],
            catalog_title=inherited["catalog_title"], original=original,
            members=members, unmatched=unmatched, match_method=inherited["match_method"],
            match_radius_arcsec=inherited["match_radius_arcsec"], all_rows=full,
            table_schema=schema, coordinate_descriptor=corrected["coordinate_descriptor"],
        ),
        input_rows=input_rows, matches=matches, resolved_metadata=corrected,
        source_run_id=run_id, source_files=paths, source_hashes=hashes,
        coordinate_corrected=corrected_epoch,
    )
