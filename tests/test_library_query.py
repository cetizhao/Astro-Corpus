"""Tests for read-only global library queries and MCP wrappers."""
from __future__ import annotations

from pathlib import Path

from astro_pipeline.library.identity import product_item_key, source_cache_key
from astro_pipeline.library.query import Library
from astro_pipeline.library.store import LibraryStore


class Clock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float = 1.0) -> None:
        self.value += seconds


def _published_run(
    store: LibraryStore,
    clock: Clock,
    corpus_path: Path,
    label: str,
    members: list[dict],
    *,
    legacy: bool = False,
) -> str:
    corpus_id = store.register_corpus(
        corpus_path,
        label,
        provenance_status="legacy_unverified" if legacy else "verified",
    )
    run_id = store.start_run(corpus_id)
    store.index_members(run_id, members, verified=not legacy)
    store.finish_run(
        run_id,
        "complete",
        metadata={
            "members_indexed": True,
            "published": True,
            "legacy_unverified": legacy,
        },
    )
    clock.advance()
    return run_id


def test_absent_library_queries_and_status_do_not_create_files(tmp_path):
    root = tmp_path / "library"
    service = Library(LibraryStore(root))

    assert service.status()["exists"] is False
    source = service.lookup_source("123")
    paper = service.lookup_paper("2026A&A...1A")
    assert source["exists"] is False and source["scope"] == "global_library"
    assert paper["exists"] is False and paper["scope"] == "global_library"
    assert not root.exists()

    disabled = Library(None).status()
    assert disabled["exists"] is False
    assert disabled["enabled"] is False


def test_source_not_found_still_exposes_cached_availability(tmp_path):
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

    result = Library(store).lookup_source("404")

    assert result["resolved"] is False
    assert result["reason"] == "source_not_found"
    assert result["xp_availability"]["id"] == observation_id
    assert result["active_products"] == []


def test_source_query_uses_latest_published_run_per_dataset(tmp_path):
    clock = Clock()
    store = LibraryStore(tmp_path / "library", clock=clock)
    corpus_a = tmp_path / "a"
    corpus_b = tmp_path / "b"
    corpus_c = tmp_path / "c"

    old_a = _published_run(
        store,
        clock,
        corpus_a,
        "alpha",
        [{"source_id": "123", "gaia_release": "dr3", "ra": 1.0}],
    )
    # The latest published alpha snapshot removes 123. A source-first join must
    # not accidentally resurrect old_a as alpha's current association.
    current_a = _published_run(
        store,
        clock,
        corpus_a,
        "alpha",
        [{"source_id": "999", "gaia_release": "dr3", "ra": 9.0}],
    )
    current_b = _published_run(
        store,
        clock,
        corpus_b,
        "beta",
        [
            {
                "source_id": "123",
                "gaia_release": "dr3",
                "ra": 1.1,
                "member_ordinal": 0,
            },
            {
                "source_id": "123",
                "gaia_release": "dr3",
                "ra": 1.1,
                "member_ordinal": 1,
            },
        ],
        legacy=True,
    )
    current_c = _published_run(
        store,
        clock,
        corpus_c,
        "gamma",
        [{"source_id": "123", "gaia_release": "dr3", "dec": 2.0}],
    )
    # A newer but unpublished run is not a current dataset snapshot.
    corpus_c_id = store.register_corpus(corpus_c, "gamma")
    unpublished = store.start_run(corpus_c_id)
    store.index_members(
        unpublished,
        [{"source_id": "888", "gaia_release": "dr3", "ra": 8.0}],
    )
    store.finish_run(
        unpublished,
        "failed",
        metadata={"members_indexed": True, "published": False},
    )

    result = Library(store).lookup_source("123", limit=1)
    assert result["resolved"] is True
    assert result["scope"] == "global_library"
    assert result["source"]["attributes"] == {"dec": 2.0, "ra": 1.0}
    assert result["pagination"] == {
        "limit": 1,
        "offset": 0,
        "total": 2,
        "returned": 1,
        "has_more": True,
    }
    assert result["datasets"][0]["label"] == "beta"
    assert result["datasets"][0]["run_id"] == current_b
    assert result["datasets"][0]["n_member_rows"] == 2
    assert result["datasets"][0]["metadata"]["legacy_unverified"] is True

    second_page = Library(store).lookup_source("123", limit=1, offset=1)
    assert second_page["datasets"][0]["label"] == "gamma"
    assert second_page["datasets"][0]["run_id"] == current_c
    returned_runs = {
        row["run_id"]
        for page in (result, second_page)
        for row in page["datasets"]
    }
    assert old_a not in returned_runs
    assert current_a not in returned_runs
    assert unpublished not in returned_runs


def test_source_query_exposes_current_refs_and_source_id_products_only(tmp_path):
    clock = Clock()
    store = LibraryStore(tmp_path / "library", clock=clock)
    _published_run(
        store,
        clock,
        tmp_path / "corpus",
        "sample",
        [{"source_id": "123", "gaia_release": "dr3", "ra": 1.0}],
    )
    snapshot_id = store.put_source_references(
        "dr3",
        "123",
        [{"bibcode": "2026A&A...1A", "title": "A global paper"}],
        ttl_seconds=5,
    )
    availability_id = store.put_cache(
        "gaia-xp-availability:v1",
        source_cache_key("dr3", "123"),
        {
            "source_id": "123",
            "gaia_source_found": True,
            "has_xp_sampled": True,
            "has_xp_continuous": False,
        },
        ttl_seconds=5,
    )

    payload = tmp_path / "spectrum.fits"
    payload.write_bytes(b"spectrum")
    blob = store.blobs.publish(payload, content_type="application/fits")
    source_contract = {
        "release": "dr3",
        "retrieval_type": "XP_SAMPLED",
        "linking_parameter": "SOURCE_ID",
    }
    source_key = product_item_key(source_contract, "123")
    lease = store.acquire_lease("product", source_key)
    assert lease is not None
    version = store.save_product(
        source_contract,
        "123",
        [{"blob": blob, "role": "normalized", "slot": "spectrum", "metadata": {}}],
        lease,
    )
    store.release_lease(lease)

    transit_contract = {
        "release": "dr3",
        "retrieval_type": "EPOCH_ASTROMETRY",
        "linking_parameter": "TRANSIT_ID",
    }
    transit_key = product_item_key(transit_contract, "123")
    lease = store.acquire_lease("product", transit_key)
    assert lease is not None
    store.save_product(
        transit_contract,
        "123",
        [{"blob": blob, "role": "normalized", "slot": "table", "metadata": {}}],
        lease,
    )
    store.release_lease(lease)

    clock.advance(6)
    result = Library(store).lookup_source("123")
    assert result["active_bibliography"]["snapshot_id"] == snapshot_id
    assert result["xp_availability"]["id"] == availability_id
    assert result["xp_availability"]["stale"] is True
    assert result["xp_availability"]["payload"] == {
        "source_id": "123",
        "gaia_source_found": True,
        "has_xp_sampled": True,
        "has_xp_continuous": False,
    }
    assert [item["item_key"] for item in result["active_products"]] == [source_key]
    assert result["active_products"][0]["version_id"] == version["version_id"]
    assert result["active_products"][0]["integrity"] == "unchecked"


def test_paper_lookup_is_current_global_and_paginated(tmp_path):
    clock = Clock()
    store = LibraryStore(tmp_path / "library", clock=clock)
    _published_run(
        store,
        clock,
        tmp_path / "a",
        "alpha",
        [{"source_id": "1", "gaia_release": "dr3", "ra": 1.0}],
    )
    _published_run(
        store,
        clock,
        tmp_path / "b",
        "beta",
        [{"source_id": "2", "gaia_release": "dr3", "ra": 2.0}],
    )
    for source_id in ("1", "2"):
        store.put_source_references(
            "dr3",
            source_id,
            [
                {
                    "bibcode": "2026A&A...1A",
                    "title": "Shared young stars",
                    "arxiv_id": "2601.00001",
                }
            ],
        )
    result = Library(store).lookup_paper("2601.00001", limit=1, offset=1)
    assert result["resolved"] is True
    assert result["paper"]["bibcode"] == "2026A&A...1A"
    assert result["pagination"]["total"] == 2
    assert result["sources"][0]["source"]["source_id"] == "2"
    assert result["sources"][0]["datasets"][0]["label"] == "beta"

    # A failed refresh is history only, so the current successful edge remains.
    store.put_source_references("dr3", "1", [], outcome="failure")
    assert Library(store).lookup_paper("2026A&A...1A")["pagination"]["total"] == 2


def test_paper_title_ambiguity_is_explicit_and_paginated(tmp_path):
    store = LibraryStore(tmp_path / "library")
    store.upsert_paper({"bibcode": "A", "title": "Young stars in Alpha"})
    store.upsert_paper({"bibcode": "B", "title": "Young stars in Beta"})

    result = Library(store).lookup_paper("Young stars", limit=1)
    assert result["resolved"] is False
    assert result["ambiguous"] is True
    assert result["reason"] == "multiple_papers"
    assert result["pagination"]["total"] == 2
    assert len(result["matches"]) == 1


def test_mcp_library_tools_are_lazy_and_absent_without_writes(tmp_path, monkeypatch):
    from astro_pipeline import library as library_package
    from astro_pipeline import mcp_server

    root = tmp_path / "library"
    store = LibraryStore(root)
    monkeypatch.setattr(
        library_package,
        "get_library",
        lambda config=None, create=False: store,
    )

    status = mcp_server.library_status()
    source = mcp_server.lookup_library_source("123")
    paper = mcp_server.lookup_library_paper("missing")
    assert status["exists"] is False
    assert source["exists"] is False
    assert paper["exists"] is False
    assert not root.exists()
