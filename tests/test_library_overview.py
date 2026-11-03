from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from astro_pipeline.library.identity import product_item_key, source_cache_key
from astro_pipeline.library.overview import (
    LibraryOverview,
    SnapshotChanged,
    SourceFilters,
)
from astro_pipeline.library.store import LibraryStore


class Clock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self) -> None:
        self.value += 1.0


def _finish_published(
    store: LibraryStore,
    clock: Clock,
    corpus_id: str,
    members: list[dict],
    *,
    published: bool = True,
    status: str = "complete",
) -> str:
    run_id = store.start_run(corpus_id)
    store.index_members(run_id, members)
    store.finish_run(
        run_id,
        status,
        metadata={"members_indexed": True, "published": published},
    )
    clock.advance()
    return run_id


def _source(source_id: int | str, *, ra: float | None = None) -> dict:
    value = {
        "source_id": str(source_id),
        "gaia_release": "dr3",
        "Plx_gdr3": 4.5,
        "pmRA_gdr3": 1.2,
        "pmDE_gdr3": -2.3,
        "Gmag_gdr3": 12.0,
        "RUWE_gdr3": 1.01,
    }
    if ra is not None:
        value.update({"RAICRS": ra, "DEICRS": -20.0 + (ra % 40.0)})
    return value


def _save_xp_product(store: LibraryStore, tmp_path: Path, source_id: str) -> None:
    path = tmp_path / f"{source_id}.fits"
    path.write_bytes(b"spectrum")
    blob = store.blobs.publish(path, content_type="application/fits")
    contract = {
        "release": "dr3",
        "retrieval_type": "XP_SAMPLED",
        "linking_parameter": "SOURCE_ID",
        "data_structure": "INDIVIDUAL",
        "output_format": "fits",
    }
    item_key = product_item_key(contract, source_id)
    lease = store.acquire_lease("product", item_key)
    assert lease is not None
    store.save_product(
        contract,
        source_id,
        [
            {
                "blob": blob,
                "role": "normalized",
                "slot": "spectrum",
                "metadata": {"source_path": str(path), "format": "fits"},
            }
        ],
        lease,
    )
    store.release_lease(lease)


def test_absent_overview_never_creates_database_or_directories(tmp_path):
    root = tmp_path / "missing-library"
    overview = LibraryOverview(LibraryStore(root))

    meta = overview.meta()
    sources = overview.sources()
    sky = overview.sky()
    detail = overview.source("dr3", "123")
    overview.close()

    assert meta["exists"] is False
    assert sources["sources"] == []
    assert sky["points"] == []
    assert detail["exists"] is False
    assert not root.exists()


def test_source_not_found_preserves_cached_availability(tmp_path):
    store = LibraryStore(tmp_path / "library")
    observation_id = store.put_cache(
        "gaia-xp-availability:v1",
        source_cache_key("dr3", "404"),
        {
            "source_id": "404",
            "gaia_source_found": False,
            "has_xp_sampled": False,
            "has_xp_continuous": False,
        },
        outcome="not_found",
    )
    overview = LibraryOverview(store)

    result = overview.source("dr3", "404")

    assert result["resolved"] is False
    assert result["xp_availability"]["id"] == observation_id
    overview.close()


def test_global_keyset_pages_include_over_500_sources_and_both_releases(tmp_path):
    clock = Clock()
    store = LibraryStore(tmp_path / "library", clock=clock)
    corpus_id = store.register_corpus(tmp_path / "corpus", "large")
    members = [_source(index, ra=float(index % 360)) for index in range(1, 602)]
    members.extend(
        [
            {
                "source_id": "42",
                "gaia_release": "dr4",
                # DR3 aliases must not be guessed for a DR4 source.
                "RAICRS": 123.0,
                "DEICRS": 45.0,
                "Plx_gdr3": 99.0,
            },
            _source("999999"),
        ]
    )
    _finish_published(store, clock, corpus_id, members)
    _save_xp_product(store, tmp_path, "1")

    overview = LibraryOverview(store)
    first = overview.sources(limit=500)
    second = overview.sources(
        limit=500,
        cursor=first["next_cursor"],
        revision=first["revision"],
    )
    sky = overview.sky(limit=500)
    meta = overview.meta()
    detail = overview.source("dr3", "1")

    all_sources = first["sources"] + second["sources"]
    keys = [source["key"] for source in all_sources]
    assert first["total"] == 603
    assert first["returned"] == 500
    assert first["has_more"] is True
    assert second["returned"] == 103
    assert second["has_more"] is False
    assert len(keys) == len(set(keys)) == 603
    assert "dr3:42" in keys and "dr4:42" in keys
    assert all(isinstance(source["source_id"], str) for source in all_sources)

    dr4 = next(source for source in all_sources if source["key"] == "dr4:42")
    assert dr4["position"]["valid"] is False
    assert dr4["astrometry"]["parallax_mas"] is None

    assert first["registered_xp_ready_sources"] == 1
    assert sky["total_sources"] == 603
    assert sky["total_positions"] == 601
    assert sky["missing_positions"] == 2
    assert all(
        not (point["ra_deg"] == 0.0 and point["dec_deg"] == 0.0)
        for point in sky["points"]
        if point["source_id"] == "999999"
    )
    assert not any(point["source_id"] == "999999" for point in sky["points"])
    assert meta["total_sources"] == 603
    assert meta["registered_xp_ready_sources"] == 1
    assert all("path" not in dataset for dataset in meta["datasets"])
    artifact = detail["active_products"][0]["artifacts"][0]
    assert "relative_path" not in artifact["blob"]
    assert artifact["metadata"] == {"format": "fits"}
    assert all("path" not in dataset for dataset in detail["datasets"])
    overview.close()


def test_current_pending_and_historical_runs_are_kept_separate(tmp_path):
    clock = Clock()
    store = LibraryStore(tmp_path / "library", clock=clock)
    corpus_id = store.register_corpus(tmp_path / "corpus", "sample")

    historical = _finish_published(
        store, clock, corpus_id, [_source("1", ra=1.0)]
    )
    current = _finish_published(
        store, clock, corpus_id, [_source("2", ra=2.0)]
    )
    pending = _finish_published(
        store,
        clock,
        corpus_id,
        [_source("1", ra=1.0)],
        published=False,
        status="failed",
    )

    overview = LibraryOverview(store)
    source_one = overview.source("dr3", "1")
    source_two = overview.source("dr3", "2")
    current_scope = overview.sources(
        filters=SourceFilters.create(scope="current_datasets")
    )
    meta = overview.meta()

    assert source_one["datasets"] == []
    assert {
        (dataset["run_id"], dataset["snapshot_state"])
        for dataset in source_one["working_datasets"]
    } == {(historical, "historical"), (pending, "pending")}
    assert [dataset["run_id"] for dataset in source_two["datasets"]] == [current]
    assert [source["key"] for source in current_scope["sources"]] == ["dr3:2"]
    run_states = {run["run_id"]: run["snapshot_state"] for run in meta["runs"]}
    assert run_states == {
        historical: "historical",
        current: "current",
        pending: "pending",
    }
    overview.close()


@pytest.mark.parametrize("pin_field", ["active_run_id", "snapshot_run_id"])
def test_current_dataset_uses_manifest_pin_not_newest_timestamp(tmp_path, pin_field):
    clock = Clock()
    store = LibraryStore(tmp_path / "library", clock=clock)
    corpus = tmp_path / "corpus"
    corpus_id = store.register_corpus(corpus, "sample")
    pinned = _finish_published(store, clock, corpus_id, [_source("1", ra=1.0)])
    newer = _finish_published(store, clock, corpus_id, [_source("2", ra=2.0)])
    (corpus / "runs" / pinned).mkdir(parents=True)
    (corpus / "runs" / newer).mkdir()
    (corpus / "manifest.json").write_text(json.dumps({
        "storage": {"kind": "shared_library"},
        pin_field: pinned,
    }))

    overview = LibraryOverview(store)
    try:
        assert overview.meta()["datasets"][0]["current_run_id"] == pinned
        filters = SourceFilters.create(scope="current_datasets")
        assert [row["source_id"] for row in overview.sources(filters=filters)["sources"]] == ["1"]
        assert [row["source_id"] for row in overview.sky(filters=filters)["points"]] == ["1"]
        assert overview.source("dr3", "1")["datasets"][0]["run_id"] == pinned
        assert overview.source("dr3", "2")["datasets"] == []
    finally:
        overview.close()


@pytest.mark.parametrize("invalid_pin", ["missing", "absent_directory", "unregistered", "unpublished", "malformed"])
def test_invalid_shared_manifest_pin_never_falls_back_to_latest_run(tmp_path, invalid_pin):
    clock = Clock()
    store = LibraryStore(tmp_path / "library", clock=clock)
    corpus = tmp_path / "corpus"
    corpus_id = store.register_corpus(corpus, "sample")
    published = _finish_published(store, clock, corpus_id, [_source("1", ra=1.0)])
    unpublished = _finish_published(
        store, clock, corpus_id, [_source("2", ra=2.0)], published=False,
    )
    (corpus / "runs" / published).mkdir(parents=True)
    manifest = {"storage": {"kind": "shared_library"}}
    if invalid_pin == "absent_directory":
        manifest["active_run_id"] = published
        (corpus / "runs" / published).rmdir()
    elif invalid_pin == "unregistered":
        manifest["active_run_id"] = "unregistered-run"
        (corpus / "runs" / "unregistered-run").mkdir()
    elif invalid_pin == "unpublished":
        manifest["active_run_id"] = unpublished
        (corpus / "runs" / unpublished).mkdir()
    elif invalid_pin == "malformed":
        manifest["active_run_id"] = "../outside"
    (corpus / "manifest.json").write_text(json.dumps(manifest))

    overview = LibraryOverview(store)
    try:
        assert overview.meta()["datasets"] == []
        assert overview.sources(filters=SourceFilters.create(scope="current_datasets"))["sources"] == []
        assert overview.source("dr3", "1")["datasets"] == []
        assert overview.sources()["total"] == 2
    finally:
        overview.close()


def test_manifest_repin_invalidates_page_revision_without_sqlite_write(tmp_path):
    clock = Clock()
    store = LibraryStore(tmp_path / "library", clock=clock)
    corpus = tmp_path / "corpus"
    corpus_id = store.register_corpus(corpus, "sample")
    first_run = _finish_published(store, clock, corpus_id, [_source("1"), _source("2")])
    second_run = _finish_published(store, clock, corpus_id, [_source("3")])
    (corpus / "runs" / first_run).mkdir(parents=True)
    (corpus / "runs" / second_run).mkdir()
    manifest = {"storage": {"kind": "shared_library"}, "active_run_id": first_run}
    (corpus / "manifest.json").write_text(json.dumps(manifest))
    overview = LibraryOverview(store)
    try:
        filters = SourceFilters.create(scope="current_datasets")
        first_page = overview.sources(filters=filters, limit=1)
        manifest["active_run_id"] = second_run
        (corpus / "manifest.json").write_text(json.dumps(manifest))
        with pytest.raises(SnapshotChanged):
            overview.sources(
                filters=filters, limit=1, cursor=first_page["next_cursor"],
                revision=first_page["revision"],
            )
        assert overview.meta()["datasets"][0]["current_run_id"] == second_run
    finally:
        overview.close()


def test_missing_coordinates_remain_records_and_never_become_zero_zero(tmp_path):
    clock = Clock()
    store = LibraryStore(tmp_path / "library", clock=clock)
    corpus_id = store.register_corpus(tmp_path / "corpus", "coordinates")
    _finish_published(
        store,
        clock,
        corpus_id,
        [_source("1", ra=10.0), _source("2"), _source("3", ra=0.0)],
    )

    overview = LibraryOverview(store)
    sources = overview.sources()
    sky = overview.sky()

    missing = next(source for source in sources["sources"] if source["source_id"] == "2")
    assert missing["position"] == {
        "ra_deg": None,
        "dec_deg": None,
        "frame": "ICRS",
        "epoch": None,
        "epoch_status": "unavailable",
        "valid": False,
    }
    assert sources["total"] == 3
    assert sky["total_sources"] == 3
    assert sky["total_positions"] == 2
    assert sky["missing_positions"] == 1
    assert {point["source_id"] for point in sky["points"]} == {"1", "3"}
    overview.close()


def test_unverified_legacy_coordinates_plot_without_assumed_reference_epoch(tmp_path):
    clock = Clock()
    store = LibraryStore(tmp_path / "library", clock=clock)
    corpus_id = store.register_corpus(
        tmp_path / "legacy", "legacy", provenance_status="legacy_unverified"
    )
    run_id = store.start_run(corpus_id)
    store.index_members(
        run_id,
        [_source("1", ra=10.0)],
        verified=False,
    )
    store.finish_run(
        run_id,
        "complete",
        metadata={"members_indexed": True, "published": True},
    )

    overview = LibraryOverview(store)
    source = overview.sources()["sources"][0]
    sky = overview.sky()

    assert source["position"]["valid"] is True
    assert source["position"]["epoch"] is None
    assert source["position"]["epoch_status"] == "legacy_or_unverified"
    assert sky["points"][0]["source_id"] == "1"
    overview.close()


def test_page_revision_rejects_concurrent_sqlite_change(tmp_path):
    clock = Clock()
    store = LibraryStore(tmp_path / "library", clock=clock)
    corpus_id = store.register_corpus(tmp_path / "corpus", "sample")
    _finish_published(
        store,
        clock,
        corpus_id,
        [_source("1", ra=1.0), _source("2", ra=2.0), _source("3", ra=3.0)],
    )
    overview = LibraryOverview(store)
    first = overview.sources(limit=1)

    # A separate SQLite connection commits after page one.  The persistent
    # monitor connection must observe this via PRAGMA data_version.
    store.put_source_references("dr3", "9", [])

    with pytest.raises(SnapshotChanged):
        overview.sources(
            limit=1,
            cursor=first["next_cursor"],
            revision=first["revision"],
        )
    overview.close()


def test_atomic_database_replacement_starts_new_revision_generation(tmp_path):
    clock = Clock()
    store = LibraryStore(tmp_path / "library", clock=clock)
    corpus_id = store.register_corpus(tmp_path / "corpus", "original")
    _finish_published(store, clock, corpus_id, [_source("1", ra=1.0)])
    overview = LibraryOverview(store)
    original = overview.sources()

    replacement_store = LibraryStore(tmp_path / "replacement", clock=clock)
    replacement_corpus = replacement_store.register_corpus(
        tmp_path / "other-corpus", "replacement"
    )
    _finish_published(
        replacement_store,
        clock,
        replacement_corpus,
        [_source("9", ra=9.0)],
    )
    replacement_db = tmp_path / "replacement.sqlite3"
    replacement_store.backup(replacement_db)
    displaced = tmp_path / "original.sqlite3"
    if os.name == "nt":
        # Windows requires closing live SQLite readers before restoring a file.
        # The reopened monitor must still detect the changed database identity.
        overview.close()
    os.replace(store.db_path, displaced)
    os.replace(replacement_db, store.db_path)

    current = overview.sources()
    assert current["revision"] != original["revision"]
    assert [source["source_id"] for source in current["sources"]] == ["9"]
    with pytest.raises(SnapshotChanged):
        overview.sources(revision=original["revision"])
    overview.close()


def test_source_pages_use_fixed_query_count_and_sql_keyset_not_offset(
    tmp_path, monkeypatch
):
    clock = Clock()
    store = LibraryStore(tmp_path / "library", clock=clock)
    corpus_id = store.register_corpus(tmp_path / "corpus", "sample")
    _finish_published(
        store,
        clock,
        corpus_id,
        [_source(index, ra=float(index)) for index in range(1, 21)],
    )

    original_connect = store._connect
    traces: list[list[str]] = []

    def traced_connect(*, readonly: bool):
        connection = original_connect(readonly=readonly)
        statements: list[str] = []
        traces.append(statements)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(store, "_connect", traced_connect)
    overview = LibraryOverview(store)
    overview.sources(limit=1)
    overview.sources(limit=20)
    overview.close()

    query_traces = [
        [
            statement
            for statement in statements
            if statement.lstrip().upper().startswith(("SELECT", "WITH"))
        ]
        for statements in traces
    ]
    assert [len(statements) for statements in query_traces] == [4, 4]
    assert not any(
        " OFFSET " in f" {statement.upper()} "
        for statements in query_traces
        for statement in statements
    )


def test_archive_available_without_local_product_is_not_downloaded(tmp_path):
    clock = Clock()
    store = LibraryStore(tmp_path / "library", clock=clock)
    corpus_id = store.register_corpus(tmp_path / "corpus", "sample")
    member = _source("1", ra=1.0)
    member["has_xp_sampled"] = True
    _finish_published(store, clock, corpus_id, [member, _source("2", ra=2.0)])

    overview = LibraryOverview(store)
    result = overview.sources(
        filters=SourceFilters.create(spectrum_state="not_downloaded")
    )
    alias = overview.sources(
        filters=SourceFilters.create(spectrum_state="available")
    )

    assert [source["key"] for source in result["sources"]] == ["dr3:1"]
    assert result["sources"][0]["spectrum_status"] == "not_downloaded"
    assert alias["sources"] == result["sources"]
    overview.close()


def test_cursor_is_bound_to_filters_and_requires_revision(tmp_path):
    clock = Clock()
    store = LibraryStore(tmp_path / "library", clock=clock)
    corpus_id = store.register_corpus(tmp_path / "corpus", "sample")
    _finish_published(
        store, clock, corpus_id, [_source("1", ra=1), _source("2", ra=2)]
    )
    overview = LibraryOverview(store)
    first = overview.sources(limit=1)

    with pytest.raises(ValueError, match="revision"):
        overview.sources(limit=1, cursor=first["next_cursor"])
    with pytest.raises(ValueError, match="filters"):
        overview.sources(
            filters=SourceFilters.create(release="dr3"),
            limit=1,
            cursor=first["next_cursor"],
            revision=first["revision"],
        )
    overview.close()


@pytest.fixture
def named_catalogue(tmp_path):
    clock = Clock()
    store = LibraryStore(tmp_path / "library", clock=clock)
    corpus_path = tmp_path / "twa"
    corpus = store.register_corpus(corpus_path, "twa_pilot")

    def publish(name, *, published=True):
        run = store.start_run(corpus)
        store.index_members(run, [{**_source("1", ra=10), "input_row_id": "42"}],
                            input_rows=[{"input_row_id": "42", "raw": {"Name": name}}])
        store.finish_run(run, "completed", metadata={"members_indexed": True, "published": published})
        clock.advance()
        return run

    previous = publish("Retired Name")
    current = store.start_run(corpus)
    store.index_members(current, [
        {**_source("1", ra=10), "input_row_id": "42"},
        {**_source("1", ra=10), "input_row_id": "43"},
        {**_source("2", ra=20), "input_row_id": "46"},
        {**_source("3", ra=30), "input_row_id": "44"},
    ], input_rows=[
        # Exercise the JSON-string representation used by real membership runs.
        {"input_row_id": "42", "raw_json": json.dumps({"Name": "TWA 31"})},
        {"input_row_id": "43", "raw": {"Name": "TYC 123"}},
        {"input_row_id": "44", "raw": {"Name": "Odd_% Name"}},
        {"input_row_id": "45", "status": "unmatched", "raw": {"Name": "TWA 999"}},
        {"input_row_id": "46", "raw": {"member_Name": "TWA 7"}},
    ])
    store.finish_run(current, "completed", metadata={"members_indexed": True, "published": True})
    clock.advance()
    publish("Draft Name", published=False)
    other = store.register_corpus(tmp_path / "other", "control")
    _finish_published(store, clock, other, [{"gaia_release": "dr4", "source_id": "1", "ra": 20, "dec": 30}])
    return store, corpus_path, previous, current


@pytest.mark.parametrize(("query", "expected"), [
    ("TWA", {"1", "2", "3"}),  # Also matches the current sample label.
    ("TWA31", {"1"}), ("tWa  31", {"1"}), ("TWA\u00a031", {"1"}),
    ("TYC123", {"1"}), ("TWA7", {"2"}), ("Odd_%", {"3"}),
    ("Gaia DR3 1", {"1"}), ("Retired Name", set()),
    ("Draft Name", set()), ("TWA999", set()),
])
def test_searches_current_catalogue_names_and_labels_consistently(named_catalogue, query, expected):
    store, _, _, _ = named_catalogue
    overview = LibraryOverview(store)
    try:
        filters = SourceFilters.create(q=query)
        rows = overview.sources(filters=filters)
        sky = overview.sky(filters=filters)
        assert {(s["release"], s["source_id"]) for s in rows["sources"]} == {("dr3", key) for key in expected}
        assert {(s["release"], s["source_id"]) for s in sky["points"]} == {("dr3", key) for key in expected}
        assert rows["total"] == sky["total_sources"] == len(expected)
    finally:
        overview.close()


def test_catalogue_name_provenance_and_manifest_pin(named_catalogue):
    store, path, previous, current = named_catalogue
    original = store.read("SELECT raw_json FROM input_rows WHERE run_id = ? AND input_row_id = '42'", (current,))[0]["raw_json"]
    overview = LibraryOverview(store)
    try:
        source = overview.source("dr3", "1")["dto"]
        assert source["name"] == "TWA 31"
        assert {n["name"] for n in source["catalog_names"]} == {"TWA 31", "TYC 123"}
        name = next(n for n in source["catalog_names"] if n["name"] == "TWA 31")
        assert (name["field"], name["input_row_id"], name["run_id"]) == ("Name", "42", current)
        assert overview.source("dr4", "1")["dto"]["catalog_names"] == []
        path.mkdir(exist_ok=True)
        (path / "manifest.json").write_text(json.dumps({"snapshot_run_id": previous}), encoding="utf-8")
        assert overview.sources(filters=SourceFilters.create(q="TWA31"))["total"] == 0
        assert overview.source("dr3", "1")["dto"]["name"] == "Retired Name"
        assert store.read("SELECT raw_json FROM input_rows WHERE run_id = ? AND input_row_id = '42'", (current,))[0]["raw_json"] == original
    finally:
        overview.close()
