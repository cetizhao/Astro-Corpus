"""Versioned SQLite schema for the shared local library."""
from __future__ import annotations

SCHEMA_VERSION = 3
APPLICATION_ID = 0x41535452

SCHEMA_V1_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS corpora (
        corpus_id TEXT PRIMARY KEY,
        canonical_path TEXT NOT NULL UNIQUE,
        label TEXT NOT NULL,
        provenance_status TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_corpora_label ON corpora(label)",
    """
    CREATE TABLE IF NOT EXISTS requests (
        request_id TEXT PRIMARY KEY,
        contract_json TEXT NOT NULL,
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        corpus_id TEXT NOT NULL REFERENCES corpora(corpus_id),
        request_id TEXT REFERENCES requests(request_id),
        status TEXT NOT NULL,
        started_at REAL NOT NULL,
        finished_at REAL,
        options_json TEXT NOT NULL,
        error TEXT,
        metadata_json TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_runs_corpus ON runs(corpus_id, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_runs_request ON runs(request_id, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status)",
    """
    CREATE TABLE IF NOT EXISTS run_artifacts (
        artifact_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        row_count INTEGER,
        created_at REAL NOT NULL,
        UNIQUE(run_id, kind, path)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_run_artifacts_run ON run_artifacts(run_id)",
    """
    CREATE TABLE IF NOT EXISTS sources (
        gaia_release TEXT NOT NULL,
        source_id TEXT NOT NULL,
        attributes_json TEXT NOT NULL,
        covered_fields_json TEXT NOT NULL,
        field_provenance_json TEXT NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (gaia_release, source_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS input_rows (
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        input_row_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        status TEXT NOT NULL,
        raw_json TEXT NOT NULL,
        PRIMARY KEY (run_id, input_row_id),
        UNIQUE (run_id, ordinal)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sample_members (
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        member_ordinal INTEGER NOT NULL,
        release TEXT NOT NULL,
        source_id TEXT NOT NULL,
        input_row_id TEXT,
        attributes_json TEXT NOT NULL,
        match_json TEXT NOT NULL,
        PRIMARY KEY (run_id, member_ordinal),
        FOREIGN KEY (run_id, input_row_id)
            REFERENCES input_rows(run_id, input_row_id),
        FOREIGN KEY (release, source_id)
            REFERENCES sources(gaia_release, source_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sample_members_source ON sample_members(release, source_id)",
    "CREATE INDEX IF NOT EXISTS idx_sample_members_input ON sample_members(run_id, input_row_id)",
    """
    CREATE TABLE IF NOT EXISTS product_contracts (
        contract_hash TEXT PRIMARY KEY,
        contract_json TEXT NOT NULL,
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS product_items (
        item_key TEXT PRIMARY KEY,
        contract_hash TEXT NOT NULL REFERENCES product_contracts(contract_hash),
        identifier TEXT NOT NULL,
        normalizer_version TEXT NOT NULL,
        state TEXT NOT NULL,
        active_version_id TEXT,
        updated_at REAL NOT NULL,
        last_error TEXT,
        FOREIGN KEY (item_key, active_version_id)
            REFERENCES product_versions(item_key, version_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_product_items_state ON product_items(state)",
    """
    CREATE TABLE IF NOT EXISTS product_versions (
        version_id TEXT PRIMARY KEY,
        item_key TEXT NOT NULL REFERENCES product_items(item_key) ON DELETE CASCADE,
        created_at REAL NOT NULL,
        metadata_json TEXT NOT NULL,
        UNIQUE (item_key, version_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_product_versions_item ON product_versions(item_key, created_at)",
    """
    CREATE TABLE IF NOT EXISTS blobs (
        sha256 TEXT PRIMARY KEY,
        relative_path TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        content_type TEXT NOT NULL,
        extension TEXT NOT NULL,
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS product_artifacts (
        artifact_id TEXT PRIMARY KEY,
        version_id TEXT NOT NULL REFERENCES product_versions(version_id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK (role IN ('normalized', 'archive')),
        slot TEXT NOT NULL,
        blob_sha256 TEXT NOT NULL REFERENCES blobs(sha256),
        metadata_json TEXT NOT NULL,
        UNIQUE(version_id, role, slot)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_product_artifacts_version ON product_artifacts(version_id)",
    "CREATE INDEX IF NOT EXISTS idx_product_artifacts_blob ON product_artifacts(blob_sha256)",
    """
    CREATE TABLE IF NOT EXISTS attempts (
        id TEXT PRIMARY KEY,
        namespace TEXT NOT NULL,
        key TEXT NOT NULL,
        request_json TEXT NOT NULL,
        run_id TEXT REFERENCES runs(run_id),
        status TEXT NOT NULL,
        started_at REAL NOT NULL,
        finished_at REAL,
        error TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_attempts_key ON attempts(namespace, key, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_attempts_run ON attempts(run_id)",
    """
    CREATE TABLE IF NOT EXISTS leases (
        namespace TEXT NOT NULL,
        key TEXT NOT NULL,
        owner TEXT NOT NULL,
        token INTEGER NOT NULL,
        expires_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (namespace, key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_leases_expiry ON leases(expires_at)",
    """
    CREATE TABLE IF NOT EXISTS cache_observations (
        id TEXT PRIMARY KEY,
        namespace TEXT NOT NULL,
        cache_key TEXT NOT NULL,
        outcome TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        observed_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        error TEXT,
        UNIQUE (namespace, cache_key, id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cache_observations_key ON cache_observations(namespace, cache_key, observed_at)",
    """
    CREATE TABLE IF NOT EXISTS cache_current (
        namespace TEXT NOT NULL,
        cache_key TEXT NOT NULL,
        observation_id TEXT NOT NULL,
        PRIMARY KEY (namespace, cache_key),
        FOREIGN KEY (namespace, cache_key, observation_id)
            REFERENCES cache_observations(namespace, cache_key, id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS papers (
        bibcode TEXT PRIMARY KEY,
        metadata_json TEXT NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS source_ref_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        release TEXT NOT NULL,
        source_id TEXT NOT NULL,
        outcome TEXT NOT NULL,
        main_id TEXT NOT NULL,
        observed_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        UNIQUE (release, source_id, snapshot_id),
        FOREIGN KEY (release, source_id)
            REFERENCES sources(gaia_release, source_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_source_ref_snapshots_source ON source_ref_snapshots(release, source_id, observed_at)",
    """
    CREATE TABLE IF NOT EXISTS source_papers (
        snapshot_id TEXT NOT NULL REFERENCES source_ref_snapshots(snapshot_id) ON DELETE CASCADE,
        bibcode TEXT NOT NULL REFERENCES papers(bibcode),
        PRIMARY KEY (snapshot_id, bibcode)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_source_papers_bibcode ON source_papers(bibcode)",
    """
    CREATE TABLE IF NOT EXISTS source_ref_current (
        release TEXT NOT NULL,
        source_id TEXT NOT NULL,
        snapshot_id TEXT NOT NULL,
        PRIMARY KEY (release, source_id),
        FOREIGN KEY (release, source_id)
            REFERENCES sources(gaia_release, source_id),
        FOREIGN KEY (release, source_id, snapshot_id)
            REFERENCES source_ref_snapshots(release, source_id, snapshot_id)
    )
    """,
)

PAPER_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS paper_downloads (
        download_id TEXT PRIMARY KEY,
        work_id TEXT NOT NULL,
        arxiv_version INTEGER NOT NULL CHECK (arxiv_version > 0),
        kind TEXT NOT NULL,
        blob_sha256 TEXT NOT NULL REFERENCES blobs(sha256),
        metadata_json TEXT NOT NULL,
        created_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_paper_downloads_key ON paper_downloads(work_id, arxiv_version, kind, created_at)",
    """
    CREATE TABLE IF NOT EXISTS paper_text_versions (
        version_id TEXT PRIMARY KEY,
        work_id TEXT NOT NULL,
        arxiv_version INTEGER NOT NULL CHECK (arxiv_version > 0),
        parser_version TEXT NOT NULL,
        include_pdf INTEGER NOT NULL CHECK (include_pdf IN (0, 1)),
        metadata_json TEXT NOT NULL,
        quality_json TEXT NOT NULL,
        created_at REAL NOT NULL,
        UNIQUE(work_id, arxiv_version, parser_version, include_pdf, version_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_paper_text_work ON paper_text_versions(work_id, arxiv_version)",
    """
    CREATE TABLE IF NOT EXISTS paper_text_artifacts (
        version_id TEXT NOT NULL REFERENCES paper_text_versions(version_id),
        kind TEXT NOT NULL,
        blob_sha256 TEXT NOT NULL REFERENCES blobs(sha256),
        metadata_json TEXT NOT NULL,
        PRIMARY KEY(version_id, kind)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_paper_text_blobs ON paper_text_artifacts(blob_sha256)",
    """
    CREATE TABLE IF NOT EXISTS paper_text_current (
        work_id TEXT NOT NULL,
        arxiv_version INTEGER NOT NULL,
        parser_version TEXT NOT NULL,
        include_pdf INTEGER NOT NULL,
        version_id TEXT NOT NULL,
        PRIMARY KEY(work_id, arxiv_version, parser_version, include_pdf),
        FOREIGN KEY(work_id, arxiv_version, parser_version, include_pdf, version_id)
            REFERENCES paper_text_versions(work_id, arxiv_version, parser_version, include_pdf, version_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_inputs (
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        input_json TEXT NOT NULL,
        work_id TEXT NOT NULL,
        requested_id TEXT NOT NULL,
        pinned_version INTEGER,
        metadata_json TEXT NOT NULL,
        status TEXT NOT NULL,
        version_id TEXT REFERENCES paper_text_versions(version_id),
        result_json TEXT NOT NULL,
        PRIMARY KEY(run_id, ordinal)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_paper_inputs_work ON paper_inputs(work_id, run_id)",
)

EVIDENCE_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS paper_annotation_batches (
        batch_id TEXT PRIMARY KEY,
        paper_version_id TEXT NOT NULL REFERENCES paper_text_versions(version_id),
        document_sha256 TEXT NOT NULL REFERENCES blobs(sha256),
        payload_hash TEXT NOT NULL,
        request_json TEXT NOT NULL,
        extractor_json TEXT NOT NULL,
        created_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_annotation_batches_version ON paper_annotation_batches(paper_version_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS paper_annotations (
        annotation_id TEXT PRIMARY KEY,
        batch_id TEXT NOT NULL REFERENCES paper_annotation_batches(batch_id),
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        release TEXT NOT NULL,
        source_id TEXT NOT NULL,
        subject_scope TEXT NOT NULL CHECK (subject_scope IN ('source', 'component', 'system')),
        assessment TEXT NOT NULL CHECK (assessment IN
            ('evidence_found', 'uncertain', 'not_found_in_inspected_blocks')),
        review_status TEXT NOT NULL CHECK (review_status = 'pending'),
        annotation_json TEXT NOT NULL,
        UNIQUE(batch_id, ordinal),
        FOREIGN KEY (release, source_id) REFERENCES sources(gaia_release, source_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_paper_annotations_source ON paper_annotations(release, source_id, batch_id)",
    """
    CREATE TABLE IF NOT EXISTS paper_annotation_roles (
        annotation_id TEXT NOT NULL REFERENCES paper_annotations(annotation_id),
        role TEXT NOT NULL CHECK (role IN ('mentioned', 'in_sample', 'analysed',
            'reports_result', 'cites_prior_result', 'comparison', 'calibrator')),
        PRIMARY KEY(annotation_id, role)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_paper_annotation_roles_role ON paper_annotation_roles(role, annotation_id)",
)

SCHEMA_STATEMENTS = SCHEMA_V1_STATEMENTS + PAPER_SCHEMA_STATEMENTS + EVIDENCE_SCHEMA_STATEMENTS
