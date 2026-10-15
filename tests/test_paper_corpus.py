"""Offline contracts for version-pinned, shared-library paper collections."""
from __future__ import annotations

from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

from astro_pipeline.library import paper_corpus as pc
from astro_pipeline.library.files import BlobIntegrityError
from astro_pipeline.library.import_export import export_corpus, verify_library
from astro_pipeline.library.query import Library
from astro_pipeline.library.schema import APPLICATION_ID, SCHEMA_V1_STATEMENTS, SCHEMA_VERSION
from astro_pipeline.library.store import LeaseLostError, LibraryStore
from astro_pipeline.sources.paper_fulltext import DownloadedArtifact, PaperDownloadError, PaperUnavailable


class FakeArxiv:
    def __init__(self):
        self.version = 1
        self.calls = []
        self.fail = set()
        self.absent = set()
        self.html_extra = b""

    def resolve(self, identifier):
        from astro_pipeline.sources.paper_fulltext import split_arxiv_id
        self.calls.append(("resolve", identifier))
        if "resolve" in self.fail:
            raise PaperDownloadError("temporary metadata failure")
        work, version = split_arxiv_id(identifier)
        version = version or self.version
        return {"arxiv_id": work, "version": version, "versioned_id": f"{work}v{version}",
                "title": "A synthetic stellar census", "authors": ["Example Author"],
                "doi": "10.1234/example", "license": None, "license_evidence": [],
                "metadata_url": f"https://arxiv.org/abs/{work}v{version}",
                "resolved_at": "2026-10-08T00:00:00Z", "_metadata_bytes": b"<html>metadata</html>",
                "_metadata_content_type": "text/html"}

    def fetch(self, identifier, kind):
        self.calls.append((kind, identifier))
        if kind in self.fail:
            raise PaperDownloadError("temporary transport failure")
        if kind in self.absent:
            raise PaperUnavailable("HTTP 404")
        data = {"html": b"<html><article><h1>A paper</h1><h2>1 Introduction</h2>"
                        b"<p>The stellar census contains measured fluxes and uncertainty estimates.</p>"
                        b"<h2>2 Results</h2><p>Masses and ages are reported with their limitations.</p>"
                        b"</article></html>",
                "source": br"\begin{document}\section{Results}Stars have measured fluxes.\end{document}",
                "pdf": b"%PDF-1.4\nsynthetic-reference-only"}[kind]
        if kind == "html" and self.html_extra:
            data = data.replace(b"</article>", self.html_extra + b"</article>")
        url = f"https://arxiv.org/{kind}/{identifier}"
        return DownloadedArtifact(data, url, url, "text/html" if kind == "html" else "application/octet-stream",
                                  "2026-10-08T00:00:00Z", kind, identifier)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from astro_pipeline import paper_text

    monkeypatch.setattr(pc, "_parse_document", lambda kind, data, metadata: {
        "html": paper_text.parse_html, "source": paper_text.parse_latex, "pdf": paper_text.parse_pdf,
    }[kind](data, paper=metadata))
    return LibraryStore(tmp_path / "library"), FakeArxiv(), tmp_path / "data"


def build(setup, label="papers", papers=None, **kwargs):
    store, client, data = setup
    return pc.build_paper_corpus(label, ["2305.03557"] if papers is None else papers,
                                 store=store, client=client, output_dir=data, **kwargs)


def test_v1_readonly_then_additive_migration(tmp_path):
    store = LibraryStore(tmp_path / "library")
    store.root.mkdir()
    with sqlite3.connect(store.db_path) as conn:
        for statement in SCHEMA_V1_STATEMENTS:
            conn.execute(statement)
        conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        conn.execute("PRAGMA user_version = 1")
        conn.execute("INSERT INTO papers VALUES ('bib', '{\"title\":\"Kept\"}', 1)")
    assert pc.lookup_paper_text(store, "bib")["schema_ready"] is False
    assert store.status()["n_papers"] == 1
    assert store.read("PRAGMA user_version")[0]["user_version"] == 1
    backup = store.backup(tmp_path / "before.sqlite3")
    store.initialize()
    store.initialize()
    assert store.read("PRAGMA user_version")[0]["user_version"] == SCHEMA_VERSION
    assert store.read("SELECT bibcode FROM papers") == [{"bibcode": "bib"}]
    assert store.read("PRAGMA foreign_key_check") == []
    with sqlite3.connect(backup) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_absent_status_does_not_create(tmp_path):
    store = LibraryStore(tmp_path / "library")
    assert pc.lookup_paper_text(store, "2305.03557")["available"] is False
    assert pc.paper_corpus_status("missing", output_dir=tmp_path / "data")["exists"] is False
    assert not store.root.exists()
    assert not (tmp_path / "data").exists()


def test_build_persists_artifacts_with_real_spawned_parser(tmp_path):
    """Exercise file publication and the actual parser process on every platform."""
    store = LibraryStore(tmp_path / "library")
    client = FakeArxiv()
    result = pc.build_paper_corpus(
        "spawned-parser", ["2305.03557v1"], store=store, client=client,
        output_dir=tmp_path / "data", include_pdf=False,
    )
    assert result["n_fulltext"] == 1, result
    paper = result["papers"][0]
    assert paper["versioned_id"] == "2305.03557v1"
    artifacts = {artifact["kind"]: artifact for artifact in paper["artifacts"]}
    assert {"metadata", "html", "json", "markdown"}.issubset(artifacts)
    for artifact in artifacts.values():
        store.blobs.verify(artifact)
    assert "The stellar census" in Path(artifacts["markdown"]["path"]).read_text(encoding="utf-8")
    assert client.calls == [("resolve", "2305.03557v1"), ("html", "2305.03557v1")]
    assert verify_library(store)["ok"]


def test_build_deduplicates_but_keeps_occurrences_and_aliases(setup):
    store, client, data = setup
    papers = [{"arxiv_id": "2305.03557", "bibcode": "bibA", "source_run": "run-a"},
              {"arxiv_id": "2305.03557", "bibcode": "bibB", "source_run": "run-b"}]
    first = build(setup, papers=papers)
    assert first["n_inputs"] == 2
    assert first["n_unique_papers"] == 1
    assert first["n_fulltext"] == 2
    assert [p["input"]["source_run"] for p in first["papers"]] == ["run-a", "run-b"]
    assert first["papers"][0]["version_id"] == first["papers"][1]["version_id"]
    assert client.calls == [("resolve", "2305.03557"), ("html", "2305.03557v1"), ("pdf", "2305.03557v1")]
    assert len(store.read("SELECT * FROM paper_text_versions")) == 1
    assert not store.read("SELECT * FROM source_papers")
    assert not store.read("SELECT * FROM papers")
    text = pc.lookup_paper_text(store, "bibB")
    assert text["available"] and text["versions"][0]["metadata"]["license"] is None
    doc_artifact = next(a for a in text["versions"][0]["artifacts"] if a["kind"] == "json")
    document = json.loads(Path(doc_artifact["path"]).read_text())
    assert document["source"]["sha256"]
    assert document["rights"]["reuse_permission"] == "not_assessed"
    assert document["quality"]["scientifically_verified"] is False
    assert verify_library(store)["ok"]
    assert Library(store).lookup_paper("bibB")["fulltext"]["available"]
    assert Library(store).status()["n_current_datasets"] == 0
    with pytest.raises(ValueError, match="does not export paper"):
        export_corpus(store, Path(first["path"]), data / "export")


def test_resume_and_cross_label_reuse_download_nothing(setup):
    first = build(setup)
    calls = list(setup[1].calls)
    second = build(setup)
    assert second["run_id"] == first["run_id"]
    assert setup[1].calls == calls
    third = build(setup, label="other")
    assert third["run_id"] != first["run_id"]
    assert third["papers"][0]["version_id"] == first["papers"][0]["version_id"]
    assert setup[1].calls == calls


def test_partial_reference_pdf_retries_without_refetching_text(setup):
    store, client, _ = setup
    client.fail.add("pdf")
    first = build(setup)
    assert first["status"] == "partial"
    assert first["n_fulltext"] == 1
    assert not first["papers"][0]["acquisition_complete"]
    client.fail.clear()
    second = build(setup)
    assert second["papers"][0]["acquisition_complete"]
    assert second["papers"][0]["versioned_id"] == "2305.03557v1"
    assert client.calls.count(("html", "2305.03557v1")) == 1
    assert client.calls.count(("pdf", "2305.03557v1")) == 2
    assert len(store.read("SELECT * FROM paper_text_versions")) == 2


def test_refresh_failure_keeps_old_snapshot_and_pointer(setup):
    store, client, data = setup
    first = build(setup)
    before = (Path(first["path"]) / "runs" / first["run_id"] / "manifest.json").read_bytes()
    client.fail.update({"html", "source", "pdf"})
    failed = build(setup, refresh=True)
    assert failed["counts"] == {"failed": 1}
    assert failed["run_id"] != first["run_id"]
    assert (Path(first["path"]) / "runs" / first["run_id"] / "manifest.json").read_bytes() == before
    assert store.read("SELECT version_id FROM paper_text_current")[0]["version_id"] == first["papers"][0]["version_id"]
    assert pc.paper_corpus_status("papers", run_id=first["run_id"], output_dir=data)["n_fulltext"] == 1


def test_new_arxiv_version_refresh_does_not_rewrite_old(setup):
    store, client, _ = setup
    first = build(setup)
    client.version = 2
    newer = build(setup, refresh=True)
    assert newer["papers"][0]["versioned_id"] == "2305.03557v2"
    assert pc.lookup_paper_text(store, "2305.03557v1")["versions"][0]["version_id"] == first["papers"][0]["version_id"]
    assert len(pc.lookup_paper_text(store, "2305.03557")["versions"]) == 2


def test_mid_paper_interrupt_keeps_exact_version_and_raw_download(setup, monkeypatch):
    store, client, data = setup
    original = pc._parse_document
    monkeypatch.setattr(pc, "_parse_document", lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        build(setup)
    row = store.read("SELECT * FROM paper_inputs")[0]
    assert row["pinned_version"] == 1
    assert store.read("SELECT kind FROM paper_downloads WHERE kind = 'html'")
    client.version = 9
    monkeypatch.setattr(pc, "_parse_document", original)
    resumed = build(setup)
    assert resumed["papers"][0]["versioned_id"] == "2305.03557v1"
    assert client.calls.count(("html", "2305.03557v1")) == 1
    assert client.calls.count(("resolve", "2305.03557")) == 1


def test_missing_blob_regenerated_not_silently_reused(setup):
    store, client, _ = setup
    result = build(setup)
    artifact = next(a for a in result["papers"][0]["artifacts"] if a["kind"] == "html")
    artifact_path = Path(artifact["path"])
    artifact_path.chmod(0o644)
    artifact_path.unlink()
    assert not verify_library(store)["ok"]
    repaired = build(setup)
    assert repaired["n_fulltext"] == 1
    assert repaired["papers"][0]["version_id"] == result["papers"][0]["version_id"]
    assert client.calls.count(("html", "2305.03557v1")) == 2
    assert verify_library(store)["ok"]


def test_missing_derivatives_reproduce_exact_pins_without_network(setup):
    store, client, _ = setup
    first = build(setup)
    calls = list(client.calls)
    for artifact in first["papers"][0]["artifacts"]:
        if artifact["kind"] in {"json", "markdown"}:
            artifact_path = Path(artifact["path"])
            artifact_path.chmod(0o644)
            artifact_path.unlink()
    restored = build(setup)
    assert restored["n_fulltext"] == 1
    assert restored["papers"][0]["version_id"] == first["papers"][0]["version_id"]
    assert client.calls == calls
    assert verify_library(store)["ok"]


def test_failed_refresh_resume_cannot_fall_back_to_pre_refresh_cache(setup):
    _, client, _ = setup
    first = build(setup)
    client.fail.update({"html", "source", "pdf"})
    failed = build(setup, refresh=True)
    client.fail.clear()
    resumed = build(setup)
    assert resumed["run_id"] == failed["run_id"]
    assert resumed["papers"][0]["version_id"] != first["papers"][0]["version_id"]
    assert client.calls.count(("html", "2305.03557v1")) == 3
    assert resumed["n_fulltext"] == 1


def test_partial_completion_keeps_its_raw_text_not_a_newer_global_version(setup):
    store, client, _ = setup
    build(setup)
    client.html_extra = b"<p>Version B rendering.</p>"
    client.fail.add("pdf")
    partial = build(setup, refresh=True)
    partial_id = partial["papers"][0]["version_id"]
    assert not partial["papers"][0]["acquisition_complete"]
    client.fail.clear()
    client.html_extra = b"<p>Version C rendering.</p>"
    newer = build(setup, label="newer", refresh=True)
    calls = list(client.calls)
    resumed = build(setup)
    assert client.calls == calls
    assert resumed["papers"][0]["version_id"] != newer["papers"][0]["version_id"]
    text = pc._version(store, resumed["papers"][0]["version_id"], verify=True)
    assert text["metadata"]["completed_from_text_version_id"] == partial_id
    document_path = next(a["path"] for a in text["artifacts"] if a["kind"] == "json")
    content = Path(document_path).read_text()
    assert "Version B rendering" in content and "Version C rendering" not in content


def test_damaged_pin_never_rebinds_to_newer_global_bytes(setup):
    store, client, _ = setup
    first = build(setup)
    original_id = first["papers"][0]["version_id"]
    original_html = next(a for a in first["papers"][0]["artifacts"] if a["kind"] == "html")
    client.html_extra = b"<p>A newer server rendering.</p>"
    newer = build(setup, label="newer", refresh=True)
    newer_id = newer["papers"][0]["version_id"]
    original_path = Path(original_html["path"])
    original_path.chmod(0o644)
    original_path.unlink()
    failed = build(setup)
    assert failed["papers"][0]["version_id"] == original_id
    assert failed["papers"][0]["status"] == "failed"
    assert "cannot reproduce pinned html bytes" in failed["papers"][0]["error"]
    assert store.read("SELECT version_id FROM paper_text_current WHERE include_pdf = 1")[0]["version_id"] == newer_id
    client.html_extra = b""
    repaired = build(setup)
    assert repaired["papers"][0]["version_id"] == original_id
    assert repaired["n_fulltext"] == 1
    assert verify_library(store)["ok"]


def test_negative_availability_cached_failures_retryable(setup):
    store, client, _ = setup
    client.absent.update({"html", "source", "pdf"})
    first = build(setup)
    assert first["counts"] == {"unavailable": 1}
    before = len(client.calls)
    build(setup)
    assert len(client.calls) == before
    client.absent.clear()
    second = build(setup, refresh=True)
    assert second["n_fulltext"] == 1


def test_selection_contract_and_bounds_rejected_before_network(setup):
    store, client, _ = setup
    for papers in ([], ["2305.03557"] * 51, ["../../bad"]):
        with pytest.raises(ValueError):
            build(setup, papers=papers)
    assert not store.exists and not client.calls
    build(setup)
    count = len(store.read("SELECT run_id FROM runs"))
    with pytest.raises(ValueError, match="different paper selection"):
        build(setup, papers=["2305.03558"])
    assert len(store.read("SELECT run_id FROM runs")) == count


def test_stale_fence_cannot_publish_download(setup):
    store, client, _ = setup
    metadata = client.resolve("2305.03557")
    lease = store.acquire_lease("paper-text", "2305.03557")
    store.release_lease(lease)
    successor = store.acquire_lease("paper-text", "2305.03557")
    with pytest.raises(LeaseLostError):
        pc._save_download(store, metadata, "html", b"abc", {"content_type": "text/html"}, lease)
    assert store.read("SELECT * FROM paper_downloads") == []
    store.release_lease(successor)


def test_versioned_and_doi_url_lookup_keep_existing_source_links(setup):
    store, _, data = setup
    corpus_id = store.register_corpus(data / "stars", "stars")
    run_id = store.start_run(corpus_id)
    store.index_members(run_id, [{"source_id": "123", "gaia_release": "dr3"}])
    store.finish_run(run_id, "completed", metadata={"members_indexed": True, "published": True})
    store.put_source_references("dr3", "123", [{"bibcode": "bibA", "arxiv_id": "2305.03557",
                                                "doi": "10.1234/example", "title": "A synthetic stellar census"}])
    build(setup)
    for query in ("2305.03557v1", "https://arxiv.org/abs/2305.03557v1", "https://doi.org/10.1234/example"):
        result = Library(store).lookup_paper(query)
        assert result["resolved"]
        assert result["sources"][0]["source"]["source_id"] == "123"
        assert result["fulltext"]["available"]
    assert pc.lookup_paper_text(store, "doi:10.1234/example")["available"]
    assert Library(store).lookup_paper("2305.03557v999")["fulltext"]["available"] is False


def test_non_object_alias_provenance_cannot_break_global_lookup(setup):
    store, _, _ = setup
    build(setup, papers=[{"arxiv_id": "2305.03557", "aliases": ["display name", 3, None, {"bibcode": "aliasBib"}]}])
    assert pc.lookup_paper_text(store, "aliasBib")["available"]
    assert not pc.lookup_paper_text(store, "not a paper")["available"]


def test_missing_metadata_blob_repairs_exact_pinned_version(setup):
    store, client, _ = setup
    first = build(setup)
    metadata_artifact = next(a for a in first["papers"][0]["artifacts"] if a["kind"] == "metadata")
    metadata_path = Path(metadata_artifact["path"])
    metadata_path.chmod(0o644)
    metadata_path.unlink()
    client.version = 2
    resumed = build(setup)
    assert resumed["papers"][0]["versioned_id"] == "2305.03557v1"
    assert ("resolve", "2305.03557v1") in client.calls
    assert verify_library(store)["ok"]


def test_library_cli_status_and_mcp_no_creation(tmp_path, monkeypatch, capsys):
    from astro_pipeline import mcp_server
    from astro_pipeline.config import PipelineConfig

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(PipelineConfig, "load", lambda *a, **k: PipelineConfig())
    assert mcp_server.paper_corpus_status("absent")["exists"] is False
    assert "error" in mcp_server.download_paper_corpus("../bad", ["2305.03557"])
    script = Path(__file__).parents[1] / "scripts" / "library.py"
    spec = importlib.util.spec_from_file_location("paper_library_cli", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["paper-status", "absent"]) == 0
    assert json.loads(capsys.readouterr().out)["exists"] is False
    assert not (tmp_path / "data").exists()
