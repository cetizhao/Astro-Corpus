from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

import pytest

from astro_pipeline.library.files import BlobIntegrityError, BlobStore, sha256_file
from astro_pipeline.library.refs import (
    ProductReferenceError,
    active_corpus_dir,
    collect_corpus_refs,
    copy_reference_file,
    count_cached_products,
    read_reference_documents,
    resolve_product,
)


def _publish_blob(tmp_path: Path, library_root: Path, name: str, payload: bytes) -> dict:
    source = tmp_path / name
    source.write_bytes(payload)
    return BlobStore(library_root).publish(
        source,
        extension="fits",
        content_type="application/fits",
    ).to_dict()


def _product_document(
    product_dir: Path,
    library_root: Path,
    *,
    retrieval_type: str,
    items: list[dict],
    release: str = "dr3",
    linking_parameter: str = "SOURCE_ID",
) -> dict:
    return {
        "schema_version": "2.0",
        "storage": {
            "kind": "shared_library",
            "library_root": str(library_root.resolve()),
            "relative_library_root": os.path.relpath(library_root, product_dir),
            "index_path": "index.sqlite3",
        },
        "contract": {
            "release": release,
            "archive_tag": "Gaia DR3" if release == "dr3" else "Gaia DR4",
            "retrieval_type": retrieval_type,
            "data_structure": "INDIVIDUAL",
            "output_format": "fits",
            "linking_parameter": linking_parameter,
            "valid_data": False,
        },
        "items": items,
    }


def _item(
    identifier: str,
    blob: dict,
    *,
    version_id: str = "version-pinned",
    status: str = "ready",
) -> dict:
    return {
        "identifier": identifier,
        "item_key": f"item-{identifier}",
        "status": status,
        "version_id": version_id,
        "artifacts": [
            {
                "role": "normalized",
                "slot": identifier,
                "blob": blob,
                "metadata": {"test": True},
                "verified_path": "/stale/absolute/path.fits",
            }
        ],
        "error": None if status == "ready" else "refresh failed",
    }


def _write_refs(product_dir: Path, document: dict) -> Path:
    product_dir.mkdir(parents=True, exist_ok=True)
    path = product_dir / "product_refs.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _standalone_blob(path: Path, relative_path: str) -> dict:
    return {
        "sha256": sha256_file(path),
        "relative_path": relative_path,
        "size_bytes": path.stat().st_size,
        "content_type": "application/fits",
        "extension": "fits",
    }


def _standalone_product_document(
    *, retrieval_type: str, items: list[dict], release: str = "dr3"
) -> dict:
    return {
        "schema_version": "2.0",
        "storage": {"kind": "standalone", "root": "."},
        "contract": {
            "release": release,
            "archive_tag": "Gaia DR3" if release == "dr3" else "Gaia DR4",
            "retrieval_type": retrieval_type,
            "data_structure": "INDIVIDUAL",
            "output_format": "fits",
            "linking_parameter": "SOURCE_ID",
            "valid_data": False,
        },
        "items": items,
    }


def _write_corpus_manifest(
    corpus_dir: Path,
    *,
    storage_kind: str = "shared_library",
    active_run_id: str | None = None,
    snapshot_run_id: str | None = None,
) -> Path:
    corpus_dir.mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "schema_version": "2.0",
        "storage": {"kind": storage_kind},
    }
    if active_run_id is not None:
        payload["active_run_id"] = active_run_id
    if snapshot_run_id is not None:
        payload["snapshot_run_id"] = snapshot_run_id
    path = corpus_dir / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_resolve_product_uses_pinned_blob_not_stale_local_or_newest(tmp_path: Path) -> None:
    library_root = tmp_path / "_library"
    product_dir = tmp_path / "corpus" / "gaia_xp_sampled"
    pinned = _publish_blob(tmp_path, library_root, "pinned.fits", b"pinned-version")
    _publish_blob(tmp_path, library_root, "newest.fits", b"newer-unreferenced-version")
    product_dir.mkdir(parents=True)
    (product_dir / "123.fits").write_bytes(b"stale-local-copy")
    document = _product_document(
        product_dir,
        library_root,
        retrieval_type="XP_SAMPLED",
        items=[_item("123", pinned)],
    )
    refs_path = _write_refs(product_dir, document)

    resolved = resolve_product(product_dir, "123", "XP_SAMPLED")

    assert resolved["path"].read_bytes() == b"pinned-version"
    assert resolved["sha256"] == pinned["sha256"]
    assert resolved["version_id"] == "version-pinned"
    assert resolved["item_key"] == "item-123"
    assert resolved["storage_kind"] == "shared_library"
    assert not (library_root / "index.sqlite3").exists()

    # An authoritative failed refresh must not reveal its old artifact or the
    # stale legacy file beside the reference document.
    document["items"][0]["status"] = "refresh_failed"
    refs_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ProductReferenceError, match="refresh_failed"):
        resolve_product(product_dir, "123", "XP_SAMPLED")


def test_resolve_product_rejects_wrong_release_and_non_source_linking(tmp_path: Path) -> None:
    library_root = tmp_path / "_library"
    product_dir = tmp_path / "gaia_xp_continuous"
    blob = _publish_blob(tmp_path, library_root, "continuous.fits", b"continuous")
    wrong_release = _product_document(
        product_dir,
        library_root,
        retrieval_type="XP_CONTINUOUS",
        release="dr4",
        items=[_item("456", blob)],
    )
    refs = _write_refs(product_dir, wrong_release)

    with pytest.raises(ProductReferenceError, match="does not match"):
        resolve_product(product_dir, "456", "XP_CONTINUOUS", release="dr3")

    wrong_linking = _product_document(
        product_dir,
        library_root,
        retrieval_type="XP_CONTINUOUS",
        linking_parameter="TRANSIT_ID",
        items=[_item("456", blob)],
    )
    refs.write_text(json.dumps(wrong_linking), encoding="utf-8")
    with pytest.raises(ProductReferenceError, match="SOURCE_ID"):
        resolve_product(product_dir, "456", "XP_CONTINUOUS")


def test_collect_move_and_copy_refs_rebase_relative_library_root(tmp_path: Path) -> None:
    original_data = tmp_path / "original" / "data"
    corpus_dir = original_data / "new-label"
    product_dir = corpus_dir / "gaia_xp_sampled"
    library_root = original_data / "_library"
    blob = _publish_blob(tmp_path, library_root, "moved.fits", b"portable-blob")
    _write_refs(
        product_dir,
        _product_document(
            product_dir,
            library_root,
            retrieval_type="XP_SAMPLED",
            items=[_item("789", blob, version_id="version-before-move")],
        ),
    )

    aggregate_path = collect_corpus_refs(
        corpus_dir,
        run_id="run-1",
        release="dr3",
    )
    aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    assert aggregate["kind"] == "corpus_product_refs"
    assert aggregate["run_id"] == "run-1"
    assert aggregate["products"][0]["storage"]["relative_library_root"] == os.path.relpath(
        library_root, corpus_dir
    )
    assert list(corpus_dir.rglob("*.fits")) == []

    moved_data = tmp_path / "moved" / "data"
    moved_data.parent.mkdir(parents=True)
    shutil.move(str(original_data), str(moved_data))
    moved_corpus = moved_data / "new-label"

    resolved = resolve_product(moved_corpus, "789", "XP_SAMPLED")
    assert resolved["path"].read_bytes() == b"portable-blob"
    assert resolved["version_id"] == "version-before-move"

    copied_refs = tmp_path / "views" / "portable-product-refs.json"
    copy_reference_file(moved_corpus / "product_refs.json", copied_refs)
    copied_payload = json.loads(copied_refs.read_text(encoding="utf-8"))
    expected_relative = os.path.relpath(moved_data / "_library", copied_refs.parent)
    assert copied_payload["products"][0]["storage"]["relative_library_root"] == expected_relative
    assert resolve_product(copied_refs, "789", "XP_SAMPLED")["path"].read_bytes() == b"portable-blob"

    documents = read_reference_documents(copied_refs)
    assert len(documents) == 1
    assert documents[0]["contract"]["retrieval_type"] == "XP_SAMPLED"


def test_count_cached_products_verifies_ready_items_and_never_uses_stale_files(
    tmp_path: Path,
) -> None:
    library_root = tmp_path / "_library"
    product_dir = tmp_path / "corpus" / "gaia_xp_sampled"
    ready_blob = _publish_blob(tmp_path, library_root, "ready.fits", b"ready")
    corrupt_blob = _publish_blob(tmp_path, library_root, "corrupt.fits", b"before-corruption")
    corrupt_path = BlobStore(library_root).path(corrupt_blob)
    corrupt_path.chmod(0o644)
    corrupt_path.write_bytes(b"corrupt")
    document = _product_document(
        product_dir,
        library_root,
        retrieval_type="XP_SAMPLED",
        items=[
            _item("1", ready_blob),
            _item("2", corrupt_blob),
            _item("3", ready_blob, status="absent", version_id=""),
        ],
    )
    _write_refs(product_dir, document)
    (product_dir / "2.fits").write_bytes(b"stale-two")
    (product_dir / "3.fits").write_bytes(b"stale-three")

    assert count_cached_products(product_dir, ["1", "2", "3", "4"]) == 1


def test_legacy_directory_fallback_is_dr3_only(tmp_path: Path) -> None:
    product_dir = tmp_path / "gaia_xp_sampled"
    product_dir.mkdir()
    legacy = product_dir / "42.fits"
    legacy.write_bytes(b"legacy")

    resolved = resolve_product(product_dir, "42", "XP_SAMPLED")

    assert resolved["path"] == legacy
    assert resolved["version_id"] is None
    assert resolved["storage_kind"] == "legacy_local"
    assert count_cached_products(product_dir, ["42", "43"]) == 1
    with pytest.raises(ProductReferenceError, match="release provenance"):
        resolve_product(product_dir, "42", "XP_SAMPLED", release="dr4")


def test_resolve_standalone_aggregate_survives_complete_export_move(
    tmp_path: Path,
) -> None:
    export_root = tmp_path / "original" / "export"
    product_dir = export_root / "gaia_xp_sampled"
    product_dir.mkdir(parents=True)
    artifact = product_dir / "123.fits"
    artifact.write_bytes(b"standalone-pinned-product")
    blob = _standalone_blob(artifact, "gaia_xp_sampled/123.fits")
    product = _standalone_product_document(
        retrieval_type="XP_SAMPLED",
        items=[_item("123", blob, version_id="standalone-version")],
    )
    aggregate = {
        "schema_version": "2.0",
        "kind": "corpus_product_refs",
        "gaia_release": "dr3",
        "run_id": "export-run",
        "products": [product],
    }
    (export_root / "product_refs.json").write_text(
        json.dumps(aggregate),
        encoding="utf-8",
    )

    resolved = resolve_product(export_root, "123", "XP_SAMPLED")

    assert resolved["path"] == artifact.resolve()
    assert resolved["path"].read_bytes() == b"standalone-pinned-product"
    assert resolved["sha256"] == blob["sha256"]
    assert resolved["version_id"] == "standalone-version"
    assert resolved["item_key"] == "item-123"
    assert resolved["storage_kind"] == "standalone"
    assert not (export_root / "index.sqlite3").exists()

    moved_root = tmp_path / "moved" / "export"
    moved_root.parent.mkdir(parents=True)
    shutil.move(str(export_root), str(moved_root))

    moved = resolve_product(moved_root, "123", "XP_SAMPLED")
    assert moved["path"] == (moved_root / "gaia_xp_sampled" / "123.fits").resolve()
    assert moved["path"].read_bytes() == b"standalone-pinned-product"


def test_collect_corpus_refs_rebases_nested_standalone_artifacts(
    tmp_path: Path,
) -> None:
    corpus_dir = tmp_path / "export"
    product_dir = corpus_dir / "gaia_xp_sampled"
    product_dir.mkdir(parents=True)
    artifact = product_dir / "456.fits"
    artifact.write_bytes(b"nested-standalone")
    blob = _standalone_blob(artifact, "456.fits")
    _write_refs(
        product_dir,
        _standalone_product_document(
            retrieval_type="XP_SAMPLED",
            items=[_item("456", blob)],
        ),
    )

    aggregate_path = collect_corpus_refs(corpus_dir, release="dr3")
    aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))

    exported_blob = aggregate["products"][0]["items"][0]["artifacts"][0]["blob"]
    assert exported_blob["relative_path"] == "gaia_xp_sampled/456.fits"
    assert resolve_product(corpus_dir, "456", "XP_SAMPLED")["path"] == artifact.resolve()


def test_standalone_integrity_failure_never_falls_back_to_legacy(
    tmp_path: Path,
) -> None:
    product_dir = tmp_path / "gaia_xp_sampled"
    product_dir.mkdir()
    artifact = product_dir / "789.fits"
    artifact.write_bytes(b"expected-bytes")
    blob = _standalone_blob(artifact, "789.fits")
    _write_refs(
        product_dir,
        _standalone_product_document(
            retrieval_type="XP_SAMPLED",
            items=[_item("789", blob)],
        ),
    )
    resolved = resolve_product(product_dir, "789", "XP_SAMPLED")
    assert resolved["path"] == artifact.resolve()
    assert resolved["storage_kind"] == "standalone"

    artifact.write_bytes(b"tampered-bytes")

    with pytest.raises(BlobIntegrityError, match="checksum mismatch"):
        resolve_product(product_dir, "789", "XP_SAMPLED")

    artifact.write_bytes(b"short")
    with pytest.raises(BlobIntegrityError, match="size mismatch"):
        resolve_product(product_dir, "789", "XP_SAMPLED")


@pytest.mark.parametrize("relative_path", ["../outside.fits", "/tmp/outside.fits"])
def test_standalone_artifact_paths_cannot_escape_reference_directory(
    tmp_path: Path,
    relative_path: str,
) -> None:
    product_dir = tmp_path / "gaia_xp_sampled"
    product_dir.mkdir()
    outside = tmp_path / "outside.fits"
    outside.write_bytes(b"outside")
    blob = _standalone_blob(outside, relative_path)
    _write_refs(
        product_dir,
        _standalone_product_document(
            retrieval_type="XP_SAMPLED",
            items=[_item("999", blob)],
        ),
    )

    with pytest.raises(BlobIntegrityError, match="confined"):
        resolve_product(product_dir, "999", "XP_SAMPLED")


def test_shared_corpus_readers_follow_active_run_not_promoted_refs(
    tmp_path: Path,
) -> None:
    library_root = tmp_path / "data" / "_library"
    corpus_dir = tmp_path / "data" / "corpus"
    active_run = corpus_dir / "runs" / "run-active"
    active_product = active_run / "gaia_xp_sampled"
    promoted_product = corpus_dir / "gaia_xp_sampled"
    active_blob = _publish_blob(tmp_path, library_root, "active.fits", b"active")
    promoted_blob = _publish_blob(tmp_path, library_root, "promoted.fits", b"promoted")

    _write_refs(
        active_product,
        _product_document(
            active_product,
            library_root,
            retrieval_type="XP_SAMPLED",
            items=[_item("101", active_blob, version_id="active-version")],
        ),
    )
    collect_corpus_refs(active_run, run_id="run-active", release="dr3")
    _write_corpus_manifest(
        active_run,
        active_run_id="run-active",
        snapshot_run_id="run-active",
    )

    _write_refs(
        promoted_product,
        _product_document(
            promoted_product,
            library_root,
            retrieval_type="XP_SAMPLED",
            items=[_item("202", promoted_blob, version_id="half-promoted-version")],
        ),
    )
    collect_corpus_refs(corpus_dir, run_id="run-pending", release="dr3")
    _write_corpus_manifest(corpus_dir, active_run_id="run-active")

    assert active_corpus_dir(corpus_dir) == active_run.resolve()
    assert active_corpus_dir(promoted_product) == active_product.resolve()
    assert active_corpus_dir(corpus_dir / "product_refs.json") == (
        active_run / "product_refs.json"
    ).resolve()
    assert resolve_product(corpus_dir, "101", "XP_SAMPLED")["version_id"] == (
        "active-version"
    )
    assert resolve_product(promoted_product, "101", "XP_SAMPLED")["version_id"] == (
        "active-version"
    )
    assert resolve_product(
        corpus_dir / "product_refs.json",
        "101",
        "XP_SAMPLED",
    )["version_id"] == "active-version"
    documents = read_reference_documents(corpus_dir)
    assert documents[0]["items"][0]["version_id"] == "active-version"
    assert count_cached_products(promoted_product, ["101", "202"]) == 1
    with pytest.raises(ProductReferenceError, match="no item 202"):
        resolve_product(corpus_dir, "202", "XP_SAMPLED")


@pytest.mark.parametrize(
    "active_run_id",
    [None, "", ".", "../escape", "nested/run", "windows\\escape", "missing"],
)
def test_shared_corpus_without_safe_published_run_fails_closed(
    tmp_path: Path,
    active_run_id: str | None,
) -> None:
    corpus_dir = tmp_path / "corpus"
    _write_corpus_manifest(corpus_dir, active_run_id=active_run_id)

    with pytest.raises(ProductReferenceError):
        active_corpus_dir(corpus_dir)
    with pytest.raises(ProductReferenceError):
        read_reference_documents(corpus_dir)
    with pytest.raises(ProductReferenceError):
        resolve_product(corpus_dir, "1", "XP_SAMPLED")
    with pytest.raises(ProductReferenceError):
        count_cached_products(corpus_dir / "gaia_xp_sampled", ["1"])


def test_snapshot_standalone_and_pending_run_readers_stay_pinned(
    tmp_path: Path,
) -> None:
    library_root = tmp_path / "data" / "_library"
    corpus_dir = tmp_path / "data" / "corpus"
    active_run = corpus_dir / "runs" / "active-run"
    active_run.mkdir(parents=True)
    _write_corpus_manifest(corpus_dir, active_run_id="active-run")

    pending_run = corpus_dir / "runs" / "pending-run"
    pending_product = pending_run / "gaia_xp_sampled"
    pending_blob = _publish_blob(tmp_path, library_root, "pending.fits", b"pending")
    _write_refs(
        pending_product,
        _product_document(
            pending_product,
            library_root,
            retrieval_type="XP_SAMPLED",
            items=[_item("301", pending_blob, version_id="pending-version")],
        ),
    )
    collect_corpus_refs(pending_run, run_id="pending-run", release="dr3")
    assert active_corpus_dir(pending_run) == pending_run
    assert resolve_product(pending_run, "301", "XP_SAMPLED")["version_id"] == (
        "pending-version"
    )

    snapshot_run = corpus_dir / "runs" / "snapshot-run"
    snapshot_product = snapshot_run / "gaia_xp_sampled"
    snapshot_blob = _publish_blob(tmp_path, library_root, "snapshot.fits", b"snapshot")
    _write_refs(
        snapshot_product,
        _product_document(
            snapshot_product,
            library_root,
            retrieval_type="XP_SAMPLED",
            items=[_item("302", snapshot_blob, version_id="snapshot-version")],
        ),
    )
    collect_corpus_refs(snapshot_run, run_id="snapshot-run", release="dr3")
    _write_corpus_manifest(
        snapshot_run,
        active_run_id="active-run",
        snapshot_run_id="snapshot-run",
    )
    assert active_corpus_dir(snapshot_run) == snapshot_run
    assert resolve_product(snapshot_run, "302", "XP_SAMPLED")["version_id"] == (
        "snapshot-version"
    )

    standalone_root = tmp_path / "standalone"
    standalone_product = standalone_root / "gaia_xp_sampled"
    standalone_product.mkdir(parents=True)
    standalone_artifact = standalone_product / "303.fits"
    standalone_artifact.write_bytes(b"standalone")
    standalone_blob = _standalone_blob(
        standalone_artifact,
        "gaia_xp_sampled/303.fits",
    )
    standalone_document = _standalone_product_document(
        retrieval_type="XP_SAMPLED",
        items=[_item("303", standalone_blob, version_id="standalone-version")],
    )
    (standalone_root / "product_refs.json").write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "kind": "corpus_product_refs",
                "gaia_release": "dr3",
                "run_id": "standalone-run",
                "products": [standalone_document],
            }
        ),
        encoding="utf-8",
    )
    _write_corpus_manifest(standalone_root, storage_kind="standalone")
    assert active_corpus_dir(standalone_root) == standalone_root
    assert resolve_product(standalone_root, "303", "XP_SAMPLED")["version_id"] == (
        "standalone-version"
    )
