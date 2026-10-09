from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from astropy.table import Table

from astro_pipeline import spectra
from astro_pipeline.library.files import BlobStore
from astro_pipeline.spectral_adapters import (
    DR3_XP_FLUX_UNIT,
    SPECTRUM_ADAPTERS,
    _clear_parsed_spectrum_cache,
    get_library_spectrum,
)


SOURCE_ID = "123456789"


def _contract(
    retrieval_type: str = "XP_SAMPLED",
    *,
    release: str = "dr3",
    output_format: str = "fits",
    linking_parameter: str = "SOURCE_ID",
) -> dict[str, Any]:
    return {
        "release": release,
        "archive_tag": "Gaia DR3" if release == "dr3" else "Gaia DR4",
        "retrieval_type": retrieval_type,
        "data_structure": "INDIVIDUAL",
        "output_format": output_format,
        "linking_parameter": linking_parameter,
        "valid_data": False,
    }


def _write_sampled(
    path: Path,
    source_id: str,
    flux: list[float],
    *,
    wavelength: list[float] | None = None,
    flux_error: list[float] | None = None,
) -> Path:
    wavelength = wavelength or [336.0 + 2.0 * index for index in range(len(flux))]
    flux_error = flux_error or [0.1] * len(flux)
    Table(
        {
            "source_id": [int(source_id)],
            "wavelength": [np.asarray(wavelength, dtype="float64")],
            "flux": [np.asarray(flux, dtype="float64")],
            "flux_error": [np.asarray(flux_error, dtype="float64")],
        }
    ).write(path)
    return path


def _publish_sampled(
    tmp_path: Path,
    blobs: BlobStore,
    name: str,
    source_id: str,
    flux: list[float],
    *,
    wavelength: list[float] | None = None,
    flux_error: list[float] | None = None,
) -> dict[str, Any]:
    source = _write_sampled(
        tmp_path / name,
        source_id,
        flux,
        wavelength=wavelength,
        flux_error=flux_error,
    )
    return blobs.publish(
        source,
        extension="fits",
        content_type="application/fits",
    ).to_dict()


def _version(
    item_key: str,
    source_id: str,
    blob: dict[str, Any],
    version_id: str,
    *,
    contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "item_key": item_key,
        "identifier": source_id,
        "version_id": version_id,
        "contract": contract or _contract(),
        "metadata": {},
        "artifacts": [
            {
                "role": "normalized",
                "slot": "normalized:000",
                "blob": blob,
                "metadata": {"is_primary": True},
            }
        ],
    }


def _row(
    item_key: str,
    source_id: str,
    contract: dict[str, Any],
    *,
    state: str = "ready",
    active_version_id: str | None = "version-current",
    updated_at: float = 1.0,
    last_error: str | None = None,
) -> dict[str, Any]:
    return {
        "item_key": item_key,
        "identifier": source_id,
        "state": state,
        "active_version_id": active_version_id,
        "updated_at": updated_at,
        "last_error": last_error,
        "contract_json": json.dumps(contract),
    }


class FakeStore:
    """Read-only duck type for LibraryStore backed by temporary blobs."""

    exists = True

    def __init__(
        self,
        blobs: BlobStore,
        *,
        product_rows: list[dict[str, Any]] | None = None,
        versions: dict[str, dict[str, Any] | None] | None = None,
        corpora: list[dict[str, Any]] | None = None,
        runs: list[dict[str, Any]] | None = None,
        cache: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.blobs = blobs
        self.product_rows = list(product_rows or [])
        self.versions = dict(versions or {})
        self.corpora = list(corpora or [])
        self.runs = list(runs or [])
        self.cache = dict(cache or {})
        self.read_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.get_product_calls: list[tuple[str, bool]] = []
        self.write_calls = 0

    def read(self, sql: str, params=()):
        params = tuple(params)
        self.read_calls.append((sql, params))
        if "FROM product_items AS i" in sql:
            source_id = str(params[0])
            return [
                {key: value for key, value in row.items() if key != "identifier"}
                for row in self.product_rows
                if row["identifier"] == source_id
            ]
        if "FROM product_contracts" in sql:
            unique: dict[str, dict[str, Any]] = {}
            for row in self.product_rows:
                unique[row["contract_json"]] = {
                    "contract_json": row["contract_json"]
                }
            return list(unique.values())
        if "FROM corpora" in sql:
            corpus_id = str(params[0])
            return [row.copy() for row in self.corpora if row["corpus_id"] == corpus_id]
        if "FROM runs" in sql:
            corpus_id = str(params[0])
            return [row.copy() for row in self.runs if row["corpus_id"] == corpus_id]
        raise AssertionError(f"unexpected read query: {sql}")

    def get_product(self, item_key: str, verify: bool = True):
        self.get_product_calls.append((item_key, verify))
        value = self.versions.get(item_key)
        if value is None:
            return None
        version = deepcopy(value)
        if verify:
            for artifact in version["artifacts"]:
                self.blobs.verify(artifact["blob"])
        return version

    def get_cache(self, namespace: str, key: str, include_stale: bool = False):
        assert namespace == "product"
        return deepcopy(self.cache.get(key))

    # Any accidental write path fails the test immediately.
    def initialize(self):  # pragma: no cover - called only by a regression
        self.write_calls += 1
        raise AssertionError("spectrum reads must not initialize SQLite")

    def transaction(self):  # pragma: no cover - called only by a regression
        self.write_calls += 1
        raise AssertionError("spectrum reads must not open a write transaction")

    def put_cache(self, *args, **kwargs):  # pragma: no cover
        self.write_calls += 1
        raise AssertionError("spectrum reads must not update cache state")


def _reference_document(
    run_dir: Path,
    library_root: Path,
    source_id: str,
    blob: dict[str, Any],
    version_id: str,
    *,
    status: str = "ready",
    items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if items is None:
        items = [
            {
                "identifier": source_id,
                "item_key": f"pinned-{version_id}",
                "status": status,
                "version_id": version_id,
                "artifacts": [
                    {
                        "role": "normalized",
                        "slot": "normalized:000",
                        "blob": blob,
                        "metadata": {"is_primary": True},
                    }
                ],
                "error": None if status == "ready" else "failed",
            }
        ]
    return {
        "schema_version": "2.0",
        "storage": {
            "kind": "shared_library",
            "library_root": str(library_root.resolve()),
            "relative_library_root": os.path.relpath(library_root, run_dir),
            "index_path": "index.sqlite3",
        },
        "contract": _contract(),
        "items": items,
    }


def _write_run_refs(
    corpus_root: Path,
    run_id: str,
    library_root: Path,
    source_id: str,
    blob: dict[str, Any],
    version_id: str,
    *,
    items: list[dict[str, Any]] | None = None,
) -> Path:
    run_dir = corpus_root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    document = _reference_document(
        run_dir,
        library_root,
        source_id,
        blob,
        version_id,
        items=items,
    )
    (run_dir / "product_refs.json").write_text(
        json.dumps(document),
        encoding="utf-8",
    )
    return run_dir


@pytest.fixture(autouse=True)
def _empty_parse_cache():
    _clear_parsed_spectrum_cache()
    yield
    _clear_parsed_spectrum_cache()


def test_registry_is_exact_and_dr4_products_are_not_dr3_aliases() -> None:
    assert set(SPECTRUM_ADAPTERS) == {
        ("dr3", "XP_CONTINUOUS"),
        ("dr3", "XP_SAMPLED"),
    }
    assert ("dr4", "MEAN_SPECTRUM_XP") not in SPECTRUM_ADAPTERS
    assert ("dr4", "EPOCH_SPECTRUM_XP") not in SPECTRUM_ADAPTERS


def test_global_active_version_and_corpus_pinned_version_stay_distinct(
    tmp_path: Path,
) -> None:
    library_root = tmp_path / "library"
    blobs = BlobStore(library_root)
    old_blob = _publish_sampled(tmp_path, blobs, "old.fits", SOURCE_ID, [1.0, 2.0])
    latest_blob = _publish_sampled(
        tmp_path, blobs, "latest.fits", SOURCE_ID, [10.0, 20.0]
    )
    item_key = "global-sampled"
    corpus_root = tmp_path / "corpus"
    _write_run_refs(
        corpus_root,
        "old-run",
        library_root,
        SOURCE_ID,
        old_blob,
        "version-old",
    )
    store = FakeStore(
        blobs,
        product_rows=[_row(item_key, SOURCE_ID, _contract())],
        versions={
            item_key: _version(
                item_key,
                SOURCE_ID,
                latest_blob,
                "version-current",
            )
        },
        corpora=[{"corpus_id": "corpus-id", "canonical_path": str(corpus_root)}],
        runs=[
            {
                "corpus_id": "corpus-id",
                "run_id": "old-run",
                "status": "completed",
                "started_at": 1.0,
                "finished_at": 2.0,
                "metadata_json": json.dumps(
                    {"members_indexed": True, "published": True}
                ),
            }
        ],
    )

    global_result = get_library_spectrum(store, "dr3", SOURCE_ID)
    corpus_result = get_library_spectrum(
        store,
        "dr3",
        SOURCE_ID,
        scope="corpus",
        corpus_id="corpus-id",
    )

    assert global_result["status"] == corpus_result["status"] == "ready"
    assert global_result["version_id"] == "version-current"
    assert global_result["product_type"] == "XP_SAMPLED"
    assert "fallback" in global_result["warning"].lower()
    assert corpus_result["version_id"] == "version-old"
    assert global_result["series"][0]["flux"] == [10.0, 20.0]
    assert corpus_result["series"][0]["flux"] == [1.0, 2.0]
    assert global_result["provenance"]["selection"] == "active"
    assert corpus_result["provenance"]["selection"] == "pinned"
    assert store.get_product_calls == [(item_key, True)]


def test_corpus_uses_latest_published_or_explicit_requested_run(tmp_path: Path) -> None:
    library_root = tmp_path / "library"
    blobs = BlobStore(library_root)
    old_blob = _publish_sampled(tmp_path, blobs, "old.fits", SOURCE_ID, [1.0])
    new_blob = _publish_sampled(tmp_path, blobs, "new.fits", SOURCE_ID, [2.0])
    corpus_root = tmp_path / "corpus"
    _write_run_refs(corpus_root, "old", library_root, SOURCE_ID, old_blob, "v-old")
    _write_run_refs(corpus_root, "new", library_root, SOURCE_ID, new_blob, "v-new")
    store = FakeStore(
        blobs,
        corpora=[{"corpus_id": "corpus-id", "canonical_path": str(corpus_root)}],
        runs=[
            {
                "corpus_id": "corpus-id",
                "run_id": "old",
                "status": "completed",
                "started_at": 1.0,
                "finished_at": 2.0,
                "metadata_json": json.dumps(
                    {"members_indexed": True, "published": True}
                ),
            },
            {
                "corpus_id": "corpus-id",
                "run_id": "new",
                "status": "partial",
                "started_at": 3.0,
                "finished_at": 4.0,
                "metadata_json": json.dumps(
                    {"members_indexed": True, "published": True}
                ),
            },
        ],
    )

    current = get_library_spectrum(
        store, "dr3", SOURCE_ID, scope="corpus", corpus_id="corpus-id"
    )
    explicit = get_library_spectrum(
        store,
        "dr3",
        SOURCE_ID,
        scope="corpus",
        corpus_id="corpus-id",
        run_id="old",
    )

    assert current["version_id"] == "v-new"
    assert current["provenance"]["run_id"] == "new"
    assert explicit["version_id"] == "v-old"
    assert explicit["provenance"]["run_id"] == "old"
    assert explicit["provenance"]["explicit_run"] is True


def test_corpus_manifest_active_run_overrides_newer_published_db_row(
    tmp_path: Path,
) -> None:
    library_root = tmp_path / "library"
    blobs = BlobStore(library_root)
    old_blob = _publish_sampled(tmp_path, blobs, "old-pin.fits", SOURCE_ID, [1.0])
    new_blob = _publish_sampled(tmp_path, blobs, "newer-db.fits", SOURCE_ID, [9.0])
    corpus_root = tmp_path / "corpus"
    _write_run_refs(corpus_root, "old", library_root, SOURCE_ID, old_blob, "v-old")
    _write_run_refs(corpus_root, "new", library_root, SOURCE_ID, new_blob, "v-new")
    (corpus_root / "manifest.json").write_text(
        json.dumps(
            {
                "storage": {"kind": "shared_library"},
                "active_run_id": "old",
            }
        ),
        encoding="utf-8",
    )
    runs = [
        {
            "corpus_id": "corpus-id",
            "run_id": "old",
            "status": "partial",
            "started_at": 1.0,
            "finished_at": 2.0,
            "metadata_json": json.dumps(
                {"members_indexed": True, "published": True}
            ),
        },
        {
            "corpus_id": "corpus-id",
            "run_id": "new",
            "status": "completed",
            "started_at": 3.0,
            "finished_at": 4.0,
            "metadata_json": json.dumps(
                {"members_indexed": True, "published": True}
            ),
        },
    ]
    store = FakeStore(
        blobs,
        corpora=[{"corpus_id": "corpus-id", "canonical_path": str(corpus_root)}],
        runs=runs,
    )

    pinned = get_library_spectrum(
        store, "dr3", SOURCE_ID, scope="corpus", corpus_id="corpus-id"
    )

    assert pinned["status"] == "ready"
    assert pinned["version_id"] == "v-old"
    assert pinned["series"][0]["flux"] == [1.0]
    assert pinned["provenance"]["run_id"] == "old"

    # The manifest/SQLite publication disagreement fails closed rather than
    # silently switching to the newer database row or a global product.
    runs[0]["metadata_json"] = json.dumps(
        {"members_indexed": True, "published": False}
    )
    store.runs = runs
    disagreed = get_library_spectrum(
        store, "dr3", SOURCE_ID, scope="corpus", corpus_id="corpus-id"
    )
    assert disagreed["status"] == "unknown"
    assert disagreed["series"] == []


def test_missing_corpus_pin_never_substitutes_global_current(tmp_path: Path) -> None:
    library_root = tmp_path / "library"
    blobs = BlobStore(library_root)
    blob = _publish_sampled(tmp_path, blobs, "global.fits", SOURCE_ID, [5.0])
    item_key = "global-sampled"
    corpus_root = tmp_path / "corpus"
    _write_run_refs(
        corpus_root,
        "run",
        library_root,
        SOURCE_ID,
        blob,
        "unused",
        items=[],
    )
    store = FakeStore(
        blobs,
        product_rows=[_row(item_key, SOURCE_ID, _contract())],
        versions={item_key: _version(item_key, SOURCE_ID, blob, "global-current")},
        corpora=[{"corpus_id": "corpus-id", "canonical_path": str(corpus_root)}],
        runs=[
            {
                "corpus_id": "corpus-id",
                "run_id": "run",
                "status": "completed",
                "started_at": 1.0,
                "finished_at": 2.0,
                "metadata_json": json.dumps(
                    {"members_indexed": True, "published": True}
                ),
            }
        ],
    )

    result = get_library_spectrum(
        store, "dr3", SOURCE_ID, scope="corpus", corpus_id="corpus-id"
    )

    assert result["status"] == "not_downloaded"
    assert result["series"] == []
    assert store.get_product_calls == []


def test_payload_has_documented_units_one_combined_series_and_json_missing_values(
    tmp_path: Path,
) -> None:
    blobs = BlobStore(tmp_path / "library")
    blob = _publish_sampled(
        tmp_path,
        blobs,
        "finite.fits",
        SOURCE_ID,
        [1.5, np.nan],
        wavelength=[400.0, 500.0],
        flux_error=[0.2, np.inf],
    )
    item_key = "sampled"
    store = FakeStore(
        blobs,
        product_rows=[_row(item_key, SOURCE_ID, _contract())],
        versions={item_key: _version(item_key, SOURCE_ID, blob, "v1")},
    )

    result = get_library_spectrum(
        store, "dr3", SOURCE_ID, retrieval_type="XP_SAMPLED"
    )

    assert result["status"] == "ready"
    assert result["wavelength_unit"] == "nm"
    assert result["flux_unit"] == DR3_XP_FLUX_UNIT
    assert result["quality"] == {}
    assert result["series"] == [
        {
            "name": "XP combined",
            "wavelength": [400.0, 500.0],
            "flux": [1.5, None],
            "flux_error": [0.2, None],
        }
    ]
    assert len(result["series"]) == 1
    assert "BP" not in result["series"][0]["name"]
    assert "RP" not in result["series"][0]["name"]
    assert str(tmp_path) not in json.dumps(result)


@pytest.mark.parametrize(
    ("state", "active_version_id", "expected"),
    [
        ("pending", None, "pending"),
        ("failed", None, "failed"),
        ("absent", None, "unavailable"),
        ("mystery", None, "unknown"),
    ],
)
def test_global_product_states_are_reported_without_writes(
    tmp_path: Path,
    state: str,
    active_version_id: str | None,
    expected: str,
) -> None:
    blobs = BlobStore(tmp_path / "library")
    store = FakeStore(
        blobs,
        product_rows=[
            _row(
                "item",
                SOURCE_ID,
                _contract(),
                state=state,
                active_version_id=active_version_id,
            )
        ],
    )

    result = get_library_spectrum(
        store, "dr3", SOURCE_ID, retrieval_type="XP_SAMPLED"
    )

    assert result["status"] == expected
    assert store.write_calls == 0


def test_missing_deleted_corrupt_and_unknown_products_fail_closed(tmp_path: Path) -> None:
    blobs = BlobStore(tmp_path / "library")
    missing = get_library_spectrum(FakeStore(blobs), "dr3", SOURCE_ID)
    assert missing["status"] == "not_downloaded"

    deleted_store = FakeStore(
        blobs,
        product_rows=[_row("deleted", SOURCE_ID, _contract())],
        versions={"deleted": None},
    )
    deleted = get_library_spectrum(
        deleted_store, "dr3", SOURCE_ID, retrieval_type="XP_SAMPLED"
    )
    assert deleted["status"] == "corrupt"

    blob = _publish_sampled(tmp_path, blobs, "corrupt.fits", SOURCE_ID, [1.0])
    corrupt_path = blobs.path(blob)
    corrupt_path.chmod(0o644)
    corrupt_path.write_bytes(b"not-fits")
    corrupt_store = FakeStore(
        blobs,
        product_rows=[_row("corrupt", SOURCE_ID, _contract())],
        versions={"corrupt": _version("corrupt", SOURCE_ID, blob, "v-corrupt")},
    )
    corrupt = get_library_spectrum(
        corrupt_store, "dr3", SOURCE_ID, retrieval_type="XP_SAMPLED"
    )
    assert corrupt["status"] == "corrupt"

    unknown = get_library_spectrum(
        FakeStore(blobs),
        "dr3",
        SOURCE_ID,
        retrieval_type="MEAN_SPECTRUM_XP",
    )
    assert unknown["status"] == "unsupported"
    assert unknown["product_type"] == "MEAN_SPECTRUM_XP"


def test_shared_blob_integrity_failure_is_seen_by_every_product(tmp_path: Path) -> None:
    blobs = BlobStore(tmp_path / "library")
    shared = _publish_sampled(tmp_path, blobs, "shared.fits", SOURCE_ID, [1.0])
    other_id = "987654321"
    rows = [
        _row("first", SOURCE_ID, _contract()),
        _row("second", other_id, _contract()),
    ]
    store = FakeStore(
        blobs,
        product_rows=rows,
        versions={
            "first": _version("first", SOURCE_ID, shared, "v1"),
            "second": _version("second", other_id, shared, "v2"),
        },
    )
    path = blobs.path(shared)
    path.chmod(0o644)
    path.write_bytes(b"broken-shared-object")

    first = get_library_spectrum(
        store, "dr3", SOURCE_ID, retrieval_type="XP_SAMPLED"
    )
    second = get_library_spectrum(
        store, "dr3", other_id, retrieval_type="XP_SAMPLED"
    )

    assert first["status"] == second["status"] == "corrupt"


def test_same_numeric_id_is_release_qualified_and_dr4_has_no_parser(
    tmp_path: Path,
) -> None:
    blobs = BlobStore(tmp_path / "library")
    blob = _publish_sampled(tmp_path, blobs, "dr3.fits", SOURCE_ID, [3.0])
    store = FakeStore(
        blobs,
        product_rows=[
            _row("dr3", SOURCE_ID, _contract(release="dr3")),
            _row(
                "dr4",
                SOURCE_ID,
                _contract("MEAN_SPECTRUM_XP", release="dr4"),
                active_version_id="dr4-version",
            ),
        ],
        versions={"dr3": _version("dr3", SOURCE_ID, blob, "dr3-version")},
    )

    dr3 = get_library_spectrum(
        store, "dr3", SOURCE_ID, retrieval_type="XP_SAMPLED"
    )
    dr4 = get_library_spectrum(store, "dr4", SOURCE_ID)

    assert dr3["status"] == "ready"
    assert dr3["release"] == "dr3"
    assert dr4["status"] == "unsupported"
    assert dr4["release"] == "dr4"
    assert dr4["series"] == []
    assert store.get_product_calls == [("dr3", True)]


def test_parser_cache_hits_without_network_or_db_writes_and_invalidates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    blobs = BlobStore(tmp_path / "library")
    first_blob = _publish_sampled(tmp_path, blobs, "first.fits", SOURCE_ID, [1.0])
    second_blob = _publish_sampled(tmp_path, blobs, "second.fits", SOURCE_ID, [2.0])
    item_key = "sampled"
    row = _row(item_key, SOURCE_ID, _contract(), active_version_id="v1")
    store = FakeStore(
        blobs,
        product_rows=[row],
        versions={item_key: _version(item_key, SOURCE_ID, first_blob, "v1")},
    )
    real_reader = spectra.read_xp_sampled
    parser_calls: list[Path] = []

    def counted_reader(path):
        parser_calls.append(Path(path))
        return real_reader(path)

    monkeypatch.setattr(spectra, "read_xp_sampled", counted_reader)

    first = get_library_spectrum(
        store, "dr3", SOURCE_ID, retrieval_type="XP_SAMPLED"
    )
    cached = get_library_spectrum(
        store, "dr3", SOURCE_ID, retrieval_type="XP_SAMPLED"
    )
    assert first["provenance"]["parse_cache"] == "miss"
    assert cached["provenance"]["parse_cache"] == "hit"
    assert len(parser_calls) == 1

    row["active_version_id"] = "v2"
    store.versions[item_key] = _version(item_key, SOURCE_ID, first_blob, "v2")
    new_version = get_library_spectrum(
        store, "dr3", SOURCE_ID, retrieval_type="XP_SAMPLED"
    )
    assert new_version["provenance"]["parse_cache"] == "miss"
    assert new_version["version_id"] == "v2"
    assert new_version["series"][0]["flux"] == [1.0]
    assert len(parser_calls) == 2

    # A changed artifact hash also invalidates parsing even if malformed metadata
    # were to reuse a version identifier.
    store.versions[item_key] = _version(item_key, SOURCE_ID, second_blob, "v2")
    refreshed = get_library_spectrum(
        store, "dr3", SOURCE_ID, retrieval_type="XP_SAMPLED"
    )

    assert refreshed["provenance"]["parse_cache"] == "miss"
    assert refreshed["version_id"] == "v2"
    assert refreshed["series"][0]["flux"] == [2.0]
    assert len(parser_calls) == 3
    assert store.write_calls == 0
    assert all(verify is True for _, verify in store.get_product_calls)


def test_failed_refresh_warning_keeps_old_ready_version(tmp_path: Path) -> None:
    blobs = BlobStore(tmp_path / "library")
    blob = _publish_sampled(tmp_path, blobs, "old-ready.fits", SOURCE_ID, [4.0])
    item_key = "sampled"
    store = FakeStore(
        blobs,
        product_rows=[
            _row(
                item_key,
                SOURCE_ID,
                _contract(),
                active_version_id="old-ready",
                last_error=f"failed while staging {tmp_path / 'secret.fits'}",
            )
        ],
        versions={item_key: _version(item_key, SOURCE_ID, blob, "old-ready")},
    )

    result = get_library_spectrum(
        store, "dr3", SOURCE_ID, retrieval_type="XP_SAMPLED"
    )

    assert result["status"] == "ready"
    assert "latest refresh failed" in result["warning"].lower()
    assert str(tmp_path) not in json.dumps(result)


def test_invalid_arguments_raise_value_error() -> None:
    with pytest.raises(ValueError):
        get_library_spectrum(None, "dr5", SOURCE_ID)
    with pytest.raises(ValueError):
        get_library_spectrum(None, "dr3", "not-a-source")
    with pytest.raises(ValueError):
        get_library_spectrum(None, "dr3", SOURCE_ID, scope="dataset")
    with pytest.raises(ValueError):
        get_library_spectrum(None, "dr3", SOURCE_ID, scope="corpus")
    with pytest.raises(ValueError):
        get_library_spectrum(None, "dr3", SOURCE_ID, corpus_id="unexpected")
