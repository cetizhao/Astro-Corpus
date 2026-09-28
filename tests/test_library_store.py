"""Offline unit tests for the SQLite library foundation."""
from __future__ import annotations

import multiprocessing
from pathlib import Path
import sqlite3
import time

import pytest

from astro_pipeline.library.files import BlobIntegrityError
from astro_pipeline.library.identity import product_item_key
from astro_pipeline.library.store import (
    LeaseLostError,
    LibraryBusyError,
    LibraryStore,
)


def _lease_contender(root: str, barrier, results) -> None:
    store = LibraryStore(root)
    barrier.wait()
    lease = store.acquire_lease("test", "shared", ttl=5)
    results.put(lease is not None)
    barrier.wait()
    if lease is not None:
        store.release_lease(lease)


def _new_run(store: LibraryStore, path: Path, label: str = "sample") -> str:
    corpus_id = store.register_corpus(path, label)
    return store.start_run(corpus_id)


def test_absent_read_status_and_constructor_do_not_create_files(tmp_path):
    root = tmp_path / "library"
    store = LibraryStore(root)

    assert store.root == root.resolve()
    assert store.db_path == root.resolve() / "index.sqlite3"
    assert store.exists is False
    assert store.read("SELECT 1") == []
    assert store.status() == {
        "exists": False,
        "schema": 0,
        "n_sources": 0,
        "n_corpora": 0,
        "n_runs": 0,
        "n_papers": 0,
        "n_ready_products": 0,
        "n_blobs": 0,
    }
    assert not root.exists()


def test_initialize_wal_backup_and_integrity(tmp_path):
    store = LibraryStore(tmp_path / "library")
    store.initialize()

    assert store.exists
    assert store.read("PRAGMA user_version") == [{"user_version": 1}]
    assert store.read("PRAGMA journal_mode")[0]["journal_mode"].lower() == "wal"
    assert store.read("PRAGMA integrity_check") == [{"integrity_check": "ok"}]
    assert store.read("PRAGMA foreign_key_check") == []

    destination = tmp_path / "backup.sqlite3"
    assert store.backup(destination) == destination.resolve()
    with sqlite3.connect(destination) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with pytest.raises(FileExistsError):
        store.backup(destination)


def test_readonly_rejects_newer_schema_and_does_not_migrate_old(tmp_path):
    newer_root = tmp_path / "newer"
    newer_root.mkdir()
    with sqlite3.connect(newer_root / "index.sqlite3") as connection:
        connection.execute("PRAGMA user_version = 999")
    newer = LibraryStore(newer_root)
    with pytest.raises(RuntimeError, match="newer"):
        newer.read("SELECT 1")

    old_root = tmp_path / "old"
    old_root.mkdir()
    with sqlite3.connect(old_root / "index.sqlite3") as connection:
        connection.execute("CREATE TABLE legacy(value TEXT)")
        connection.execute("INSERT INTO legacy VALUES ('kept')")
    old = LibraryStore(old_root)
    with pytest.raises(RuntimeError, match="not an initialized Astro-Corpus"):
        old.read("SELECT value FROM legacy")
    with sqlite3.connect(old.db_path) as connection:
        assert connection.execute("SELECT value FROM legacy").fetchone()[0] == "kept"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'sources'"
        ).fetchone()[0] == 0


def test_scoped_corpora_runs_artifacts_and_attempts(tmp_path):
    store = LibraryStore(tmp_path / "library")
    first = store.register_corpus(tmp_path / "one", "same")
    second = store.register_corpus(tmp_path / "two", "same")
    assert first != second
    assert store.register_corpus(tmp_path / "one", "renamed") == first

    request_id = store.register_request({"mode": "cone", "radius": 0.1})
    run_id = store.start_run(first, request_id, options={"limit": 2})
    store.update_run(run_id, {"stage": "members"})
    artifact_id = store.record_run_artifact(
        run_id, "members", "members.parquet", "a" * 64, row_count=2
    )
    attempt_id = store.record_attempt("gaia", "batch", {"ids": ["1"]}, run_id)
    store.finish_attempt(attempt_id, "failed", "offline")
    store.finish_run(run_id, "complete", metadata={"n_members": 2})

    run = store.read("SELECT * FROM runs WHERE run_id = ?", (run_id,))[0]
    assert run["status"] == "complete"
    assert '"stage":"members"' in run["metadata_json"]
    assert '"n_members":2' in run["metadata_json"]
    assert store.read(
        "SELECT artifact_id FROM run_artifacts WHERE run_id = ?", (run_id,)
    ) == [{"artifact_id": artifact_id}]
    assert store.read("SELECT status FROM attempts WHERE id = ?", (attempt_id,)) == [
        {"status": "failed"}
    ]


def test_source_release_uniqueness_duplicates_and_partial_merge(tmp_path):
    store = LibraryStore(tmp_path / "library")
    run1 = _new_run(store, tmp_path / "corpus-1")
    counts = store.index_members(
        run1,
        members=[
            {
                "Gaia": "00123",
                "gaia_release": "dr3",
                "ra": 1.0,
                "dec": 2.0,
                "member_name": "not canonical",
                "input_note": "not canonical",
                "match_method": "not canonical",
            },
            {"source_id": "123", "gaia_release": "dr3", "ra": 1.0},
            {"source_id": "123", "gaia_release": "dr4", "ra": 9.0},
        ],
        input_rows=[
            {"input_row_id": "r0", "ordinal": 0, "status": "matched", "raw": {"x": 1}},
            {"input_row_id": "r1", "ordinal": 1, "status": "matched", "raw": {"x": 2}},
            {"input_row_id": "r2", "ordinal": 2, "status": "matched", "raw": {"x": 3}},
        ],
        matches=[
            {"member_ordinal": 0, "input_row_id": "r0", "method": "id"},
            {"member_ordinal": 1, "input_row_id": "r1", "method": "id"},
            {"member_ordinal": 2, "input_row_id": "r2", "method": "id"},
        ],
    )
    assert counts == {
        "n_input_rows": 3,
        "n_sample_members": 3,
        "n_unique_sources": 2,
    }
    assert store.status()["n_sources"] == 2
    assert len(store.read("SELECT * FROM sample_members")) == 3

    dr3 = store.source("Gaia DR3", "123")
    assert dr3 is not None
    assert dr3["attributes"] == {"dec": 2.0, "ra": 1.0}
    assert dr3["covered_fields"] == ["dec", "ra"]
    assert store.source("dr4", "123")["attributes"] == {"ra": 9.0}

    run2 = _new_run(store, tmp_path / "corpus-2")
    store.index_members(
        run2,
        [{"source_id": "123", "gaia_release": "dr3", "ra": 99.0, "parallax": 4.0}],
        verified=False,
    )
    dr3 = store.source("dr3", "123")
    assert dr3["attributes"] == {"dec": 2.0, "parallax": 4.0, "ra": 1.0}
    assert dr3["covered_fields"] == ["dec", "ra"]
    assert dr3["field_provenance"]["parallax"]["verified"] is False

    run3 = _new_run(store, tmp_path / "corpus-3")
    store.index_members(
        run3,
        [{"source_id": "123", "gaia_release": "dr3", "parallax": 5.0}],
    )
    dr3 = store.source("dr3", "123")
    assert dr3["attributes"] == {"dec": 2.0, "parallax": 5.0, "ra": 1.0}
    assert dr3["covered_fields"] == ["dec", "parallax", "ra"]


def test_transaction_rolls_back_and_foreign_keys_are_enforced(tmp_path):
    store = LibraryStore(tmp_path / "library")
    with pytest.raises(RuntimeError, match="rollback"):
        with store.transaction() as connection:
            connection.execute(
                "INSERT INTO corpora VALUES (?, ?, ?, ?, ?, ?)",
                ("id", "/tmp/id", "label", "verified", 1.0, 1.0),
            )
            raise RuntimeError("rollback")
    assert store.read("SELECT * FROM corpora") == []

    with pytest.raises(sqlite3.IntegrityError):
        store.start_run("missing")
    assert store.read("PRAGMA foreign_key_check") == []


def test_lease_takeover_fencing_renewal_and_bounded_hold(tmp_path):
    now = [100.0]
    store = LibraryStore(tmp_path / "library", clock=lambda: now[0])
    first = store.acquire_lease("product", "key", owner="one", ttl=10)
    assert first is not None and first.token == 1
    assert store.acquire_lease("product", "key", owner="two", ttl=10) is None
    with pytest.raises(LibraryBusyError):
        with store.hold_lease("product", "key", wait_timeout=0):
            pass

    now[0] = 111.0
    second = store.acquire_lease("product", "key", owner="two", ttl=10)
    assert second is not None and second.token == 2
    with store.transaction() as connection:
        with pytest.raises(LeaseLostError):
            store.assert_lease(connection, first)
    with pytest.raises(LeaseLostError):
        store.renew_lease(first)
    renewed = store.renew_lease(second, ttl=20)
    assert renewed.expires_at == 131.0
    store.release_lease(renewed)


def test_hold_lease_renews_and_releases(tmp_path):
    store = LibraryStore(tmp_path / "library")
    with store.hold_lease("long", "job", ttl=0.12) as lease:
        time.sleep(0.2)
        assert store.acquire_lease("long", "job", owner="competitor", ttl=1) is None
        with store.transaction() as connection:
            store.assert_lease(connection, lease)
    next_lease = store.acquire_lease("long", "job", owner="next", ttl=1)
    assert next_lease is not None and next_lease.token == lease.token + 1


def test_multiprocessing_lease_exclusion(tmp_path):
    root = str(tmp_path / "library")
    LibraryStore(root).initialize()
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    results = context.Queue()
    processes = [
        context.Process(target=_lease_contender, args=(root, barrier, results))
        for _ in range(3)
    ]
    for process in processes:
        process.start()
    acquired = [results.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    assert acquired.count(True) == 1


def test_product_versions_fencing_integrity_and_failed_refresh(tmp_path):
    store = LibraryStore(tmp_path / "library")
    source = tmp_path / "first.fits"
    source.write_bytes(b"first product")
    first_blob = store.blobs.publish(source, content_type="application/fits")
    contract = {"release": "dr3", "retrieval_type": "XP_SAMPLED"}
    item_key = product_item_key(contract, "123")

    first_lease = store.acquire_lease("product", item_key, owner="worker-1")
    assert first_lease is not None
    first = store.save_product(
        contract,
        "123",
        [{"blob": first_blob, "role": "normalized", "slot": "spectrum", "metadata": {}}],
        first_lease,
        metadata={"generation": 1},
    )
    store.release_lease(first_lease)
    assert first["item_key"] == item_key
    assert store.get_product(item_key)["version_id"] == first["version_id"]

    second_source = tmp_path / "second.fits"
    second_source.write_bytes(b"second product")
    second_blob = store.blobs.publish(second_source, content_type="application/fits")
    second_lease = store.acquire_lease("product", item_key, owner="worker-2")
    assert second_lease is not None and second_lease.token > first_lease.token
    with pytest.raises(LeaseLostError):
        store.save_product(
            contract,
            "123",
            [{"blob": second_blob, "role": "normalized", "slot": "spectrum", "metadata": {}}],
            first_lease,
        )
    second = store.save_product(
        contract,
        "123",
        [{"blob": second_blob, "role": "normalized", "slot": "spectrum", "metadata": {}}],
        second_lease,
        metadata={"generation": 2},
    )
    store.release_lease(second_lease)
    assert second["version_id"] != first["version_id"]
    assert store.get_product_version(first["version_id"])["metadata"] == {"generation": 1}
    assert store.record_product_error(item_key, "refresh failed") is True
    assert store.get_product(item_key)["version_id"] == second["version_id"]

    object_path = store.blobs.path(second_blob)
    object_path.chmod(0o644)
    object_path.write_bytes(b"corrupt")
    with pytest.raises(BlobIntegrityError):
        store.get_product(item_key)
    assert store.read(
        "SELECT active_version_id, state FROM product_items WHERE item_key = ?", (item_key,)
    ) == [{"active_version_id": second["version_id"], "state": "ready"}]


def test_cache_positive_negative_stale_failure_and_lease_validation(tmp_path):
    now = [10.0]
    store = LibraryStore(tmp_path / "library", clock=lambda: now[0])
    success_id = store.put_cache("availability", "123", {"available": True}, ttl_seconds=5)
    assert store.get_cache("availability", "123") == {
        "id": success_id,
        "payload": {"available": True},
        "outcome": "success",
        "observed_at": 10.0,
        "expires_at": 15.0,
        "stale": False,
        "error": None,
    }
    store.put_cache(
        "availability", "123", None, outcome="failure", ttl_seconds=1, error="timeout"
    )
    assert store.get_cache("availability", "123")["id"] == success_id

    negative_id = store.put_cache(
        "availability", "456", {"available": False}, outcome="negative", ttl_seconds=2
    )
    assert store.get_cache("availability", "456")["id"] == negative_id
    now[0] = 12.0
    assert store.get_cache("availability", "456") is None
    assert store.get_cache("availability", "456", include_stale=True)["stale"] is True

    lease = store.acquire_lease("cache", "fenced", ttl=1)
    assert lease is not None
    now[0] = 14.0
    with pytest.raises(LeaseLostError):
        store.put_cache("cache", "fenced", {}, ttl_seconds=1, lease=lease)


def test_empty_source_references_refresh_failure_and_paper_merge(tmp_path):
    now = [100.0]
    store = LibraryStore(tmp_path / "library", clock=lambda: now[0])
    empty_id = store.put_source_references(
        "dr3", "123", [], main_id="Gaia DR3 123", ttl_seconds=2
    )
    empty = store.get_source_references("dr3", "123")
    assert empty["snapshot_id"] == empty_id
    assert empty["papers"] == []
    assert store.source("dr3", "123")["covered_fields"] == []

    now[0] = 101.0
    filled_id = store.put_source_references(
        "dr3",
        "123",
        [
            {"bibcode": "2026A&A...1A", "title": "First", "doi": "10.1/x"},
            {"bibcode": "2026A&A...1A", "title": "", "arxiv_id": "2601.00001"},
        ],
        ttl_seconds=2,
    )
    filled = store.get_source_references("dr3", "123")
    assert filled["snapshot_id"] == filled_id
    assert filled["papers"] == [
        {
            "arxiv_id": "2601.00001",
            "bibcode": "2026A&A...1A",
            "doi": "10.1/x",
            "title": "First",
        }
    ]

    now[0] = 102.0
    failure_id = store.put_source_references(
        "dr3", "123", [], outcome="failure", ttl_seconds=1
    )
    assert failure_id != filled_id
    assert store.get_source_references("dr3", "123")["snapshot_id"] == filled_id
    assert store.status()["n_papers"] == 1

    now[0] = 104.0
    assert store.get_source_references("dr3", "123") is None
    assert store.get_source_references("dr3", "123", include_stale=True)["stale"] is True
    assert store.read("PRAGMA foreign_key_check") == []
