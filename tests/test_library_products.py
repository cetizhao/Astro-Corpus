"""Offline coverage for shared, content-addressed Gaia products."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading

from astropy.table import Table
import pytest

import astro_pipeline.gaia_products as gaia_products
import astro_pipeline.library.products as library_products
from astro_pipeline.config import GaiaConfig, NetworkConfig
from astro_pipeline.gaia_products import GaiaProductRequest, download_gaia_product_request
from astro_pipeline.library.leases import LeaseBatch
from astro_pipeline.library.store import LeaseLostError, LibraryStore
from astro_pipeline.sources.gaia import GaiaSource


def _request(
    identifiers,
    *,
    release: str = "dr3",
    retrieval_type: str = "XP_SAMPLED",
    output_format: str = "fits",
    batch_size: int = 5,
) -> GaiaProductRequest:
    return GaiaProductRequest(
        release=release,
        retrieval_type=retrieval_type,
        identifiers=identifiers,
        data_structure="RAW" if release == "dr3" else "INDIVIDUAL",
        output_format=output_format,
        linking_parameter="SOURCE_ID",
        batch_size=batch_size,
    )


def _loader(calls: list[dict], *, flux_offset: float = 0.0):
    def load(**kwargs):
        calls.append(kwargs)
        identifiers = [str(value) for value in kwargs["ids"]]
        return {
            "archive-product": [
                Table(
                    {
                        "source_id": [int(value) for value in identifiers],
                        "flux": [
                            [float(value) + flux_offset, float(value) + 1 + flux_offset]
                            for value in identifiers
                        ],
                    },
                    meta={"origin": "fake-loader"},
                )
            ]
        }

    return load


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _ref_item(output_dir: Path, identifier: str) -> dict:
    refs = _read_json(output_dir / "product_refs.json")
    return next(item for item in refs["items"] if item["identifier"] == identifier)


def test_cross_label_overlap_downloads_each_item_once_without_label_fits(tmp_path):
    store = LibraryStore(tmp_path / "library")
    calls: list[dict] = []
    first = download_gaia_product_request(
        _request(["100", "200"], batch_size=2),
        tmp_path / "label-a",
        library=store,
        loader=_loader(calls),
    )
    second = download_gaia_product_request(
        _request(["200", "300"], batch_size=1),
        tmp_path / "label-b",
        library=store,
        loader=_loader(calls),
    )

    assert [call["ids"] for call in calls] == [["100", "200"], ["300"]]
    assert {item.identifier for item in first if item.success} == {"100", "200"}
    assert {item.identifier for item in second if item.success} == {"200", "300"}
    assert not list((tmp_path / "label-a").glob("*.fits"))
    assert not (tmp_path / "label-a" / "archive_tables").exists()
    assert all(Path(item.normalized_path).is_file() for item in first + second if item.success)
    assert all(
        Path(item.normalized_path).is_relative_to(store.blobs.objects)
        for item in first + second
        if item.success
    )
    normalized = store.read(
        "SELECT COUNT(DISTINCT blob_sha256) AS n FROM product_artifacts "
        "WHERE role = 'normalized'"
    )
    assert normalized[0]["n"] == 3


def test_concurrent_cross_label_request_has_one_loader_owner(tmp_path):
    store = LibraryStore(tmp_path / "library")
    loader_started = threading.Event()
    release_loader = threading.Event()
    calls: list[dict] = []
    results: list[list] = []
    errors: list[BaseException] = []

    def slow_loader(**kwargs):
        calls.append(kwargs)
        loader_started.set()
        assert release_loader.wait(timeout=5)
        return {"archive": [Table({"source_id": [100], "flux": [[1.0, 2.0]]})]}

    def run(label: str, loader):
        try:
            results.append(
                download_gaia_product_request(
                    _request(["100"]),
                    tmp_path / label,
                    library=store,
                    loader=loader,
                    wait_timeout_seconds=5,
                )
            )
        except BaseException as exc:  # asserted below from the coordinator thread
            errors.append(exc)

    first = threading.Thread(target=run, args=("label-a", slow_loader))
    second = threading.Thread(
        target=run,
        args=(
            "label-b",
            lambda **kwargs: pytest.fail("second owner called loader"),
        ),
    )
    first.start()
    assert loader_started.wait(timeout=5)
    second.start()
    release_loader.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert len(calls) == 1
    assert len(results) == 2
    assert len({batch[0].version_id for batch in results}) == 1


def test_large_claim_set_uses_one_batch_renewal_thread(tmp_path, monkeypatch):
    store = LibraryStore(tmp_path / "library")
    created_threads: list[str] = []
    real_thread = threading.Thread

    def tracking_thread(*args, **kwargs):
        created_threads.append(str(kwargs.get("name", "")))
        return real_thread(*args, **kwargs)

    monkeypatch.setattr(threading, "Thread", tracking_thread)
    identifiers = [str(1000 + index) for index in range(50)]
    artifacts = download_gaia_product_request(
        _request(identifiers, batch_size=50),
        tmp_path / "large-label",
        library=store,
        loader=_loader([]),
    )

    assert len(artifacts) == 50
    assert created_threads.count("library-lease-batch-product") == 1
    assert created_threads.count("library-lease-product-view") == 1
    assert "library-lease-product" not in created_threads


def test_lease_batch_renews_together_and_allows_fenced_takeover(tmp_path):
    now = [100.0]
    store = LibraryStore(tmp_path / "library", clock=lambda: now[0])

    with LeaseBatch(store, "product", ["a", "b", "a"], ttl=10) as batch:
        assert list(batch.leases) == ["a", "b"]
        assert batch.busy == []
        old_lease = batch.leases["a"]
        with LeaseBatch(store, "product", ["a", "c"], ttl=10) as competitor:
            assert competitor.busy == ["a"]
            assert list(competitor.leases) == ["c"]

        now[0] = 106.0
        batch.renew_now()
        now[0] = 111.0
        assert store.acquire_lease("product", "a", owner="too-early", ttl=10) is None
        assert batch.assert_current("a").token == old_lease.token

    takeover = store.acquire_lease("product", "a", owner="new-owner", ttl=10)
    assert takeover is not None
    assert takeover.token == old_lease.token + 1
    with store.transaction() as connection:
        with pytest.raises(LeaseLostError):
            store.assert_lease(connection, old_lease)


def test_shared_archive_blob_is_retained_by_each_item_version(tmp_path):
    store = LibraryStore(tmp_path / "library")
    artifacts = download_gaia_product_request(
        _request(["100", "200"], batch_size=2),
        tmp_path / "label",
        library=store,
        loader=_loader([]),
    )
    by_identifier = {item.identifier: item for item in artifacts}
    assert by_identifier["100"].archive_blob["sha256"]
    assert (
        by_identifier["100"].archive_blob["sha256"]
        == by_identifier["200"].archive_blob["sha256"]
    )
    archive_rows = store.read(
        "SELECT COUNT(*) AS n_refs, COUNT(DISTINCT blob_sha256) AS n_blobs "
        "FROM product_artifacts WHERE role = 'archive'"
    )[0]
    assert archive_rows == {"n_refs": 2, "n_blobs": 1}
    assert by_identifier["100"].metadata["preservation"] == (
        "astroquery_table_reencoded"
    )


def test_release_and_format_are_separate_semantic_items(tmp_path):
    store = LibraryStore(tmp_path / "library")
    calls: list[dict] = []
    outputs = []
    outputs.extend(
        download_gaia_product_request(
            _request(["100"], output_format="fits"),
            tmp_path / "dr3-fits",
            library=store,
            loader=_loader(calls),
        )
    )
    outputs.extend(
        download_gaia_product_request(
            _request(["100"], output_format="ecsv"),
            tmp_path / "dr3-ecsv",
            library=store,
            loader=_loader(calls),
        )
    )
    outputs.extend(
        download_gaia_product_request(
            _request(
                ["100"],
                release="dr4",
                retrieval_type="EPOCH_ASTROMETRY",
                output_format="fits",
            ),
            tmp_path / "dr4-fits",
            library=store,
            loader=_loader(calls),
        )
    )

    assert len(calls) == 3
    assert len({item.item_key for item in outputs}) == 3
    assert len({item.version_id for item in outputs}) == 3


def test_subset_order_and_batch_size_do_not_change_identity(tmp_path):
    store = LibraryStore(tmp_path / "library")
    calls: list[dict] = []
    initial = download_gaia_product_request(
        _request(["200", "100"], batch_size=2),
        tmp_path / "initial",
        library=store,
        loader=_loader(calls),
    )
    old_100 = next(item for item in initial if item.identifier == "100")
    subset = download_gaia_product_request(
        _request(["100"], batch_size=1),
        tmp_path / "subset",
        library=store,
        loader=lambda **kwargs: pytest.fail("semantic cache hit called loader"),
    )

    assert len(calls) == 1
    assert subset[0].item_key == old_100.item_key
    assert subset[0].version_id == old_100.version_id
    assert subset[0].cache_status == "hit"


def test_refresh_pins_new_version_without_rewriting_old_label(tmp_path):
    store = LibraryStore(tmp_path / "library")
    old_dir = tmp_path / "old-label"
    old = download_gaia_product_request(
        _request(["100"]),
        old_dir,
        library=store,
        loader=_loader([]),
    )[0]
    old_refs_before = (old_dir / "product_refs.json").read_bytes()

    refreshed = download_gaia_product_request(
        _request(["100"]),
        tmp_path / "refresh-label",
        library=store,
        loader=_loader([], flux_offset=1000.0),
        refresh=True,
    )[0]

    assert refreshed.version_id != old.version_id
    assert refreshed.cache_status == "refreshed"
    assert store.get_product_version(old.version_id, verify=True) is not None
    assert store.get_product_version(refreshed.version_id, verify=True) is not None
    assert (old_dir / "product_refs.json").read_bytes() == old_refs_before
    assert _ref_item(old_dir, "100")["version_id"] == old.version_id


def test_corrupt_cached_blob_is_redownloaded_and_quarantined(tmp_path):
    store = LibraryStore(tmp_path / "library")
    first = download_gaia_product_request(
        _request(["100"]),
        tmp_path / "first",
        library=store,
        loader=_loader([]),
    )[0]
    damaged = Path(first.normalized_path)
    os.chmod(damaged, 0o644)
    damaged.write_bytes(b"truncated")
    calls: list[dict] = []

    repaired = download_gaia_product_request(
        _request(["100"]),
        tmp_path / "repair",
        library=store,
        loader=_loader(calls),
    )[0]

    assert len(calls) == 1
    assert repaired.success is True
    assert repaired.cache_status == "repaired"
    assert store.blobs.verify(repaired.normalized_blob) == Path(repaired.normalized_path)
    assert list(store.blobs.staging.glob("damaged-*"))


def test_failed_refresh_keeps_old_ready_but_marks_current_view_failed(tmp_path):
    store = LibraryStore(tmp_path / "library")
    old = download_gaia_product_request(
        _request(["100"]),
        tmp_path / "old",
        library=store,
        loader=_loader([]),
    )[0]

    failed = download_gaia_product_request(
        _request(["100"]),
        tmp_path / "failed-refresh",
        library=store,
        loader=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("refresh outage")),
        refresh=True,
    )[0]
    current = store.get_product(old.item_key, verify=True)
    ref = _ref_item(tmp_path / "failed-refresh", "100")
    checkpoint = _read_json(tmp_path / "failed-refresh" / "checkpoint.json")

    assert failed.success is False
    assert current["version_id"] == old.version_id
    assert ref["status"] == "refresh_failed"
    assert ref["version_id"] is None
    assert ref["previous_ready_version_id"] == old.version_id
    assert ref["artifacts"] == []
    assert checkpoint["completed"] == []
    assert checkpoint["failed"] == ["100"]


def test_explicit_absence_is_negative_cached_but_loader_failure_is_not(tmp_path):
    store = LibraryStore(tmp_path / "library")
    calls: list[dict] = []

    absent = download_gaia_product_request(
        _request(["100"]),
        tmp_path / "absent-a",
        library=store,
        loader=lambda **kwargs: calls.append(kwargs) or {},
    )[0]
    cached = download_gaia_product_request(
        _request(["100"]),
        tmp_path / "absent-b",
        library=store,
        loader=lambda **kwargs: pytest.fail("negative cache called loader"),
    )[0]
    assert absent.success is False
    assert cached.cache_status == "negative_hit"
    assert len(calls) == 1

    failure_calls: list[dict] = []
    for label in ("failure-a", "failure-b"):
        result = download_gaia_product_request(
            _request(["200"]),
            tmp_path / label,
            library=store,
            loader=lambda **kwargs: (
                failure_calls.append(kwargs),
                (_ for _ in ()).throw(RuntimeError("temporary failure")),
            )[1],
        )[0]
        assert result.success is False
    assert len(failure_calls) == 2

    malformed_calls: list[dict] = []
    for label in ("malformed-a", "malformed-b"):
        malformed = download_gaia_product_request(
            _request(["300", "400"], batch_size=2),
            tmp_path / label,
            library=store,
            loader=lambda **kwargs: (
                malformed_calls.append(kwargs)
                or {"unassociated": [Table({"time": [1.0]})]}
            ),
        )
        assert all(not item.success for item in malformed)
    assert len(malformed_calls) == 2


def test_expired_negative_requeries_instead_of_resurrecting_old_ready(tmp_path):
    now = [1000.0]
    store = LibraryStore(tmp_path / "library", clock=lambda: now[0])
    old = download_gaia_product_request(
        _request(["100"]),
        tmp_path / "old-ready",
        library=store,
        loader=_loader([]),
    )[0]
    absent = download_gaia_product_request(
        _request(["100"]),
        tmp_path / "refresh-absent",
        library=store,
        loader=lambda **kwargs: {},
        refresh=True,
    )[0]
    assert absent.success is False
    assert store.get_product(old.item_key, verify=True)["version_id"] == old.version_id

    now[0] += store.negative_ttl_seconds + 1
    calls: list[dict] = []
    current = download_gaia_product_request(
        _request(["100"]),
        tmp_path / "after-expiry",
        library=store,
        loader=_loader(calls, flux_offset=20.0),
    )[0]
    assert len(calls) == 1
    assert current.version_id != old.version_id
    assert current.cache_status == "downloaded"


def test_lost_fence_cannot_publish_current_label_view(tmp_path, monkeypatch):
    store = LibraryStore(tmp_path / "library")

    def reject_stale(*args, **kwargs):
        raise LeaseLostError("stale fencing token")

    monkeypatch.setattr(store, "save_product", reject_stale)
    with pytest.raises(LeaseLostError, match="lost fencing token"):
        download_gaia_product_request(
            _request(["100"]),
            tmp_path / "stale",
            library=store,
            loader=_loader([]),
        )

    assert not (tmp_path / "stale" / "product_refs.json").exists()
    assert not (tmp_path / "stale" / "checkpoint.json").exists()
    assert not (tmp_path / "stale" / "artifacts.json").exists()
    assert store.status()["n_ready_products"] == 0


def test_view_publication_holds_fence_through_atomic_replacements(
    tmp_path, monkeypatch
):
    now = [100.0]
    store = LibraryStore(
        tmp_path / "library",
        busy_timeout_ms=2000,
        clock=lambda: now[0],
    )
    store.initialize()
    output_dir = tmp_path / "shared-view"
    output_dir.mkdir()
    lease_key = library_products.product_view_lease_key(output_dir)
    lease_a = store.acquire_lease(
        "product-view", lease_key, owner="worker-a", ttl=1
    )
    assert lease_a is not None

    after_fence = threading.Event()
    allow_a_to_write = threading.Event()
    b_attempted = threading.Event()
    b_acquired = threading.Event()
    b_published = threading.Event()
    takeover_tokens: list[int] = []
    errors: list[BaseException] = []
    real_merge = library_products.merge_product_refs

    def blocking_merge(*args, **kwargs):
        if threading.current_thread().name == "publisher-a":
            after_fence.set()
            assert allow_a_to_write.wait(timeout=5)
        return real_merge(*args, **kwargs)

    monkeypatch.setattr(library_products, "merge_product_refs", blocking_merge)
    request = _request(["100"])

    def publish(version_id: str, lease) -> None:
        view = library_products.ProductViewItem(
            identifier="100",
            item_key="item-100",
            status="ready",
            version_id=version_id,
            created_at=now[0],
            cache_status="test",
        )
        gaia_products._publish_shared_view_metadata(
            store,
            lease,
            output_dir=output_dir,
            contract=request.immutable_contract(),
            source_name="gaiadr3:XP_SAMPLED",
            updates=[view],
            requested_identifiers=["100"],
            artifacts=[],
        )

    def run_a() -> None:
        try:
            publish("version-a", lease_a)
        except BaseException as exc:  # asserted by the coordinator thread
            errors.append(exc)

    def run_b() -> None:
        try:
            b_attempted.set()
            lease_b = store.acquire_lease(
                "product-view", lease_key, owner="worker-b", ttl=10
            )
            assert lease_b is not None
            takeover_tokens.append(lease_b.token)
            b_acquired.set()
            publish("version-b", lease_b)
            b_published.set()
        except BaseException as exc:  # asserted by the coordinator thread
            errors.append(exc)

    publisher_a = threading.Thread(target=run_a, name="publisher-a")
    publisher_a.start()
    assert after_fence.wait(timeout=5)

    # A's lease is now expired, but its BEGIN IMMEDIATE transaction still fences
    # the metadata replacements.  B can try to take over but cannot acquire the
    # lease row until A has finished its atomic replacements and committed.
    now[0] = 102.0
    publisher_b = threading.Thread(target=run_b, name="publisher-b")
    publisher_b.start()
    assert b_attempted.wait(timeout=5)
    assert not b_acquired.wait(timeout=0.1)
    assert not b_published.is_set()

    allow_a_to_write.set()
    publisher_a.join(timeout=5)
    publisher_b.join(timeout=5)

    assert not publisher_a.is_alive() and not publisher_b.is_alive()
    assert errors == []
    assert b_acquired.is_set() and b_published.is_set()
    assert takeover_tokens == [lease_a.token + 1]
    assert _ref_item(output_dir, "100")["version_id"] == "version-b"


def test_library_mode_rejects_nonempty_output_without_request_contract(tmp_path):
    output_dir = tmp_path / "legacy-output"
    output_dir.mkdir()
    sentinel = output_dir / "checkpoint.json"
    sentinel.write_text('{"legacy": true}\n', encoding="utf-8")
    store = LibraryStore(tmp_path / "library")

    with pytest.raises(ValueError, match="non-empty.*product_request.json"):
        download_gaia_product_request(
            _request(["100"]),
            output_dir,
            library=store,
            loader=lambda **kwargs: pytest.fail("preflight guard called loader"),
        )

    assert sentinel.read_text(encoding="utf-8") == '{"legacy": true}\n'
    assert not (output_dir / "product_request.json").exists()
    assert store.exists is False


def test_label_refs_and_current_checkpoint_are_idempotent(tmp_path):
    store = LibraryStore(tmp_path / "library")
    output_dir = tmp_path / "label"
    request = _request(["200", "100"], batch_size=2)
    download_gaia_product_request(
        request, output_dir, library=store, loader=_loader([])
    )
    download_gaia_product_request(
        request,
        output_dir,
        library=store,
        loader=lambda **kwargs: pytest.fail("idempotent hit called loader"),
    )

    refs = _read_json(output_dir / "product_refs.json")
    checkpoint = _read_json(output_dir / "checkpoint.json")
    manifest = _read_json(output_dir / "artifacts.json")
    assert refs["schema_version"] == "2.0"
    assert refs["storage"]["library_root"] == str(store.root)
    assert refs["storage"]["relative_library_root"] == os.path.relpath(
        store.root, output_dir
    )
    assert [item["identifier"] for item in refs["items"]] == ["200", "100"]
    assert all(item["version_id"] for item in refs["items"])
    assert checkpoint["completed"] == ["100", "200"]
    assert checkpoint["failed"] == []
    assert len(manifest["artifacts"]) == 2
    assert {item["identifier"] for item in manifest["artifacts"]} == {"100", "200"}


def test_gaia_source_cache_hit_does_not_login_or_load(tmp_path, monkeypatch):
    store = LibraryStore(tmp_path / "library")
    request = _request(["100"])
    download_gaia_product_request(
        request,
        tmp_path / "seed",
        library=store,
        loader=_loader([]),
    )
    source = GaiaSource(
        GaiaConfig(release="dr3"),
        NetworkConfig(),
        library_store=store,
    )
    monkeypatch.setattr(
        source, "_ensure_login", lambda: pytest.fail("cache hit attempted Gaia login")
    )
    monkeypatch.setattr(
        source, "_load_data", lambda **kwargs: pytest.fail("cache hit called loader")
    )

    artifacts = source.download_products(request, tmp_path / "source-hit")
    assert artifacts[0].success is True
    assert artifacts[0].cache_status == "hit"


def test_importing_gaia_products_does_not_import_astroquery_gaia():
    code = (
        "import sys; import astro_pipeline.gaia_products; "
        "assert 'astroquery.gaia' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
