"""Shared-library product publication and label-view helpers.

This module deliberately knows nothing about Gaia's network client or table
normalization.  A downloader materializes one unique staging attempt, then uses
these helpers to publish immutable blobs, commit a fenced product version, and
write a label-local reference view.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any, TYPE_CHECKING

from astro_pipeline.library.files import Blob, atomic_json
from astro_pipeline.library.identity import (
    NORMALIZER_VERSION,
    identity_hash,
    product_item_key,
)

if TYPE_CHECKING:
    from astro_pipeline.library.store import Lease, LibraryStore

PRODUCT_REFS_SCHEMA_VERSION = "2.0"


@dataclass(frozen=True)
class StagedProductArtifact:
    """One staged file and the role it will have in a product version."""

    role: str
    slot: str
    path: Path
    extension: str
    content_type: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProductViewItem:
    """Current outcome for one identifier in one label-local product view."""

    identifier: str
    item_key: str
    status: str
    version_id: str | int | None = None
    created_at: Any = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    cache_status: str = ""
    attempt_id: str | int | None = None
    actual_batch_id: str = ""
    error: str | None = None
    previous_ready_version_id: str | int | None = None
    observed_at: Any = None
    expires_at: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "identifier": self.identifier,
            "item_key": self.item_key,
            "status": self.status,
            "version_id": self.version_id,
            "created_at": self.created_at,
            "cache_status": self.cache_status,
            "attempt_id": self.attempt_id,
            "actual_batch_id": self.actual_batch_id,
            "artifacts": self.artifacts,
            "error": self.error,
            "previous_ready_version_id": self.previous_ready_version_id,
            "observed_at": self.observed_at,
            "expires_at": self.expires_at,
        }


def product_view_lease_key(output_dir: Path | str) -> str:
    """Return a stable lease key for one materialized output directory."""
    return identity_hash(
        {"output_dir": str(Path(output_dir).resolve())},
        namespace="gaia-product-view",
    )


def publish_staged_artifacts(
    store: LibraryStore,
    artifacts: list[StagedProductArtifact],
    *,
    repair: bool = False,
    published_by_path: dict[Path, Blob | dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Publish staged files and return ``save_product`` artifact records.

    ``published_by_path`` lets several product items point at the same
    re-serialized Archive table without republishing it for every identifier.
    Content addressing still deduplicates across independent attempts.
    """
    published_by_path = published_by_path if published_by_path is not None else {}
    records: list[dict[str, Any]] = []
    for artifact in artifacts:
        source = artifact.path.resolve()
        blob = published_by_path.get(source)
        if blob is None:
            blob = store.blobs.publish(
                source,
                extension=artifact.extension,
                content_type=artifact.content_type,
                repair=repair,
            )
            published_by_path[source] = blob
        records.append(
            {
                "blob": blob,
                "role": artifact.role,
                "slot": artifact.slot,
                "metadata": artifact.metadata,
            }
        )
    return records


def save_staged_product(
    store: LibraryStore,
    *,
    contract: dict[str, Any],
    identifier: str,
    staged_artifacts: list[StagedProductArtifact],
    lease: Lease,
    metadata: dict[str, Any] | None = None,
    normalizer_version: str = NORMALIZER_VERSION,
    repair: bool = False,
    published_by_path: dict[Path, Blob | dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Publish and commit one fenced immutable product version."""
    records = publish_staged_artifacts(
        store,
        staged_artifacts,
        repair=repair,
        published_by_path=published_by_path,
    )
    return store.save_product(
        contract,
        identifier,
        records,
        lease,
        normalizer_version=normalizer_version,
        metadata=metadata,
    )


def verified_version_artifacts(
    store: LibraryStore, version: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return portable artifact refs after verifying every referenced blob."""
    verified: list[dict[str, Any]] = []
    for artifact in version.get("artifacts", []):
        blob_value = artifact["blob"]
        blob = blob_value.to_dict() if isinstance(blob_value, Blob) else dict(blob_value)
        path = store.blobs.verify(blob)
        verified.append(
            {
                "role": str(artifact.get("role", "")),
                "slot": str(artifact.get("slot", "")),
                "blob": blob,
                "metadata": dict(artifact.get("metadata") or {}),
                # The blob record plus library_root is the portable locator.  The
                # absolute path is included for direct local readers and diagnostics.
                "verified_path": str(path.resolve()),
            }
        )
    return verified


def ready_view_item(
    store: LibraryStore,
    version: dict[str, Any],
    *,
    cache_status: str,
    attempt_id: str | int | None = None,
    actual_batch_id: str = "",
) -> ProductViewItem:
    """Build a pinned ready view item from a verified store version."""
    return ProductViewItem(
        identifier=str(version["identifier"]),
        item_key=str(version["item_key"]),
        status="ready",
        version_id=version["version_id"],
        created_at=version.get("created_at"),
        artifacts=verified_version_artifacts(store, version),
        cache_status=cache_status,
        attempt_id=attempt_id,
        actual_batch_id=actual_batch_id,
    )


def merge_product_refs(
    path: Path | str,
    *,
    store: LibraryStore,
    contract: dict[str, Any],
    updates: list[ProductViewItem],
) -> dict[str, Any]:
    """Atomically merge current per-identifier refs for one compatible label."""
    path = Path(path)
    root = Path(store.root).resolve()
    db_path = Path(store.db_path).resolve()
    try:
        index_path = str(db_path.relative_to(root))
    except ValueError:
        index_path = str(db_path)
    storage = {
        "kind": "shared_library",
        "library_root": str(root),
        "relative_library_root": os.path.relpath(root, path.parent.resolve()),
        "index_path": index_path,
    }
    library_id = getattr(store, "library_id", None)
    if callable(library_id):
        library_id = library_id()
    if library_id is not None:
        storage["library_id"] = str(library_id)
    data: dict[str, Any] = {
        "schema_version": PRODUCT_REFS_SCHEMA_VERSION,
        "storage": storage,
        "contract": contract,
        "updated_at": None,
        "items": [],
    }
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") != PRODUCT_REFS_SCHEMA_VERSION:
            raise ValueError(f"Unsupported product refs schema at {path}")
        if data.get("contract") != contract:
            raise ValueError(f"Product refs contain a different request contract: {path}")
        existing_storage = data.get("storage") or {}
        existing_absolute = existing_storage.get("library_root")
        existing_relative = existing_storage.get("relative_library_root")
        absolute_matches = bool(existing_absolute) and Path(
            str(existing_absolute)
        ).resolve() == root
        relative_matches = bool(existing_relative) and (
            path.parent / str(existing_relative)
        ).resolve() == root
        if existing_storage.get("kind") != "shared_library" or not (
            absolute_matches or relative_matches
        ):
            raise ValueError(f"Product refs point at a different shared library: {path}")
        # Refresh absolute/relative locators after an entire data tree moves.
        data["storage"] = storage

    by_identifier = {
        str(item["identifier"]): item for item in data.get("items", [])
    }
    for update in updates:
        by_identifier[update.identifier] = update.to_dict()
    data["items"] = list(by_identifier.values())
    # Version/cache timestamps are preferred because they identify the data being
    # projected.  A pure failure still gets the store's injectable current time.
    latest = [
        item.created_at or item.observed_at
        for item in updates
        if item.created_at is not None or item.observed_at is not None
    ]
    data["updated_at"] = latest[-1] if latest else float(store.clock())
    atomic_json(path, data)
    return data


def merge_checkpoint_view(
    path: Path | str,
    *,
    source_name: str,
    updates: list[ProductViewItem],
) -> dict[str, Any]:
    """Write a compatible current checkpoint, replacing requested outcomes."""
    path = Path(path)
    data: dict[str, Any] = {
        "source_name": source_name,
        "completed": [],
        "failed": [],
    }
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    completed = {str(value) for value in data.get("completed", [])}
    failed_value = data.get("failed", [])
    failed = {
        str(value)
        for value in (
            failed_value.keys() if isinstance(failed_value, dict) else failed_value
        )
    }
    for update in updates:
        if update.status == "ready":
            completed.add(update.identifier)
            failed.discard(update.identifier)
        else:
            failed.add(update.identifier)
            completed.discard(update.identifier)
    data = {
        "source_name": source_name,
        "completed": sorted(completed),
        "failed": sorted(failed),
    }
    atomic_json(path, data)
    return data


def merge_artifact_view(
    path: Path | str,
    *,
    requested_identifiers: list[str] | tuple[str, ...],
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Replace current artifacts for requested IDs while retaining other IDs."""
    path = Path(path)
    data: dict[str, Any] = {
        "schema_version": "1.0",
        "updated_at": None,
        "artifacts": [],
    }
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    replaced = {str(identifier) for identifier in requested_identifiers}
    retained = [
        item
        for item in data.get("artifacts", [])
        if str(item.get("identifier", "")) not in replaced
    ]
    data["artifacts"] = retained + artifacts
    version_times = [item.get("version_created_at") for item in artifacts]
    data["updated_at"] = next(
        (value for value in reversed(version_times) if value is not None),
        data.get("updated_at"),
    )
    atomic_json(path, data)
    return data


def semantic_item_key(contract: dict[str, Any], identifier: str) -> str:
    """Expose the canonical key at the product-adapter boundary."""
    return product_item_key(contract, identifier)
