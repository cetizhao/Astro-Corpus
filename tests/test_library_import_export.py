"""Tests for non-destructive library import, verification, and export."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pandas as pd
import pytest

from astro_pipeline.library.identity import product_item_key
from astro_pipeline.library.import_export import (
    export_corpus,
    import_corpus,
    plan_import,
    reconcile_library,
    verify_library,
)
from astro_pipeline.library.store import LibraryStore
from astro_pipeline.spectra import fetch_xp_spectrum


def _file_state(root: Path) -> dict[str, tuple[int, int, str]]:
    from astro_pipeline.library.files import sha256_file

    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_mtime_ns,
            path.stat().st_size,
            sha256_file(path),
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def _legacy_fixture(root: Path, *, release: str | None = "dr3") -> None:
    root.mkdir()
    manifest = {"corpus": {"label": root.name}}
    if release is not None:
        manifest["corpus"]["gaia_release"] = release
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "members.csv").write_text(
        "source_id,ra,member_name\n"
        "1234567890123456789,1.5,literature row\n"
        "1234567890123456789,1.5,duplicate row\n",
        encoding="utf-8",
    )
    pd.DataFrame(
        [{"source_id": "1234567890123456789", "bibcode": "2026A&A...1A"}]
    ).to_parquet(root / "paper_edges.parquet", index=False)
    (root / "notes.bin").write_bytes(b"unknown evidence")


def _legacy_product_fixture(
    root: Path,
    *,
    identifier: str = "1234567890123456789",
    normalized_identifier: str | None = None,
    archive_identifiers: tuple[str, ...] | None = None,
    content_release: str | None = None,
    complete_contract: bool = True,
    corrupt_checksum: bool = False,
    unparseable: bool = False,
) -> dict:
    from astropy.table import Table

    from astro_pipeline.library.files import sha256_file

    product_dir = root / "gaia_xp_sampled"
    archive_dir = product_dir / "archive_tables"
    archive_dir.mkdir(parents=True)
    normalized = product_dir / f"{identifier}.fits"
    archive = archive_dir / "batch-00000.fits"
    table_metadata = {"RELEASE": content_release} if content_release else {}
    normalized_identifier = normalized_identifier or identifier
    archive_identifiers = archive_identifiers or (identifier,)
    if unparseable:
        normalized.write_bytes(b"not-a-table")
    else:
        Table(
            {
                "source_id": [int(normalized_identifier)],
                "flux": [1.0],
            },
            meta=table_metadata,
        ).write(normalized, format="fits")
    Table(
        {
            "source_id": [int(value) for value in archive_identifiers],
            "flux": [float(index + 1) for index in range(len(archive_identifiers))],
        },
        meta=table_metadata,
    ).write(archive, format="fits")
    contract = {
        "release": "dr3",
        "archive_tag": "Gaia DR3",
        "retrieval_type": "XP_SAMPLED",
        "data_structure": "INDIVIDUAL",
        "output_format": "fits",
        "linking_parameter": "SOURCE_ID",
        "valid_data": False,
    }
    if not complete_contract:
        contract.pop("archive_tag")
    (product_dir / "product_request.json").write_text(
        json.dumps({"schema_version": "1.0", "contract": contract}),
        encoding="utf-8",
    )
    artifact = {
        "release": "dr3",
        "archive_tag": "Gaia DR3",
        "retrieval_type": "XP_SAMPLED",
        "linking_parameter": "SOURCE_ID",
        "identifier": identifier,
        "output_format": "fits",
        "data_structure": "INDIVIDUAL",
        "normalized_path": str(normalized.resolve()),
        "archive_table_path": str(archive.resolve()),
        "checksum_sha256": "0" * 64 if corrupt_checksum else sha256_file(normalized),
        "archive_checksum_sha256": sha256_file(archive),
        "content_type": "application/fits",
        "table_key": "XP_SAMPLED",
        "metadata": {"legacy": True},
        "success": True,
        "error": None,
    }
    (product_dir / "artifacts.json").write_text(
        json.dumps({"schema_version": "1.0", "artifacts": [artifact]}),
        encoding="utf-8",
    )
    return {"contract": contract, "normalized": normalized, "archive": archive}


def _published_corpus(store: LibraryStore, source: Path, label: str = "sample") -> tuple[str, str]:
    corpus_id = store.register_corpus(source, label)
    run_id = store.start_run(corpus_id)
    store.index_members(
        run_id,
        [{"source_id": "123", "gaia_release": "dr3", "ra": 1.0}],
    )
    store.finish_run(
        run_id,
        "complete",
        metadata={"members_indexed": True, "published": True},
    )
    children = list(source.iterdir())
    run_dir = source / "runs" / run_id
    run_dir.mkdir(parents=True)
    for child in children:
        target = run_dir / child.name
        if child.is_dir() and not child.is_symlink():
            shutil.copytree(child, target, symlinks=True)
        elif child.is_symlink():
            target.symlink_to(os.readlink(child))
        else:
            shutil.copy2(child, target)
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "snapshot_run_id": run_id,
                "storage": {"kind": "shared_library"},
            }
        ),
        encoding="utf-8",
    )
    return corpus_id, run_id


def test_legacy_dry_run_is_hash_complete_and_writes_nothing(tmp_path):
    source = tmp_path / "legacy"
    _legacy_fixture(source)
    before = _file_state(source)
    library_root = tmp_path / "library"

    plan = import_corpus(LibraryStore(library_root), source)

    assert plan["mode"] == "dry_run"
    assert plan["writes_library"] is False
    assert plan["modifies_source"] is False
    assert plan["release"] == "dr3"
    assert plan["can_index_members"] is True
    assert plan["n_member_rows"] == 2
    assert plan["n_inventory_files"] == 4
    assert all(
        item.get("sha256")
        for item in plan["inventory"]
        if item["kind"] == "file"
    )
    assert _file_state(source) == before
    assert not library_root.exists()


def test_library_cli_defaults_to_read_only_and_requires_explicit_apply(tmp_path):
    source = tmp_path / "legacy"
    _legacy_fixture(source)
    library_root = tmp_path / "library"
    script = Path(__file__).parents[1] / "scripts" / "library.py"

    status_process = subprocess.run(
        [
            sys.executable,
            str(script),
            "--library-root",
            str(library_root),
            "status",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert status_process.returncode == 0
    assert json.loads(status_process.stdout)["exists"] is False
    assert not library_root.exists()

    dry_process = subprocess.run(
        [
            sys.executable,
            str(script),
            "--library-root",
            str(library_root),
            "import",
            str(source),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert dry_process.returncode == 0
    assert json.loads(dry_process.stdout)["mode"] == "dry_run"
    assert not library_root.exists()

    apply_process = subprocess.run(
        [
            sys.executable,
            str(script),
            "--library-root",
            str(library_root),
            "import",
            str(source),
            "--apply",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert apply_process.returncode == 0
    assert json.loads(apply_process.stdout)["writes_library"] is True
    assert library_root.is_dir()

    verify_process = subprocess.run(
        [
            sys.executable,
            str(script),
            "--library-root",
            str(library_root),
            "verify",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert verify_process.returncode == 0
    assert json.loads(verify_process.stdout)["ok"] is True


def test_legacy_apply_is_unverified_idempotent_and_preserves_source(tmp_path):
    source = tmp_path / "legacy"
    _legacy_fixture(source)
    before = _file_state(source)
    store = LibraryStore(tmp_path / "library")

    first = import_corpus(store, source, apply=True)
    second = import_corpus(store, source, apply=True)

    assert first["writes_library"] is True
    assert first["members_indexed"] is True
    assert first["n_input_rows"] == 2
    assert first["n_sample_members"] == 2
    assert first["n_unique_sources"] == 1
    assert second["idempotent"] is True
    assert second["run_id"] == first["run_id"]
    assert len(store.read("SELECT * FROM corpora")) == 1
    assert len(store.read("SELECT * FROM runs")) == 1
    assert len(store.read("SELECT * FROM sample_members")) == 2
    assert store.read("SELECT request_id FROM runs") == [{"request_id": None}]

    source_record = store.source("dr3", "1234567890123456789")
    assert source_record["attributes"]["ra"] == 1.5
    assert "member_name" not in source_record["attributes"]
    assert source_record["covered_fields"] == []
    assert store.read("SELECT * FROM source_ref_current") == []
    metadata = json.loads(store.read("SELECT metadata_json FROM runs")[0]["metadata_json"])
    assert metadata["legacy_unverified"] is True
    assert metadata["published"] is True
    assert metadata["paper_evidence_only"] is True
    assert metadata["managed_artifacts"]
    assert _file_state(source) == before
    assert verify_library(store)["ok"] is True


def test_verified_legacy_products_are_promoted_without_replacing_active_bytes(tmp_path):
    source = tmp_path / "legacy"
    _legacy_fixture(source)
    product = _legacy_product_fixture(source)
    before = _file_state(source)
    store = LibraryStore(tmp_path / "library")

    plan = plan_import(source)
    assert len(plan["legacy_products"]["promotable"]) == 1
    assert plan["legacy_products"]["inventory_only"] == []
    assert not store.root.exists()

    result = import_corpus(store, source, apply=True)
    registered = result["legacy_products"]["registered"]
    assert len(registered) == 1
    assert result["legacy_products"]["reused"] == []
    key = product_item_key(product["contract"], "1234567890123456789")
    assert registered[0]["item_key"] == key
    saved = store.get_product(key)
    assert saved is not None
    assert saved["version_id"] == registered[0]["version_id"]
    assert {(item["role"], item["slot"]) for item in saved["artifacts"]} == {
        ("normalized", "XP_SAMPLED"),
        ("archive", "XP_SAMPLED"),
    }
    assert saved["metadata"]["legacy_import"] is True
    assert _file_state(source) == before

    repeated = tmp_path / "repeated"
    _legacy_fixture(repeated)
    _legacy_product_fixture(repeated)
    reused = import_corpus(store, repeated, apply=True)
    assert reused["legacy_products"]["registered"] == []
    assert reused["legacy_products"]["reused"] == [
        {
            "item_key": key,
            "version_id": saved["version_id"],
            "identifier": "1234567890123456789",
        }
    ]

    # A different complete legacy snapshot must not replace active immutable bytes.
    competing = tmp_path / "competing"
    _legacy_fixture(competing)
    _legacy_product_fixture(competing)
    from astropy.table import Table

    competing_normalized = (
        competing / "gaia_xp_sampled" / "1234567890123456789.fits"
    )
    Table(
        {"source_id": [1234567890123456789], "flux": [2.0]}
    ).write(competing_normalized, format="fits", overwrite=True)
    from astro_pipeline.library.files import sha256_file

    artifacts_path = competing / "gaia_xp_sampled" / "artifacts.json"
    artifacts = json.loads(artifacts_path.read_text(encoding="utf-8"))
    artifacts["artifacts"][0]["checksum_sha256"] = sha256_file(
        competing_normalized
    )
    artifacts_path.write_text(json.dumps(artifacts), encoding="utf-8")

    collision = import_corpus(store, competing, apply=True)
    assert collision["legacy_products"]["registered"] == []
    assert "different active product" in collision["legacy_products"]["skipped"][0][
        "reason"
    ]
    assert store.get_product(key)["version_id"] == saved["version_id"]


def test_incomplete_or_corrupt_legacy_products_remain_inventory_only(tmp_path):
    incomplete = tmp_path / "incomplete"
    _legacy_fixture(incomplete)
    _legacy_product_fixture(incomplete, complete_contract=False)
    corrupt = tmp_path / "corrupt"
    _legacy_fixture(corrupt)
    _legacy_product_fixture(corrupt, corrupt_checksum=True)
    unparseable = tmp_path / "unparseable"
    _legacy_fixture(unparseable)
    _legacy_product_fixture(unparseable, unparseable=True)

    incomplete_plan = plan_import(incomplete)
    corrupt_plan = plan_import(corrupt)
    unparseable_plan = plan_import(unparseable)
    assert incomplete_plan["legacy_products"]["promotable"] == []
    assert "incomplete product contract" in incomplete_plan["legacy_products"][
        "inventory_only"
    ][0]["reason"]
    assert corrupt_plan["legacy_products"]["promotable"] == []
    assert "unverifiable normalized" in corrupt_plan["legacy_products"][
        "inventory_only"
    ][0]["reason"]
    assert unparseable_plan["legacy_products"]["promotable"] == []
    assert "unsupported or unparseable" in unparseable_plan["legacy_products"][
        "inventory_only"
    ][0]["reason"]

    store = LibraryStore(tmp_path / "library")
    result = import_corpus(store, corrupt, apply=True)
    assert result["legacy_products"]["registered"] == []
    assert store.status()["n_ready_products"] == 0


def test_legacy_product_content_identity_and_release_contradictions_are_rejected(
    tmp_path,
):
    mismatched = tmp_path / "mismatched"
    _legacy_fixture(mismatched)
    _legacy_product_fixture(mismatched, normalized_identifier="200")

    archive_mismatch = tmp_path / "archive-mismatch"
    _legacy_fixture(archive_mismatch)
    _legacy_product_fixture(archive_mismatch, archive_identifiers=("200",))

    wrong_release = tmp_path / "wrong-release"
    _legacy_fixture(wrong_release)
    _legacy_product_fixture(wrong_release, content_release="dr4")

    plans = {
        "normalized": plan_import(mismatched),
        "archive": plan_import(archive_mismatch),
        "release": plan_import(wrong_release),
    }
    assert all(
        plan["legacy_products"]["promotable"] == [] for plan in plans.values()
    )
    assert "not only" in plans["normalized"]["legacy_products"]["inventory_only"][
        0
    ]["reason"]
    assert "does not contain" in plans["archive"]["legacy_products"][
        "inventory_only"
    ][0]["reason"]
    assert "conflicts with contract release" in plans["release"][
        "legacy_products"
    ]["inventory_only"][0]["reason"]

    store = LibraryStore(tmp_path / "library")
    for source in (mismatched, archive_mismatch, wrong_release):
        result = import_corpus(store, source, apply=True)
        assert result["legacy_products"]["registered"] == []
    assert store.status()["n_ready_products"] == 0


def test_import_release_is_never_guessed_and_conflicts_are_safe(tmp_path):
    unknown = tmp_path / "unknown"
    _legacy_fixture(unknown, release=None)
    _legacy_product_fixture(unknown)
    store = LibraryStore(tmp_path / "library")

    dry = plan_import(unknown)
    assert dry["release"] is None
    assert dry["can_index_members"] is False
    assert dry["legacy_products"]["promotable"] == []
    assert "cannot supply" in dry["legacy_products"]["inventory_only"][0]["reason"]
    assert not store.root.exists()

    inventory_only = import_corpus(store, unknown, apply=True)
    assert inventory_only["members_indexed"] is False
    assert inventory_only["legacy_products"]["registered"] == []
    assert store.status()["n_sources"] == 0
    assert store.status()["n_ready_products"] == 0

    conflict_source = tmp_path / "conflict"
    _legacy_fixture(conflict_source, release="dr3")
    conflict_plan = plan_import(conflict_source, release="dr4")
    assert conflict_plan["errors"]
    with pytest.raises(ValueError, match="conflicts"):
        import_corpus(store, conflict_source, apply=True, release="dr4")
    assert store.read(
        "SELECT COUNT(*) AS n FROM corpora WHERE canonical_path = ?",
        (str(conflict_source.resolve()),),
    ) == [{"n": 0}]


def test_optional_external_catalog_is_managed_and_release_explicit(tmp_path):
    source = tmp_path / "twa"
    source.mkdir()
    (source / "paper_edges.parquet").write_bytes(b"legacy-only")
    catalog = tmp_path / "TWAxgdr3.csv"
    catalog.write_text("Gaia,ra\n123,1.0\n", encoding="utf-8")
    store = LibraryStore(tmp_path / "library")

    result = import_corpus(
        store,
        source,
        apply=True,
        catalog=catalog,
        release="dr3",
    )
    assert result["members_indexed"] is True
    assert store.source("dr3", "123") is not None
    metadata = json.loads(
        store.read("SELECT metadata_json FROM runs WHERE run_id = ?", (result["run_id"],))[0][
            "metadata_json"
        ]
    )
    assert any(
        artifact["source_relative_path"] == "@catalog/TWAxgdr3.csv"
        for artifact in metadata["managed_artifacts"]
    )


def test_reconcile_only_materializes_verified_missing_views(tmp_path):
    from astro_pipeline.library.files import sha256_file

    store = LibraryStore(tmp_path / "library")

    verified_corpus = tmp_path / "verified"
    verified_corpus.mkdir()
    verified_id = store.register_corpus(
        verified_corpus, "verified", provenance_status="verified"
    )
    verified_run = store.start_run(verified_id)
    verified_snapshot = verified_corpus / "runs" / verified_run / "members.csv"
    verified_snapshot.parent.mkdir(parents=True)
    verified_snapshot.write_text("source_id\n123\n", encoding="utf-8")
    store.record_run_artifact(
        verified_run,
        "members_csv",
        str(verified_snapshot),
        sha256_file(verified_snapshot),
    )
    nested_reference = (
        verified_corpus
        / "runs"
        / verified_run
        / "gaia_xp_sampled"
        / "product_refs.json"
    )
    nested_reference.parent.mkdir()
    nested_reference.write_text("{}\n", encoding="utf-8")
    store.record_run_artifact(
        verified_run,
        "gaia_xp_sampled/product_refs.json",
        str(nested_reference),
        sha256_file(nested_reference),
    )
    store.finish_run(
        verified_run,
        "complete",
        metadata={"members_indexed": True, "published": True},
    )

    legacy_corpus = tmp_path / "legacy-unverified"
    legacy_corpus.mkdir()
    legacy_id = store.register_corpus(
        legacy_corpus, "legacy-unverified", provenance_status="legacy_unverified"
    )
    legacy_run = store.start_run(legacy_id)
    legacy_snapshot = legacy_corpus / "runs" / legacy_run / "members.csv"
    legacy_snapshot.parent.mkdir(parents=True)
    legacy_snapshot.write_text("source_id\n999\n", encoding="utf-8")
    store.record_run_artifact(
        legacy_run,
        "members_csv",
        str(legacy_snapshot),
        sha256_file(legacy_snapshot),
    )
    store.finish_run(
        legacy_run,
        "complete",
        metadata={
            "members_indexed": True,
            "published": True,
            "legacy_unverified": True,
        },
    )

    planned = reconcile_library(store)
    assert planned["writes"] == 0
    assert len(planned["actions"]) == 2
    assert {action["run_id"] for action in planned["actions"]} == {verified_run}
    assert any(
        item["run_id"] == legacy_run
        and item["reason"] == "unverified_run_provenance"
        for item in planned["skipped"]
    )
    assert not (verified_corpus / "members.csv").exists()
    assert not (
        verified_corpus / "gaia_xp_sampled" / "product_refs.json"
    ).exists()
    assert not (legacy_corpus / "members.csv").exists()

    applied = reconcile_library(store, apply=True)
    assert applied["writes"] == 2
    assert (verified_corpus / "members.csv").read_text(encoding="utf-8") == (
        "source_id\n123\n"
    )
    assert (
        verified_corpus / "gaia_xp_sampled" / "product_refs.json"
    ).read_text(encoding="utf-8") == "{}\n"
    assert not (legacy_corpus / "members.csv").exists()


def test_verify_reports_missing_corrupt_orphan_and_staging(tmp_path):
    residue_store = LibraryStore(tmp_path / "residue-only")
    residue_object = residue_store.blobs.objects / "aa" / ("a" * 64 + ".bin")
    residue_object.parent.mkdir(parents=True)
    residue_object.write_bytes(b"unregistered")
    residue_store.blobs.staging.mkdir(parents=True)
    (residue_store.blobs.staging / "partial").write_bytes(b"partial")
    residue_result = verify_library(residue_store)
    assert residue_result["exists"] is False
    assert residue_result["clean"] is False
    assert residue_result["orphan_objects"] == [
        residue_object.relative_to(residue_store.root).as_posix()
    ]
    assert residue_result["staging_files"] == ["staging/partial"]
    assert not residue_store.db_path.exists()

    store = LibraryStore(tmp_path / "library")
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    first_blob = store.blobs.publish(first)
    second_blob = store.blobs.publish(second)
    store.register_blob(first_blob)
    store.register_blob(second_blob)

    first_path = store.blobs.path(first_blob)
    first_path.chmod(0o644)
    first_path.unlink()
    second_path = store.blobs.path(second_blob)
    second_path.chmod(0o644)
    second_path.write_bytes(b"damaged")
    orphan = store.blobs.objects / "ff" / ("f" * 64 + ".bin")
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"orphan")
    store.blobs.staging.mkdir(parents=True, exist_ok=True)
    (store.blobs.staging / "leftover.part").write_bytes(b"partial")

    result = verify_library(store)
    assert result["ok"] is False
    assert result["clean"] is False
    assert [item["sha256"] for item in result["missing_blobs"]] == [first_blob.sha256]
    assert [item["sha256"] for item in result["corrupt_blobs"]] == [second_blob.sha256]
    assert len(result["orphan_objects"]) == 1
    assert result["staging_files"] == ["staging/leftover.part"]
    assert result["foreign_key_violations"] == []


@pytest.mark.parametrize("publication_error", [None, "late view update failed"])
def test_verify_reports_pending_publication_without_automatic_recovery(
    tmp_path, publication_error
):
    from astro_pipeline.library.files import sha256_file

    store = LibraryStore(tmp_path / "library")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    corpus_id = store.register_corpus(
        corpus, "corpus", provenance_status="verified"
    )

    active_run = store.start_run(corpus_id)
    active_snapshot = corpus / "runs" / active_run / "members.csv"
    active_snapshot.parent.mkdir(parents=True)
    active_snapshot.write_text("source_id\nactive\n", encoding="utf-8")
    store.record_run_artifact(
        active_run,
        "members_csv",
        str(active_snapshot),
        sha256_file(active_snapshot),
    )
    store.finish_run(
        active_run,
        "completed",
        metadata={"members_indexed": True, "published": True},
    )

    pending_run = store.start_run(corpus_id)
    pending_dir = corpus / "runs" / pending_run
    pending_dir.mkdir(parents=True)
    pending_snapshot = pending_dir / "members.csv"
    pending_snapshot.write_text("source_id\npending\n", encoding="utf-8")
    store.record_run_artifact(
        pending_run,
        "members_csv",
        str(pending_snapshot),
        sha256_file(pending_snapshot),
    )
    pending_metadata = {
        "members_indexed": True,
        "prepared": True,
        "published": False,
        "view_pending": True,
        "snapshot_path": str(pending_dir),
    }
    if publication_error is not None:
        pending_metadata["publication_error"] = publication_error
    store.finish_run(pending_run, "completed", metadata=pending_metadata)
    (corpus / "manifest.json").write_text(
        json.dumps(
            {
                "active_run_id": active_run,
                "current_run_id": pending_run,
                "storage": {"kind": "shared_library"},
            }
        ),
        encoding="utf-8",
    )

    verification = verify_library(store)
    assert verification["ok"] is False
    assert verification["clean"] is False
    assert verification["pending_publication_recovery"] == "diagnostic_only"
    assert len(verification["pending_publications"]) == 1
    diagnostic = verification["pending_publications"][0]
    assert diagnostic["run_id"] == pending_run
    assert diagnostic["corpus_id"] == corpus_id
    assert diagnostic["status"] == "completed"
    assert diagnostic["prepared"] is True
    assert diagnostic["published"] is False
    assert diagnostic["view_pending"] is True
    assert diagnostic["snapshot_path"] == str(pending_dir)
    assert diagnostic["publication_error"] == publication_error
    assert diagnostic["recovery"] == "diagnostic_only"

    reconciled = reconcile_library(store, apply=True)
    assert reconciled["writes"] == 0
    assert reconciled["actions"] == []
    assert reconciled["pending_publications"] == verification["pending_publications"]
    assert reconciled["pending_publication_recovery"] == "diagnostic_only"
    assert reconciled["skipped"] == [
        {
            "run_id": active_run,
            "corpus_id": corpus_id,
            "pending_run_ids": [pending_run],
            "reason": "corpus_has_pending_publication",
        }
    ]
    assert not (corpus / "members.csv").exists()
    root_manifest = json.loads(
        (corpus / "manifest.json").read_text(encoding="utf-8")
    )
    assert root_manifest["active_run_id"] == active_run
    assert root_manifest["current_run_id"] == pending_run


def _product_reference_fixture(store: LibraryStore, source: Path) -> tuple[dict, Path]:
    payload = source.parent / "xp.fits"
    payload.write_bytes(b"standalone spectrum bytes")
    blob = store.blobs.publish(payload, content_type="application/fits")
    contract = {
        "release": "dr3",
        "retrieval_type": "XP_SAMPLED",
        "data_structure": "INDIVIDUAL",
        "output_format": "fits",
        "linking_parameter": "SOURCE_ID",
        "valid_data": False,
    }
    item_key = product_item_key(contract, "123")
    lease = store.acquire_lease("product", item_key)
    assert lease is not None
    version = store.save_product(
        contract,
        "123",
        [{"blob": blob, "role": "normalized", "slot": "spectrum", "metadata": {}}],
        lease,
    )
    store.release_lease(lease)
    document = {
        "schema_version": "2.0",
        "storage": {
            "kind": "shared_library",
            "library_root": str(store.root),
            "relative_library_root": os.path.relpath(store.root, source),
            "index_path": "index.sqlite3",
        },
        "contract": contract,
        "items": [
            {
                "identifier": "123",
                "item_key": item_key,
                "status": "ready",
                "version_id": version["version_id"],
                "artifacts": version["artifacts"],
                "error": None,
            },
            {
                "identifier": "999",
                "item_key": "failed",
                "status": "failed",
                "version_id": version["version_id"],
                "artifacts": version["artifacts"],
                "error": "refresh failed",
            },
        ],
    }
    (source / "product_refs.json").write_text(json.dumps(document), encoding="utf-8")
    return document, store.blobs.path(blob)


def test_reference_and_standalone_exports_are_pinned_and_independent(tmp_path, make_symlink):
    source = tmp_path / "source"
    source.mkdir()
    (source / "members.csv").write_text("source_id\n123\n", encoding="utf-8")
    store = LibraryStore(tmp_path / "library")
    _, cas_path = _product_reference_fixture(store, source)
    _, published_run = _published_corpus(store, source)
    (source / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "active_run_id": published_run,
                "storage": {
                    "kind": "shared_library",
                    "library_root": str(store.root),
                    "relative_library_root": os.path.relpath(store.root, source),
                    "index_path": "index.sqlite3",
                },
            }
        ),
        encoding="utf-8",
    )
    cas_hash = cas_path.read_bytes()

    reference_destination = tmp_path / "reference-export"
    reference = export_corpus(store, source, reference_destination)
    reference_doc = json.loads(
        (reference_destination / "product_refs.json").read_text(encoding="utf-8")
    )
    assert reference["storage_kind"] == "shared_library"
    assert reference_doc["storage"]["kind"] == "shared_library"
    assert len(reference_doc["items"]) == 1
    assert Path(reference_doc["items"][0]["artifacts"][0]["verified_path"]) == cas_path
    expected_relative = os.path.relpath(store.root, reference_destination)
    assert reference_doc["storage"]["relative_library_root"] == expected_relative
    reference_manifest = json.loads(
        (reference_destination / "manifest.json").read_text(encoding="utf-8")
    )
    assert "active_run_id" not in reference_manifest
    assert reference_manifest["snapshot_run_id"] == published_run
    assert reference["run_id"] == published_run
    assert reference_manifest["storage"]["kind"] == "shared_library"
    assert reference_manifest["storage"]["relative_library_root"] == expected_relative

    legacy_product_dir = source / "gaia_xp_sampled"
    legacy_product_dir.mkdir()
    make_symlink(legacy_product_dir / "123.fits", cas_path)
    standalone_destination = tmp_path / "standalone-export"
    standalone = export_corpus(
        store, source, standalone_destination, standalone=True
    )
    standalone_doc = json.loads(
        (standalone_destination / "product_refs.json").read_text(encoding="utf-8")
    )
    local_spectrum = standalone_destination / "gaia_xp_sampled" / "123.fits"
    assert standalone["storage_kind"] == "standalone"
    assert standalone_doc["storage"] == {"kind": "standalone", "root": "."}
    standalone_manifest = json.loads(
        (standalone_destination / "manifest.json").read_text(encoding="utf-8")
    )
    assert "active_run_id" not in standalone_manifest
    assert standalone_manifest["snapshot_run_id"] == published_run
    assert standalone["run_id"] == published_run
    assert standalone_manifest["storage"] == {"kind": "standalone"}
    assert len(standalone_doc["items"]) == 1
    standalone_artifact = standalone_doc["items"][0]["artifacts"][0]
    assert standalone_artifact["blob"]["relative_path"] == (
        "gaia_xp_sampled/123.fits"
    )
    assert standalone_artifact["verified_path"] == "gaia_xp_sampled/123.fits"
    assert not local_spectrum.is_symlink()
    assert local_spectrum.stat().st_ino != cas_path.stat().st_ino
    assert local_spectrum.read_bytes() == b"standalone spectrum bytes"
    assert fetch_xp_spectrum("123", standalone_destination)["path"] == local_spectrum

    moved = tmp_path / "moved-standalone"
    shutil.move(standalone_destination, moved)
    moved_spectrum = moved / "gaia_xp_sampled" / "123.fits"
    moved_spectrum.chmod(0o644)
    moved_spectrum.write_bytes(b"changed export")
    assert cas_path.read_bytes() == cas_hash
    assert (source / "members.csv").read_text(encoding="utf-8") == "source_id\n123\n"


def test_default_export_uses_committed_active_snapshot_during_partial_promotion(
    tmp_path,
):
    source = tmp_path / "source"
    source.mkdir()
    store = LibraryStore(tmp_path / "library")
    corpus_id = store.register_corpus(source, "sample")

    active_run = store.start_run(corpus_id)
    active_dir = source / "runs" / active_run
    active_dir.mkdir(parents=True)
    (active_dir / "members.csv").write_text(
        "source_id\nactive\n", encoding="utf-8"
    )
    (active_dir / "manifest.json").write_text(
        json.dumps(
            {
                "snapshot_run_id": active_run,
                "storage": {"kind": "shared_library"},
            }
        ),
        encoding="utf-8",
    )
    store.finish_run(
        active_run,
        "completed",
        metadata={"members_indexed": True, "published": True},
    )

    pending_run = store.start_run(corpus_id)
    pending_dir = source / "runs" / pending_run
    pending_dir.mkdir(parents=True)
    (pending_dir / "members.csv").write_text(
        "source_id\npending\n", encoding="utf-8"
    )
    (source / "members.csv").write_text(
        "source_id\npending\n", encoding="utf-8"
    )
    (source / "manifest.json").write_text(
        json.dumps(
            {
                "active_run_id": active_run,
                "current_run_id": pending_run,
                "storage": {"kind": "shared_library"},
            }
        ),
        encoding="utf-8",
    )

    destination = tmp_path / "export"
    result = export_corpus(store, source, destination)

    assert result["run_id"] == active_run
    assert (destination / "members.csv").read_text(encoding="utf-8") == (
        "source_id\nactive\n"
    )
    assert not (destination / "runs" / pending_run).exists()
    manifest = json.loads(
        (destination / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["snapshot_run_id"] == active_run


def test_explicit_run_export_projects_that_immutable_snapshot(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "notes.txt").write_text("static", encoding="utf-8")
    store = LibraryStore(tmp_path / "library")
    corpus_id = store.register_corpus(source, "sample")

    old_run = store.start_run(corpus_id)
    old_dir = source / "runs" / old_run
    old_dir.mkdir(parents=True)
    (old_dir / "members.csv").write_text("source_id\nold\n", encoding="utf-8")
    (old_dir / "old_only.csv").write_text("value\nold\n", encoding="utf-8")
    (old_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "snapshot_run_id": old_run,
                "storage": {
                    "kind": "shared_library",
                    "library_root": str(store.root),
                    "relative_library_root": os.path.relpath(store.root, old_dir),
                    "index_path": "index.sqlite3",
                },
            }
        ),
        encoding="utf-8",
    )
    store.finish_run(
        old_run,
        "completed",
        metadata={"members_indexed": True, "published": True},
    )

    current_run = store.start_run(corpus_id)
    current_dir = source / "runs" / current_run
    current_dir.mkdir(parents=True)
    (current_dir / "members.csv").write_text(
        "source_id\ncurrent\n", encoding="utf-8"
    )
    (current_dir / "current_only.csv").write_text(
        "value\ncurrent\n", encoding="utf-8"
    )
    (current_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "snapshot_run_id": current_run,
                "storage": {"kind": "shared_library"},
            }
        ),
        encoding="utf-8",
    )
    (source / "members.csv").write_text(
        "source_id\ncurrent\n", encoding="utf-8"
    )
    (source / "current_only.csv").write_text(
        "value\ncurrent\n", encoding="utf-8"
    )
    (source / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "active_run_id": current_run,
                "current_run_id": current_run,
                "storage": {"kind": "shared_library"},
            }
        ),
        encoding="utf-8",
    )
    store.finish_run(
        current_run,
        "completed",
        metadata={"members_indexed": True, "published": True},
    )

    destination = tmp_path / "old-export"
    result = export_corpus(store, source, destination, run_id=old_run)

    assert result["run_id"] == old_run
    assert (destination / "members.csv").read_text(encoding="utf-8") == (
        "source_id\nold\n"
    )
    assert (destination / "old_only.csv").is_file()
    assert not (destination / "current_only.csv").exists()
    assert (destination / "notes.txt").read_text(encoding="utf-8") == "static"
    assert (destination / "runs" / old_run / "members.csv").is_file()
    assert not (destination / "runs" / current_run).exists()
    root_manifest = json.loads(
        (destination / "manifest.json").read_text(encoding="utf-8")
    )
    snapshot_manifest = json.loads(
        (destination / "runs" / old_run / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert "active_run_id" not in root_manifest
    assert "current_run_id" not in root_manifest
    assert root_manifest["snapshot_run_id"] == old_run
    assert snapshot_manifest["snapshot_run_id"] == old_run
    assert root_manifest["storage"]["kind"] == "shared_library"
    assert root_manifest["storage"]["relative_library_root"] == os.path.relpath(
        store.root, destination
    )
    assert snapshot_manifest["storage"]["relative_library_root"] == os.path.relpath(
        store.root, destination / "runs" / old_run
    )


def test_managed_export_requires_an_immutable_snapshot_by_default(tmp_path):
    source = tmp_path / "managed"
    source.mkdir()
    (source / "members.csv").write_text(
        "source_id\nmutable-root\n", encoding="utf-8"
    )
    store = LibraryStore(tmp_path / "library")
    corpus_id = store.register_corpus(
        source, "managed", provenance_status="verified"
    )
    logical_run = store.start_run(corpus_id)
    store.index_members(
        logical_run,
        [{"source_id": "123", "gaia_release": "dr3"}],
    )
    store.finish_run(
        logical_run,
        "completed",
        metadata={"members_indexed": True, "published": True},
    )

    destination = tmp_path / "export"
    with pytest.raises(FileNotFoundError, match="managed run snapshot is missing"):
        export_corpus(store, source, destination)
    assert not destination.exists()


def test_standalone_legacy_export_preserves_metadata_without_inventing_ids(tmp_path):
    source = tmp_path / "legacy"
    source.mkdir()
    (source / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "legacy",
                "custom": {"opaque": True},
                "storage": {
                    "kind": "shared_library",
                    "library_root": "/old/library",
                    "relative_library_root": "../old/library",
                    "index_path": "index.sqlite3",
                },
            }
        ),
        encoding="utf-8",
    )
    store = LibraryStore(tmp_path / "library")
    corpus_id = store.register_corpus(
        source, "legacy", provenance_status="legacy_unverified"
    )
    logical_run = store.start_run(corpus_id)
    store.finish_run(
        logical_run,
        "complete",
        metadata={
            "legacy_unverified": True,
            "members_indexed": True,
            "published": True,
        },
    )
    destination = tmp_path / "standalone-legacy"

    result = export_corpus(store, source, destination, standalone=True)

    manifest = json.loads(
        (destination / "manifest.json").read_text(encoding="utf-8")
    )
    assert result["run_id"] is None
    assert manifest["schema_version"] == "legacy"
    assert manifest["custom"] == {"opaque": True}
    assert manifest["storage"] == {"kind": "standalone"}
    assert "snapshot_run_id" not in manifest
    assert "active_run_id" not in manifest
    assert "corpus_id" not in manifest


def test_nested_standalone_refs_use_containing_directory_paths(tmp_path, tmp_path_factory):
    if os.name == "nt":
        # This case tests relative reference roots, not Windows' path-length cap.
        tmp_path = tmp_path_factory.mktemp("nested")
    source = tmp_path / "source"
    source.mkdir()
    store = LibraryStore(tmp_path / "library")
    _product_reference_fixture(store, source)
    product_dir = source / "gaia_xp_sampled"
    product_dir.mkdir()
    shutil.move(source / "product_refs.json", product_dir / "product_refs.json")
    _published_corpus(store, source)

    destination = tmp_path / "standalone"
    export_corpus(store, source, destination, standalone=True)

    reference_path = destination / "gaia_xp_sampled" / "product_refs.json"
    document = json.loads(reference_path.read_text(encoding="utf-8"))
    artifact = document["items"][0]["artifacts"][0]
    assert document["storage"] == {"kind": "standalone", "root": "."}
    assert artifact["blob"]["relative_path"] == "123.fits"
    assert artifact["verified_path"] == "123.fits"
    assert ".." not in Path(artifact["blob"]["relative_path"]).parts
    assert (reference_path.parent / "123.fits").read_bytes() == (
        b"standalone spectrum bytes"
    )


def test_standalone_export_rejects_external_file_and_directory_symlinks(
    tmp_path, monkeypatch, make_symlink
):
    from astro_pipeline.library import import_export as import_export_module

    source = tmp_path / "source"
    source.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("credential-like contents", encoding="utf-8")
    leak = source / "leak.txt"
    make_symlink(leak, secret)
    store = LibraryStore(tmp_path / "library")
    original_copy = import_export_module.atomic_copy

    def guarded_copy(copy_source, destination):
        assert Path(copy_source).resolve() != secret.resolve()
        return original_copy(copy_source, destination)

    monkeypatch.setattr(import_export_module, "atomic_copy", guarded_copy)
    destination = tmp_path / "file-export"
    with pytest.raises(ValueError, match="refuses external"):
        export_corpus(store, source, destination, standalone=True)
    assert not destination.exists()

    leak.unlink()
    external_directory = tmp_path / "external-directory"
    external_directory.mkdir()
    (external_directory / "outside.txt").write_text("outside", encoding="utf-8")
    make_symlink(
        source / "directory-link", external_directory, target_is_directory=True
    )
    directory_destination = tmp_path / "directory-export"
    with pytest.raises(ValueError, match="refuses external"):
        export_corpus(store, source, directory_destination, standalone=True)
    assert not directory_destination.exists()


def test_export_refuses_overlap_existing_destination_and_corrupt_blob(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "members.csv").write_text("source_id\n123\n", encoding="utf-8")
    store = LibraryStore(tmp_path / "library")
    _, cas_path = _product_reference_fixture(store, source)
    _published_corpus(store, source)

    with pytest.raises(ValueError, match="overlap"):
        export_corpus(store, source, source / "nested")
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(FileExistsError):
        export_corpus(store, source, existing)

    cas_path.chmod(0o644)
    cas_path.write_bytes(b"broken")
    destination = tmp_path / "failed-export"
    with pytest.raises(Exception, match="truncated|checksum"):
        export_corpus(store, source, destination, standalone=True)
    assert not destination.exists()
