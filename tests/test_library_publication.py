"""Publication failures never promote an incomplete scientific snapshot."""
import json

import pytest

from astro_pipeline import corpus_builder
from astro_pipeline.library import get_library
from astro_pipeline.library.store import LibraryStore
from tests.test_library_build import archives, members


def test_failed_required_stage_does_not_replace_active_run(tmp_path, archives, monkeypatch):
    target = tmp_path / "field"
    first = corpus_builder.build_corpus_snapshot(
        label="field", out_dir=target, cone=(10, 20, 0.1), limit=2, skip_simbad=True,
    )
    original = (target / "members.parquet").read_bytes()
    monkeypatch.setattr(corpus_builder, "resolve_members", lambda **kwargs: members(["444"]))

    def fail_papers(*args, **kwargs):
        raise RuntimeError("SIMBAD unavailable")

    monkeypatch.setattr(corpus_builder, "build_paper_edges", fail_papers)
    failed = corpus_builder.build_corpus_snapshot(
        label="field", out_dir=target, cone=(10, 20, 0.1), limit=2, skip_simbad=False,
    )
    assert failed.error and "SIMBAD unavailable" in failed.error
    assert (target / "members.parquet").read_bytes() == original
    assert json.loads((target / "manifest.json").read_text())["active_run_id"] == first.run_id
    row = get_library().read("SELECT status, metadata_json FROM runs WHERE run_id = ?", (failed.run_id,))[0]
    assert row["status"] == "failed"
    assert json.loads(row["metadata_json"])["published"] is False


@pytest.mark.parametrize("boundary", ["manifest_artifact", "finish_run"])
def test_activation_waits_for_durable_snapshot_and_run(tmp_path, archives, monkeypatch, boundary):
    target = tmp_path / "field"
    first = corpus_builder.build_corpus_snapshot(
        label="field", out_dir=target, cone=(10, 20, 0.1), limit=2, skip_simbad=True,
    )
    original = (target / "members.parquet").read_bytes()
    monkeypatch.setattr(corpus_builder, "resolve_members", lambda **kwargs: members(["444"]))
    if boundary == "manifest_artifact":
        record = LibraryStore.record_run_artifact

        def fail(self, run_id, kind, *args, **kwargs):
            if run_id != first.run_id and kind == "manifest_snapshot":
                raise OSError("injected snapshot indexing failure")
            return record(self, run_id, kind, *args, **kwargs)

        monkeypatch.setattr(LibraryStore, "record_run_artifact", fail)
    else:
        finish = LibraryStore.finish_run

        def fail(self, run_id, status, *args, **kwargs):
            if run_id != first.run_id and status == "completed":
                raise OSError("injected run commit failure")
            return finish(self, run_id, status, *args, **kwargs)

        monkeypatch.setattr(LibraryStore, "finish_run", fail)
    with pytest.raises(OSError, match="injected"):
        corpus_builder.build_corpus_snapshot(
            label="field", out_dir=target, cone=(10, 20, 0.1), limit=2, skip_simbad=True,
        )
    assert (target / "members.parquet").read_bytes() == original
    assert json.loads((target / "manifest.json").read_text())["active_run_id"] == first.run_id


def test_late_projection_failure_preserves_completed_prepared_snapshot(tmp_path, archives, monkeypatch):
    target = tmp_path / "field"
    update = LibraryStore.update_run

    def fail(self, run_id, metadata, **kwargs):
        if metadata.get("published") is True:
            raise OSError("injected publication index failure")
        return update(self, run_id, metadata, **kwargs)

    monkeypatch.setattr(LibraryStore, "update_run", fail)
    with pytest.raises(OSError, match="injected"):
        corpus_builder.build_corpus_snapshot(
            label="field", out_dir=target, cone=(10, 20, 0.1), limit=2, skip_simbad=True,
        )
    manifest = json.loads((target / "manifest.json").read_text())
    run_id = manifest["active_run_id"]
    assert (target / "runs" / run_id / "manifest.json").exists()
    row = get_library().read("SELECT status, metadata_json FROM runs WHERE run_id = ?", (run_id,))[0]
    metadata = json.loads(row["metadata_json"])
    assert row["status"] == "completed"
    assert metadata["prepared"] is True and metadata["view_pending"] is True
    assert metadata["published"] is False
