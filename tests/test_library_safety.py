"""Safety boundaries of library identity and publication."""
from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from astro_pipeline.gaia_products import GaiaProductRequest
from astro_pipeline.intake import resolve_members_with_provenance
from astro_pipeline.library.files import sha256_file
from astro_pipeline.library.store import LeaseLostError, LibraryStore


def test_foreign_database_is_rejected_without_modification(tmp_path):
    root = tmp_path / "foreign"
    root.mkdir()
    database = root / "index.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE other_application(value TEXT)")
        connection.execute("INSERT INTO other_application VALUES ('preserve me')")
    before = sha256_file(database)
    store = LibraryStore(root)
    with pytest.raises(RuntimeError, match="not an initialized Astro-Corpus"):
        store.initialize()
    with pytest.raises(RuntimeError, match="not an initialized Astro-Corpus"):
        store.status()
    assert sha256_file(database) == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM other_application").fetchone()[0] == "preserve me"
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_numeric_identifier_spelling_does_not_split_identity(tmp_path):
    path = tmp_path / "members.csv"
    path.write_text("Gaia,note\n000111,first\n111,second\n")
    called = []

    def fetch(ids):
        called.append(ids)
        return pd.DataFrame({"Gaia": ids, "RAICRS": [10.0], "DEICRS": [20.0]})

    resolution = resolve_members_with_provenance(catalog=path, id_fetcher=fetch)
    assert called == [["111"]]
    assert resolution.members["Gaia"].tolist() == ["111", "111"]
    assert resolution.members["input_row_id"].tolist() == [0, 1]
    request = GaiaProductRequest(release="dr3", retrieval_type="XP_SAMPLED", identifiers=["000111", "111"])
    assert request.identifiers == ("111",)


def test_expired_corpus_owner_cannot_publish_source_rows(tmp_path):
    now = [100.0]
    store = LibraryStore(tmp_path / "library", clock=lambda: now[0])
    corpus = store.register_corpus(tmp_path / "corpus", "corpus")
    request = store.register_request({"release": "dr3", "input_mode": "test"})
    run_id = store.start_run(corpus, request)
    lease = store.acquire_lease("corpus", "same-output", ttl=1)
    now[0] += 2
    replacement = store.acquire_lease("corpus", "same-output", ttl=10)
    assert replacement.token > lease.token
    with pytest.raises(LeaseLostError):
        store.index_members(
            run_id, [{"gaia_release": "dr3", "Gaia": "111", "RAICRS": 10}], lease=lease,
        )
    assert store.status()["n_sources"] == 0
