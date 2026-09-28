"""Shared SIMBAD/paper cache behavior with a real temporary LibraryStore."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from astro_pipeline.library.papers import (
    DOI_ARXIV_NAMESPACE,
    PAPER_METADATA_NAMESPACE,
    normalize_doi,
)
from astro_pipeline.library.store import LibraryStore
from astro_pipeline.sources import simbad_refs
from astro_pipeline.sources.simbad_refs import (
    PaperRecord,
    SourcePapers,
    build_paper_edges,
)


class MutableClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def make_store(tmp_path: Path, clock: MutableClock | None = None) -> LibraryStore:
    store = LibraryStore(tmp_path / "library", clock=clock or MutableClock())
    store.positive_ttl_seconds = 30.0
    store.negative_ttl_seconds = 5.0
    store.lease_seconds = 5.0
    store.wait_timeout_seconds = 0.2
    return store


def source_with_papers(source_id: str, *bibcodes: str) -> SourcePapers:
    return SourcePapers(
        gaia_id=source_id,
        main_id=f"Source {source_id}",
        resolved=True,
        bibcodes=list(bibcodes),
        papers=[
            PaperRecord(
                bibcode=bibcode,
                arxiv_id=simbad_refs.bibcode_to_arxiv(bibcode),
                year=int(bibcode[:4]),
            )
            for bibcode in bibcodes
        ],
    )


def build(
    tmp_path: Path,
    label: str,
    source_ids: list[str],
    store: LibraryStore,
    **kwargs,
):
    return build_paper_edges(
        pd.DataFrame({"Gaia": source_ids}),
        library_store=store,
        cache_path=tmp_path / label / "simbad_refs.parquet",
        sleep_seconds=0,
        **kwargs,
    )


def test_source_snapshot_shared_across_labels_and_projection_is_scoped(
    monkeypatch, tmp_path
):
    store = make_store(tmp_path)
    calls: list[str] = []

    def fetch(source_id: str) -> SourcePapers:
        calls.append(source_id)
        return source_with_papers(source_id, f"2024TEST....{source_id}A")

    monkeypatch.setattr(simbad_refs, "get_source_bibcodes", fetch)
    edges_a, refs_a = build(
        tmp_path, "label-a", ["111", "111"], store, enrich=False
    )
    edges_b, refs_b = build(
        tmp_path, "label-b", ["111", "222"], store, enrich=False
    )
    edges_c, refs_c = build(tmp_path, "label-c", ["111"], store, enrich=False)

    assert calls == ["111", "222"]
    assert len(edges_a.df) == 1
    assert set(edges_b.df["source_id"]) == {"111", "222"}
    assert set(edges_c.df["source_id"]) == {"111"}
    assert set(refs_c["gaia_id"]) == {"111"}
    assert refs_a.attrs["source_progress"] == {
        "total": 1,
        "completed": 1,
        "failed": 0,
        "remaining": 0,
        "checkpoint_path": str(
            (tmp_path / "label-a" / "simbad_refs.checkpoint.json").resolve()
        ),
    }
    checkpoint = json.loads(
        (tmp_path / "label-a" / "simbad_refs.checkpoint.json").read_text()
    )
    assert checkpoint["completed"] == ["111"]
    assert checkpoint["failed"] == {}


def test_explicit_empty_snapshot_uses_negative_ttl_and_is_reused(
    monkeypatch, tmp_path
):
    clock = MutableClock()
    store = make_store(tmp_path, clock)
    calls: list[str] = []

    def unresolved(source_id: str) -> SourcePapers:
        calls.append(source_id)
        return SourcePapers(gaia_id=source_id, resolved=False)

    monkeypatch.setattr(simbad_refs, "get_source_bibcodes", unresolved)
    edges_a, refs_a = build(tmp_path, "empty-a", ["111"], store, enrich=False)
    edges_b, refs_b = build(tmp_path, "empty-b", ["111"], store, enrich=False)

    assert calls == ["111"]
    assert edges_a.df.empty and edges_b.df.empty
    assert refs_a.empty and refs_b.empty
    snapshot = store.get_source_references("dr3", "111")
    assert snapshot is not None
    assert snapshot["outcome"] == "unresolved"
    assert snapshot["papers"] == []
    assert snapshot["expires_at"] - snapshot["observed_at"] == 5.0
    checkpoint = json.loads(
        (tmp_path / "empty-b" / "simbad_refs.checkpoint.json").read_text()
    )
    assert checkpoint["completed"] == ["111"]


def test_source_failure_is_history_only_and_retries(monkeypatch, tmp_path):
    store = make_store(tmp_path)
    calls: list[str] = []

    def failed(source_id: str) -> SourcePapers:
        calls.append(source_id)
        return SourcePapers(gaia_id=source_id, error="service unavailable")

    monkeypatch.setattr(simbad_refs, "get_source_bibcodes", failed)
    _, first_refs = build(tmp_path, "retry", ["111"], store, enrich=False)
    assert first_refs.attrs["source_progress"]["failed"] == 1
    assert store.get_source_references("dr3", "111", include_stale=True) is None

    monkeypatch.setattr(
        simbad_refs,
        "get_source_bibcodes",
        lambda source_id: calls.append(source_id)
        or SourcePapers(gaia_id=source_id, resolved=True),
    )
    _, second_refs = build(tmp_path, "retry", ["111"], store, enrich=False)

    assert calls == ["111", "111"]
    assert second_refs.attrs["source_progress"]["failed"] == 0
    assert store.get_source_references("dr3", "111")["outcome"] == "empty"
    outcomes = [
        row["outcome"]
        for row in store.read(
            "SELECT outcome FROM source_ref_snapshots "
            "WHERE release = 'dr3' AND source_id = '111' ORDER BY observed_at"
        )
    ]
    assert outcomes == ["error", "empty"]


def test_failed_refresh_keeps_last_success_current(monkeypatch, tmp_path):
    store = make_store(tmp_path)
    bibcode = "2024TEST....1A"
    monkeypatch.setattr(
        simbad_refs,
        "get_source_bibcodes",
        lambda source_id: source_with_papers(source_id, bibcode),
    )
    build(tmp_path, "refresh-failure", ["111"], store, enrich=False)
    successful = store.get_source_references("dr3", "111")

    monkeypatch.setattr(
        simbad_refs,
        "get_source_bibcodes",
        lambda source_id: SourcePapers(
            gaia_id=source_id, error="temporary refresh failure"
        ),
    )
    edges, refs = build(
        tmp_path,
        "refresh-failure",
        ["111"],
        store,
        enrich=False,
        refresh=True,
    )

    assert edges.df.empty
    assert refs.attrs["source_progress"]["failed"] == 1
    current = store.get_source_references("dr3", "111")
    assert current["snapshot_id"] == successful["snapshot_id"]
    assert [paper["bibcode"] for paper in current["papers"]] == [bibcode]
    assert [
        row["outcome"]
        for row in store.read(
            "SELECT outcome FROM source_ref_snapshots "
            "WHERE release = 'dr3' AND source_id = '111' ORDER BY rowid"
        )
    ] == ["success", "error"]


def test_refresh_and_ttl_publish_replacement_snapshots_without_old_edges(
    monkeypatch, tmp_path
):
    clock = MutableClock()
    store = make_store(tmp_path, clock)
    paper_a = "2024TEST....1A"
    paper_b = "2024TEST....2B"
    responses = [
        source_with_papers("111", paper_a, paper_b),
        source_with_papers("111", paper_a),
        SourcePapers(gaia_id="111", resolved=True),
    ]
    calls = 0

    def fetch(source_id: str) -> SourcePapers:
        nonlocal calls
        result = responses[calls]
        calls += 1
        return result

    monkeypatch.setattr(simbad_refs, "get_source_bibcodes", fetch)
    first_edges, _ = build(tmp_path, "refresh", ["111"], store, enrich=False)
    cached_edges, _ = build(tmp_path, "refresh", ["111"], store, enrich=False)
    refreshed_edges, _ = build(
        tmp_path, "refresh", ["111"], store, enrich=False, refresh=True
    )

    assert calls == 2
    assert set(first_edges.df["bibcode"]) == {paper_a, paper_b}
    assert set(cached_edges.df["bibcode"]) == {paper_a, paper_b}
    assert refreshed_edges.df["bibcode"].tolist() == [paper_a]
    assert pd.read_parquet(tmp_path / "refresh" / "simbad_refs.parquet")[
        "bibcode"
    ].tolist() == [paper_a]

    clock.advance(store.positive_ttl_seconds + 1)
    expired_edges, refs = build(
        tmp_path, "refresh", ["111"], store, enrich=False
    )
    assert calls == 3
    assert expired_edges.df.empty and refs.empty
    assert store.get_source_references("dr3", "111")["outcome"] == "empty"
    assert len(
        store.read(
            "SELECT snapshot_id FROM source_ref_snapshots "
            "WHERE release = 'dr3' AND source_id = '111'"
        )
    ) == 3
    assert len(store.read("SELECT * FROM source_papers")) == 3


def test_metadata_and_no_arxiv_negative_are_shared(monkeypatch, tmp_path):
    store = make_store(tmp_path)
    bibcode = "2024TEST....1A"
    doi = "10.1234/Example"
    source_calls = 0
    metadata_calls = 0
    doi_calls = 0

    def fetch_source(source_id: str) -> SourcePapers:
        nonlocal source_calls
        source_calls += 1
        return source_with_papers(source_id, bibcode)

    def enrich(bibcode_arg: str, **kwargs) -> PaperRecord:
        nonlocal metadata_calls
        metadata_calls += 1
        assert kwargs["resolve_doi"] is False
        return PaperRecord(bibcode=bibcode_arg, title="A paper", doi=doi, year=2024)

    def no_arxiv(doi_arg: str) -> None:
        nonlocal doi_calls
        doi_calls += 1
        assert doi_arg == doi
        return None

    monkeypatch.setattr(simbad_refs, "get_source_bibcodes", fetch_source)
    monkeypatch.setattr(simbad_refs, "enrich_bibcode", enrich)
    monkeypatch.setattr(simbad_refs, "arxiv_id_from_doi", no_arxiv)

    _, refs_a = build(tmp_path, "meta-a", ["111"], store)
    _, refs_b = build(tmp_path, "meta-b", ["111"], store)

    assert source_calls == metadata_calls == doi_calls == 1
    assert refs_a.iloc[0]["title"] == refs_b.iloc[0]["title"] == "A paper"
    assert refs_a.iloc[0]["arxiv_id"] == refs_b.iloc[0]["arxiv_id"] == ""
    metadata_cache = store.get_cache(PAPER_METADATA_NAMESPACE, bibcode)
    doi_cache = store.get_cache(DOI_ARXIV_NAMESPACE, normalize_doi(doi))
    assert metadata_cache["outcome"] == "success"
    assert doi_cache["outcome"] == "not_found"
    assert doi_cache["expires_at"] - doi_cache["observed_at"] == 5.0


def test_resolve_doi_false_does_not_write_not_found(monkeypatch, tmp_path):
    store = make_store(tmp_path)
    bibcode = "2024TEST....1A"
    doi = "10.1234/example"
    doi_calls = 0

    monkeypatch.setattr(
        simbad_refs,
        "get_source_bibcodes",
        lambda source_id: source_with_papers(source_id, bibcode),
    )
    monkeypatch.setattr(
        simbad_refs,
        "enrich_bibcode",
        lambda bibcode_arg, **kwargs: PaperRecord(
            bibcode=bibcode_arg, title="A paper", doi=doi, year=2024
        ),
    )

    def resolve(doi_arg: str) -> str:
        nonlocal doi_calls
        doi_calls += 1
        return "2401.01234"

    monkeypatch.setattr(simbad_refs, "arxiv_id_from_doi", resolve)
    _, fast_refs = build(
        tmp_path, "doi-off", ["111"], store, resolve_doi=False
    )
    assert fast_refs.iloc[0]["arxiv_id"] == ""
    assert doi_calls == 0
    assert store.get_cache(DOI_ARXIV_NAMESPACE, normalize_doi(doi)) is None

    _, resolved_refs = build(tmp_path, "doi-on", ["111"], store)
    assert doi_calls == 1
    assert resolved_refs.iloc[0]["arxiv_id"] == "2401.01234"
    assert store.get_cache(DOI_ARXIV_NAMESPACE, normalize_doi(doi))[
        "outcome"
    ] == "found"


def test_doi_failure_remains_retryable(monkeypatch, tmp_path):
    store = make_store(tmp_path)
    bibcode = "2024TEST....1A"
    doi = "10.1234/example"
    attempts = 0

    monkeypatch.setattr(
        simbad_refs,
        "get_source_bibcodes",
        lambda source_id: source_with_papers(source_id, bibcode),
    )
    monkeypatch.setattr(
        simbad_refs,
        "enrich_bibcode",
        lambda bibcode_arg, **kwargs: PaperRecord(
            bibcode=bibcode_arg, title="A paper", doi=doi, year=2024
        ),
    )

    def flaky(doi_arg: str) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary failure")
        return "2401.01234"

    monkeypatch.setattr(simbad_refs, "arxiv_id_from_doi", flaky)
    _, first_refs = build(tmp_path, "doi-retry-a", ["111"], store)
    assert first_refs.iloc[0]["arxiv_id"] == ""
    assert store.get_cache(DOI_ARXIV_NAMESPACE, normalize_doi(doi)) is None

    _, second_refs = build(tmp_path, "doi-retry-b", ["111"], store)
    assert attempts == 2
    assert second_refs.iloc[0]["arxiv_id"] == "2401.01234"
    observations = store.read(
        "SELECT outcome FROM cache_observations "
        "WHERE namespace = ? ORDER BY rowid",
        (DOI_ARXIV_NAMESPACE,),
    )
    assert [row["outcome"] for row in observations] == ["error", "found"]
