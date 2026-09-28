"""Read and relocate pinned Gaia product reference documents.

A product directory may contain a version-2 ``product_refs.json`` document::

    {
      "schema_version": "2.0",
      "storage": {"kind": "shared_library", ...},
      "contract": {"release": "dr3", "retrieval_type": "XP_SAMPLED", ...},
      "items": [{"identifier": "...", "status": "ready", ...}]
    }

A corpus-root document uses ``kind="corpus_product_refs"`` and stores those
per-product documents in ``products``.  Reference documents pin exact product
versions and are authoritative: readers never replace a missing, failed, or
corrupt referenced item with a legacy file or the library's current version.
Storage may point to verified shared-library blobs or to a self-contained
``standalone`` export whose artifact paths are confined to the reference-file
directory. No function in this module opens SQLite or performs network I/O.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from astro_pipeline.gaia_releases import normalize_gaia_release
from astro_pipeline.library.files import BlobIntegrityError, BlobStore, atomic_json, sha256_file
from astro_pipeline.library.identity import normalize_identifier

REFERENCE_FILENAME = "product_refs.json"
REFERENCE_SCHEMA_VERSION = "2.0"
CORPUS_REFERENCE_KIND = "corpus_product_refs"
CORPUS_MANIFEST_FILENAME = "manifest.json"
_SAFE_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_PRODUCT_SUBDIRS = {
    "XP_CONTINUOUS": "gaia_xp_continuous",
    "XP_SAMPLED": "gaia_xp_sampled",
}


class ProductReferenceError(LookupError):
    """An authoritative reference document cannot satisfy a product lookup."""


def active_corpus_dir(directory: Path | str) -> Path:
    """Select the immutable published run behind a shared corpus view.

    Shared-library corpus roots expose compatibility files while a new run is
    being promoted. Readers must therefore use ``runs/<active_run_id>`` rather
    than those mutable top-level files. Snapshot manifests and standalone or
    legacy corpora are already pinned and remain at their supplied paths.
    Product subdirectories and their reference files inherit the manifest of
    their immediate corpus parent. Internal run directories without a manifest,
    and unrelated generic product labels, are left unchanged.
    """
    path = Path(directory)
    is_reference_file = path.name == REFERENCE_FILENAME
    base = path.parent if is_reference_file else path

    manifest_root: Path | None = None
    suffix = Path(path.name) if is_reference_file else Path()
    direct_manifest = base / CORPUS_MANIFEST_FILENAME
    if direct_manifest.is_file():
        manifest_root = base
    else:
        parent_manifest = base.parent / CORPUS_MANIFEST_FILENAME
        if base.name != "runs" and parent_manifest.is_file():
            manifest_root = base.parent
            suffix = Path(base.name) / suffix

    if manifest_root is None:
        return path

    manifest_path = manifest_root / CORPUS_MANIFEST_FILENAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductReferenceError(
            f"cannot read corpus manifest {manifest_path}: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise ProductReferenceError(
            f"corpus manifest must be an object: {manifest_path}"
        )

    storage = manifest.get("storage")
    if not isinstance(storage, dict) or storage.get("kind") != "shared_library":
        return path
    if manifest.get("snapshot_run_id") not in (None, ""):
        return path

    active_run_id = manifest.get("active_run_id")
    if (
        not isinstance(active_run_id, str)
        or not _SAFE_RUN_ID.fullmatch(active_run_id)
    ):
        raise ProductReferenceError(
            f"shared corpus has no safe active_run_id: {manifest_path}"
        )
    corpus_root = manifest_root.resolve()
    run_root = (corpus_root / "runs").resolve()
    active_root = (run_root / active_run_id).resolve()
    if (
        run_root.parent != corpus_root
        or active_root.parent != run_root
        or not active_root.is_dir()
    ):
        raise ProductReferenceError(
            f"published corpus run is missing: {active_root}"
        )
    return active_root / suffix


def read_reference_documents(directory: Path | str) -> list[dict[str, Any]]:
    """Return effective per-product v2 documents without touching SQLite.

    ``directory`` may be a ``product_refs.json`` file, a product directory, or
    a corpus root.  Corpus aggregate documents are flattened to their
    per-product ``products`` entries.  If a corpus has no aggregate view yet,
    the two conventional Gaia XP product subdirectories are inspected.  The
    returned dictionaries retain the documented v2 shape and are independent
    deep copies; storage paths remain relative to the reference file that was
    read, so callers needing resolved paths should use :func:`resolve_product`.
    """
    path = active_corpus_dir(directory)
    if path.is_file():
        return [deepcopy(document) for document, _ in _read_document_file(path)]

    root_reference = path / REFERENCE_FILENAME
    if root_reference.is_file():
        return [
            deepcopy(document)
            for document, _ in _read_document_file(root_reference)
        ]

    documents: list[dict[str, Any]] = []
    for subdirectory in _PRODUCT_SUBDIRS.values():
        reference = path / subdirectory / REFERENCE_FILENAME
        if reference.is_file():
            documents.extend(
                deepcopy(document)
                for document, _ in _read_document_file(reference)
            )
    return documents


def resolve_product(
    directory: Path | str,
    identifier: str,
    retrieval_type: str,
    release: str = "dr3",
) -> dict[str, Any]:
    """Resolve and verify one pinned normalized product artifact.

    The return value contains ``path`` (:class:`~pathlib.Path`), ``sha256``,
    ``version_id``, ``item_key``, and ``storage_kind``.  Version-2 references
    are authoritative whenever present for the requested product.  Legacy Gaia
    XP ``<source_id>.fits`` files are considered only when no applicable v2
    reference document exists.
    """
    directory = active_corpus_dir(directory)
    identifier = normalize_identifier(identifier)
    retrieval_type = str(retrieval_type).strip().upper()
    release = normalize_gaia_release(release)

    documents, authoritative = _documents_for_product(directory, retrieval_type)
    if authoritative:
        matching = []
        for document, base_directory in documents:
            contract = _product_contract(document)
            if str(contract.get("retrieval_type", "")).strip().upper() == retrieval_type:
                matching.append((document, base_directory, contract))
        if not matching:
            raise ProductReferenceError(
                f"authoritative references contain no {retrieval_type} product"
            )
        if len(matching) > 1:
            raise ProductReferenceError(
                f"authoritative references contain multiple {retrieval_type} products"
            )

        document, base_directory, contract = matching[0]
        contract_release = normalize_gaia_release(str(contract.get("release", "")))
        if contract_release != release:
            raise ProductReferenceError(
                f"referenced product release {contract_release!r} does not match {release!r}"
            )
        if retrieval_type in _PRODUCT_SUBDIRS and str(
            contract.get("linking_parameter", "")
        ).strip().upper() != "SOURCE_ID":
            raise ProductReferenceError(
                f"{retrieval_type} source spectra require SOURCE_ID linking"
            )

        item = _find_item(document, identifier)
        status = str(item.get("status", "")).strip().lower()
        if status != "ready":
            raise ProductReferenceError(
                f"referenced {retrieval_type} item {identifier} is {status or 'unknown'}"
            )
        version_id = item.get("version_id")
        if version_id is None or not str(version_id).strip():
            raise ProductReferenceError(
                f"ready referenced item {identifier} has no pinned version_id"
            )
        item_key = item.get("item_key")
        if item_key is None or not str(item_key).strip():
            raise ProductReferenceError(
                f"ready referenced item {identifier} has no item_key"
            )

        artifact = _normalized_artifact(item, identifier)
        blob = _blob_record(artifact)
        path = _verify_from_storage(document, base_directory, blob)
        return {
            "path": path,
            "sha256": blob["sha256"],
            "version_id": str(version_id),
            "item_key": str(item_key),
            "storage_kind": str(document["storage"]["kind"]),
        }

    return _resolve_legacy(directory, identifier, retrieval_type, release)


def collect_corpus_refs(
    corpus_dir: Path | str,
    *,
    run_id: str | None = None,
    release: str = "dr3",
) -> Path:
    """Write a corpus-root aggregate of conventional Gaia XP product refs.

    Each embedded product document is copied unchanged except that its storage
    locators are rebased from the product directory to the corpus root.  The
    exact item/version pins are therefore preserved.
    """
    corpus_dir = Path(corpus_dir)
    release = normalize_gaia_release(release)
    products: list[dict[str, Any]] = []
    for subdirectory in _PRODUCT_SUBDIRS.values():
        reference_path = corpus_dir / subdirectory / REFERENCE_FILENAME
        if not reference_path.is_file():
            continue
        loaded = _read_document_file(reference_path)
        if len(loaded) != 1:
            raise ValueError(f"product reference must contain one document: {reference_path}")
        document, source_base = loaded[0]
        contract = _product_contract(document)
        contract_release = normalize_gaia_release(str(contract.get("release", "")))
        if contract_release != release:
            raise ValueError(
                f"{reference_path} release {contract_release!r} does not match {release!r}"
            )
        products.append(_rebase_product_document(document, source_base, corpus_dir))

    aggregate = {
        "schema_version": REFERENCE_SCHEMA_VERSION,
        "kind": CORPUS_REFERENCE_KIND,
        "gaia_release": release,
        "run_id": run_id,
        "products": products,
    }
    destination = corpus_dir / REFERENCE_FILENAME
    return atomic_json(destination, aggregate)


def copy_reference_file(src: Path | str, dst: Path | str) -> Path:
    """Copy a v2 reference view and rebase every embedded storage locator."""
    source = Path(src)
    destination = Path(dst)
    payload = _load_json_object(source)
    _validate_schema(payload, source)
    copied = deepcopy(payload)
    if copied.get("kind") == CORPUS_REFERENCE_KIND:
        products = copied.get("products")
        if not isinstance(products, list):
            raise ValueError(f"invalid corpus product references in {source}")
        copied["products"] = [
            _rebase_product_document(_validate_product_document(item, source), source.parent, destination.parent)
            for item in products
        ]
    else:
        copied = _rebase_product_document(
            _validate_product_document(copied, source),
            source.parent,
            destination.parent,
        )
    return atomic_json(destination, copied)


def count_cached_products(product_dir: Path | str, ids: Iterable[str]) -> int:
    """Count identifiers whose ready product bytes verify successfully.

    The retrieval type and release are read from a direct v2 document.  For a
    legacy product directory they are inferred from the conventional directory
    name and DR3 semantics.
    """
    product_dir = active_corpus_dir(product_dir)
    reference_path = product_dir / REFERENCE_FILENAME
    if reference_path.is_file():
        documents = _read_document_file(reference_path)
        if len(documents) != 1:
            raise ValueError(f"expected one product document in {reference_path}")
        document, _ = documents[0]
        contract = _product_contract(document)
        retrieval_type = str(contract.get("retrieval_type", "")).strip().upper()
        release = normalize_gaia_release(str(contract.get("release", "")))
    else:
        reverse = {value: key for key, value in _PRODUCT_SUBDIRS.items()}
        retrieval_type = reverse.get(product_dir.name, "")
        if not retrieval_type:
            raise ValueError(
                f"cannot infer retrieval type for legacy directory {product_dir}"
            )
        release = "dr3"

    count = 0
    for identifier in dict.fromkeys(str(value) for value in ids):
        try:
            resolve_product(
                product_dir,
                identifier,
                retrieval_type,
                release=release,
            )
        except (BlobIntegrityError, FileNotFoundError, KeyError, ProductReferenceError, ValueError):
            continue
        count += 1
    return count


# --------------------------------------------------------------------------- I/O
def _read_document_file(path: Path) -> list[tuple[dict[str, Any], Path]]:
    payload = _load_json_object(path)
    _validate_schema(payload, path)
    if payload.get("kind") == CORPUS_REFERENCE_KIND:
        products = payload.get("products")
        if not isinstance(products, list):
            raise ValueError(f"invalid corpus product references in {path}")
        aggregate_release = normalize_gaia_release(
            str(payload.get("gaia_release", ""))
        )
        result: list[tuple[dict[str, Any], Path]] = []
        for product in products:
            document = _validate_product_document(product, path)
            contract_release = normalize_gaia_release(
                str(_product_contract(document).get("release", ""))
            )
            if contract_release != aggregate_release:
                raise ValueError(
                    f"aggregate release {aggregate_release!r} conflicts with "
                    f"product release {contract_release!r} in {path}"
                )
            result.append((document, path.parent))
        return result
    return [(_validate_product_document(payload, path), path.parent)]


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON reference document {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"reference document must be an object: {path}")
    return payload


def _validate_schema(payload: Mapping[str, Any], path: Path) -> None:
    if str(payload.get("schema_version", "")) != REFERENCE_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported product reference schema in {path}: "
            f"{payload.get('schema_version')!r}"
        )


def _validate_product_document(
    payload: Any, path: Path
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError(f"product reference entry must be an object in {path}")
    _validate_schema(payload, path)
    if payload.get("kind") == CORPUS_REFERENCE_KIND:
        raise ValueError(f"nested corpus product references are not supported in {path}")
    storage = payload.get("storage")
    contract = payload.get("contract")
    items = payload.get("items")
    if not isinstance(storage, dict) or not isinstance(contract, dict) or not isinstance(items, list):
        raise ValueError(f"malformed product reference document in {path}")
    storage_kind = str(storage.get("kind", ""))
    if storage_kind not in {"shared_library", "standalone"}:
        raise ValueError(
            f"unsupported product storage kind in {path}: {storage.get('kind')!r}"
        )
    if storage_kind == "standalone" and storage.get("root") != ".":
        raise ValueError(
            f"standalone product storage root must be '.' in {path}"
        )
    return payload


def _documents_for_product(
    directory: Path, retrieval_type: str
) -> tuple[list[tuple[dict[str, Any], Path]], bool]:
    if directory.is_file():
        return _read_document_file(directory), True

    root_reference = directory / REFERENCE_FILENAME
    if root_reference.is_file():
        return _read_document_file(root_reference), True

    subdirectory = _PRODUCT_SUBDIRS.get(retrieval_type)
    if subdirectory and directory.name != subdirectory:
        product_reference = directory / subdirectory / REFERENCE_FILENAME
        if product_reference.is_file():
            return _read_document_file(product_reference), True
    return [], False


# ---------------------------------------------------------------------- resolve
def _product_contract(document: Mapping[str, Any]) -> dict[str, Any]:
    contract = document.get("contract")
    if not isinstance(contract, dict):
        raise ValueError("product reference contract must be an object")
    return contract


def _find_item(document: Mapping[str, Any], identifier: str) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for candidate in document.get("items", []):
        if not isinstance(candidate, dict):
            raise ValueError("product reference item must be an object")
        try:
            candidate_identifier = normalize_identifier(candidate.get("identifier"))
        except (TypeError, ValueError):
            continue
        if candidate_identifier == identifier:
            matches.append(candidate)
    if not matches:
        raise ProductReferenceError(f"authoritative references contain no item {identifier}")
    if len(matches) > 1:
        raise ProductReferenceError(f"authoritative references duplicate item {identifier}")
    return matches[0]


def _normalized_artifact(
    item: Mapping[str, Any], identifier: str
) -> dict[str, Any]:
    artifacts = item.get("artifacts")
    if not isinstance(artifacts, list):
        raise ProductReferenceError(f"ready item {identifier} has no artifact list")
    normalized = [
        artifact
        for artifact in artifacts
        if isinstance(artifact, dict)
        and str(artifact.get("role", "")).strip().lower() == "normalized"
    ]
    if not normalized:
        raise ProductReferenceError(f"ready item {identifier} has no normalized artifact")
    if len(normalized) == 1:
        return normalized[0]

    primary = [
        artifact
        for artifact in normalized
        if isinstance(artifact.get("metadata"), dict)
        and artifact["metadata"].get("is_primary") is True
    ]
    if len(primary) == 1:
        return primary[0]
    if len(primary) > 1:
        raise ProductReferenceError(
            f"ready item {identifier} has multiple primary normalized artifacts"
        )

    preferred_slots = {identifier, "primary", "normalized", "normalized:000"}
    preferred = [
        artifact
        for artifact in normalized
        if str(artifact.get("slot", "")) in preferred_slots
    ]
    if len(preferred) == 1:
        return preferred[0]
    raise ProductReferenceError(
        f"ready item {identifier} has ambiguous normalized artifacts"
    )


def _blob_record(artifact: Mapping[str, Any]) -> dict[str, Any]:
    blob = artifact.get("blob")
    if not isinstance(blob, dict):
        raise ProductReferenceError("normalized artifact has no blob metadata")
    required = {"sha256", "relative_path", "size_bytes", "content_type", "extension"}
    missing = sorted(required.difference(blob))
    if missing:
        raise ProductReferenceError(
            f"normalized artifact blob is missing fields: {', '.join(missing)}"
        )
    return dict(blob)


def _verify_from_storage(
    document: Mapping[str, Any], base_directory: Path, blob: Mapping[str, Any]
) -> Path:
    storage = document.get("storage")
    if not isinstance(storage, dict):
        raise ValueError("product reference storage must be an object")
    storage_kind = str(storage.get("kind", ""))
    if storage_kind == "standalone":
        return _verify_standalone_blob(storage, base_directory, blob)
    if storage_kind != "shared_library":
        raise ValueError(f"unsupported product storage kind: {storage_kind!r}")

    errors: list[BaseException] = []
    for root in _storage_root_candidates(storage, base_directory):
        try:
            return BlobStore(root).verify(dict(blob))
        except (BlobIntegrityError, FileNotFoundError, KeyError, OSError, ValueError) as exc:
            errors.append(exc)
    if errors:
        raise BlobIntegrityError(
            f"referenced blob {blob.get('sha256')} failed verification: {errors[-1]}"
        ) from errors[-1]
    raise BlobIntegrityError("product reference has no usable library root")


def _standalone_blob_path(
    storage: Mapping[str, Any], base_directory: Path, blob: Mapping[str, Any]
) -> Path:
    """Resolve a standalone artifact without allowing it outside its export."""
    if storage.get("root") != ".":
        raise BlobIntegrityError("standalone storage root must be '.'")
    relative_value = blob.get("relative_path")
    if relative_value in (None, ""):
        raise BlobIntegrityError("standalone artifact has no relative_path")
    relative_path = Path(str(relative_value))
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise BlobIntegrityError(
            f"standalone artifact path must be confined to the reference directory: "
            f"{relative_value!r}"
        )
    reference_directory = base_directory.resolve()
    candidate = (reference_directory / relative_path).resolve()
    if not candidate.is_relative_to(reference_directory):
        raise BlobIntegrityError(
            f"standalone artifact path escapes the reference directory: "
            f"{relative_value!r}"
        )
    return candidate


def _verify_standalone_blob(
    storage: Mapping[str, Any], base_directory: Path, blob: Mapping[str, Any]
) -> Path:
    path = _standalone_blob_path(storage, base_directory, blob)
    expected_size = blob.get("size_bytes")
    expected_sha256 = str(blob.get("sha256", ""))
    if not path.is_file():
        raise BlobIntegrityError(f"Standalone artifact is missing: {path}")
    if path.stat().st_size != expected_size:
        raise BlobIntegrityError(
            f"Standalone artifact size mismatch: {expected_sha256}"
        )
    if sha256_file(path) != expected_sha256:
        raise BlobIntegrityError(
            f"Standalone artifact checksum mismatch: {expected_sha256}"
        )
    return path


def _storage_root_candidates(
    storage: Mapping[str, Any], base_directory: Path
) -> list[Path]:
    roots: list[Path] = []
    relative = storage.get("relative_library_root")
    if relative not in (None, ""):
        relative_path = Path(str(relative))
        if relative_path.is_absolute():
            raise ValueError("relative_library_root must be relative")
        roots.append((base_directory / relative_path).resolve())
    absolute = storage.get("library_root")
    if absolute not in (None, ""):
        absolute_path = Path(str(absolute)).expanduser()
        if not absolute_path.is_absolute():
            raise ValueError("library_root must be absolute")
        roots.append(absolute_path.resolve())
    unique: list[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def _resolve_legacy(
    directory: Path,
    identifier: str,
    retrieval_type: str,
    release: str,
) -> dict[str, Any]:
    if release != "dr3":
        raise ProductReferenceError(
            f"legacy Gaia XP files have no verified {release!r} release provenance"
        )
    subdirectory = _PRODUCT_SUBDIRS.get(retrieval_type)
    if subdirectory is None:
        raise ProductReferenceError(
            f"legacy fallback is unsupported for {retrieval_type!r}"
        )
    candidates = []
    if directory.name == subdirectory:
        candidates.append(directory / f"{identifier}.fits")
    else:
        candidates.append(directory / subdirectory / f"{identifier}.fits")
    for candidate in candidates:
        if candidate.is_file():
            return {
                "path": candidate,
                "sha256": sha256_file(candidate),
                "version_id": None,
                "item_key": None,
                "storage_kind": "legacy_local",
            }
    raise ProductReferenceError(
        f"no cached {retrieval_type} product for Gaia DR3 {identifier} under {directory}"
    )


# --------------------------------------------------------------------- relocate
def _declared_storage_root(storage: Mapping[str, Any], source_base: Path) -> Path:
    candidates = _storage_root_candidates(storage, source_base)
    if not candidates:
        raise ValueError("product reference has no library root locator")
    return next((candidate for candidate in candidates if candidate.exists()), candidates[0])


def _rebase_product_document(
    document: Mapping[str, Any], source_base: Path, destination_base: Path
) -> dict[str, Any]:
    rebased = deepcopy(dict(document))
    storage = rebased.get("storage")
    if not isinstance(storage, dict):
        raise ValueError("product reference storage must be an object")
    storage_kind = str(storage.get("kind", ""))
    if storage_kind == "standalone":
        return _rebase_standalone_document(
            rebased,
            source_base=source_base,
            destination_base=destination_base,
        )
    if storage_kind != "shared_library":
        raise ValueError(f"unsupported product storage kind: {storage_kind!r}")
    library_root = _declared_storage_root(storage, source_base)
    storage["library_root"] = str(library_root)
    storage["relative_library_root"] = os.path.relpath(
        library_root, destination_base.resolve()
    )
    storage.setdefault("index_path", "index.sqlite3")
    return rebased


def _rebase_standalone_document(
    document: dict[str, Any], *, source_base: Path, destination_base: Path
) -> dict[str, Any]:
    """Rebase exported artifacts only when they remain inside the new root."""
    storage = document["storage"]
    destination_root = destination_base.resolve()
    for item in document.get("items", []):
        if not isinstance(item, dict):
            raise ValueError("product reference item must be an object")
        artifacts = item.get("artifacts", [])
        if not isinstance(artifacts, list):
            raise ValueError("product reference artifact list must be an array")
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise ValueError("product reference artifact must be an object")
            blob = artifact.get("blob")
            if not isinstance(blob, dict):
                continue
            source_path = _standalone_blob_path(storage, source_base, blob)
            try:
                destination_relative = source_path.relative_to(destination_root)
            except ValueError as exc:
                raise ValueError(
                    "standalone references cannot be relocated without their artifacts"
                ) from exc
            blob["relative_path"] = destination_relative.as_posix()
    storage["root"] = "."
    return document
