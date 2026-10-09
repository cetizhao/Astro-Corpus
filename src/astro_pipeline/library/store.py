"""SQLite-backed global index and shared-product registry.

The store deliberately owns no network clients.  Read-only methods never create
or migrate the database; write methods initialize the current schema on demand.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Iterator, Mapping, Sequence
import uuid

from astro_pipeline.library.files import Blob, BlobIntegrityError, BlobStore
from astro_pipeline.library.identity import (
    canonical_json,
    identity_hash,
    json_value,
    normalize_identifier,
    product_contract_hash,
    product_item_key,
    source_identity,
)
from astro_pipeline.library.schema import (
    APPLICATION_ID, PAPER_SCHEMA_STATEMENTS, SCHEMA_STATEMENTS, SCHEMA_VERSION,
)


class LibraryBusyError(RuntimeError):
    """Raised when a bounded lease wait cannot acquire ownership."""


class LeaseLostError(RuntimeError):
    """Raised when a lease's owner, fencing token, or expiry is no longer valid."""


@dataclass(frozen=True)
class Lease:
    namespace: str
    key: str
    owner: str
    token: int
    expires_at: float


_JSON_OBJECT = "{}"
_FAILURE_OUTCOMES = frozenset({"error", "failed", "failure"})
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SOURCE_IDENTITY_KEYS = frozenset({"gaia", "source_id", "gaia_release", "release"})
_SOURCE_EXCLUDED_PREFIXES = ("input_", "member_", "match_")


def _loads(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value)


def _required_text(value: Any, name: str) -> str:
    if value is None:
        raise ValueError(f"{name} must not be blank")
    text = str(value).strip()
    if not text:
        raise ValueError(f"{name} must not be blank")
    return text


def _ttl(value: float) -> float:
    seconds = float(value)
    if seconds <= 0:
        raise ValueError("ttl must be greater than zero")
    return seconds


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set, frozenset)):
        return not value
    return False


class LibraryStore:
    """One local SQLite index plus its content-addressed blob store."""

    lease_seconds = 120.0
    wait_timeout_seconds = 30.0
    positive_ttl_seconds = 30.0 * 24.0 * 60.0 * 60.0
    negative_ttl_seconds = 24.0 * 60.0 * 60.0

    def __init__(
        self,
        root: Path | str,
        busy_timeout_ms: int = 5000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if int(busy_timeout_ms) < 0:
            raise ValueError("busy_timeout_ms must be non-negative")
        self.root = Path(root).expanduser().resolve()
        self.db_path = self.root / "index.sqlite3"
        self.blobs = BlobStore(self.root)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self.clock = clock

    @property
    def exists(self) -> bool:
        return self.db_path.is_file()

    def _connect(self, *, readonly: bool) -> sqlite3.Connection:
        timeout = self.busy_timeout_ms / 1000.0
        if readonly:
            uri = self.db_path.resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(
                uri,
                uri=True,
                timeout=timeout,
                isolation_level=None,
            )
            connection.execute("PRAGMA query_only = ON")
        else:
            connection = sqlite3.connect(
                self.db_path,
                timeout=timeout,
                isolation_level=None,
            )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return connection

    @staticmethod
    def _schema_version(connection: sqlite3.Connection) -> int:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])

    @staticmethod
    def _reject_newer_schema(version: int) -> None:
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"library schema {version} is newer than supported schema "
                f"{SCHEMA_VERSION}"
            )

    @staticmethod
    def _validate_owner(connection: sqlite3.Connection, *, allow_empty: bool = False) -> None:
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        if application_id == APPLICATION_ID:
            return
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if allow_empty and application_id == 0 and not tables:
            return
        raise RuntimeError("Refusing a database that is not an initialized Astro-Corpus library")

    def initialize(self) -> None:
        """Create the current schema or apply additive, transactional migrations."""
        self.root.mkdir(parents=True, exist_ok=True)
        connection = self._connect(readonly=False)
        try:
            connection.execute("BEGIN")
            version = self._schema_version(connection)
            self._reject_newer_schema(version)
            self._validate_owner(connection, allow_empty=version == 0)
            connection.commit()
            journal_mode = str(
                connection.execute("PRAGMA journal_mode").fetchone()[0]
            ).lower()
            if journal_mode != "wal":
                connection.execute("PRAGMA journal_mode = WAL")
            if version == SCHEMA_VERSION:
                return
            connection.execute("BEGIN IMMEDIATE")
            try:
                # Another initializer may have completed while this connection
                # waited for the write lock.
                version = self._schema_version(connection)
                self._reject_newer_schema(version)
                if version == 0:
                    self._validate_owner(connection, allow_empty=True)
                    for statement in SCHEMA_STATEMENTS:
                        connection.execute(statement)
                    connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                elif version == 1:
                    self._validate_owner(connection)
                    for statement in PAPER_SCHEMA_STATEMENTS:
                        connection.execute(statement)
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                elif version != SCHEMA_VERSION:
                    raise RuntimeError(
                        f"no migration from library schema {version} to "
                        f"{SCHEMA_VERSION}"
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        finally:
            connection.close()

    @contextmanager
    def _readonly_connection(self) -> Iterator[sqlite3.Connection | None]:
        if not self.exists:
            yield None
            return
        connection = self._connect(readonly=True)
        try:
            self._reject_newer_schema(self._schema_version(connection))
            self._validate_owner(connection)
            yield connection
        finally:
            connection.close()

    def read(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        """Execute one query without creating or migrating an absent database."""
        with self._readonly_connection() as connection:
            if connection is None:
                return []
            cursor = connection.execute(sql, tuple(params))
            return [dict(row) for row in cursor.fetchall()]

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Open a short, fenced write transaction on a fresh connection."""
        self.initialize()
        connection = self._connect(readonly=False)
        try:
            version = self._schema_version(connection)
            self._reject_newer_schema(version)
            if version != SCHEMA_VERSION:
                raise RuntimeError(
                    f"library schema {version} is not initialized to {SCHEMA_VERSION}"
                )
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        finally:
            connection.close()

    def status(self) -> dict[str, Any]:
        result = {
            "exists": self.exists,
            "schema": 0,
            "n_sources": 0,
            "n_corpora": 0,
            "n_runs": 0,
            "n_papers": 0,
            "n_ready_products": 0,
            "n_blobs": 0,
        }
        with self._readonly_connection() as connection:
            if connection is None:
                return result
            version = self._schema_version(connection)
            result["schema"] = version
            if version not in (1, SCHEMA_VERSION):
                return result
            counts = {
                "n_sources": "SELECT COUNT(*) FROM sources",
                "n_corpora": "SELECT COUNT(*) FROM corpora",
                "n_runs": "SELECT COUNT(*) FROM runs",
                "n_papers": "SELECT COUNT(*) FROM papers",
                "n_ready_products": (
                    "SELECT COUNT(*) FROM product_items "
                    "WHERE state = 'ready' AND active_version_id IS NOT NULL"
                ),
                "n_blobs": "SELECT COUNT(*) FROM blobs",
            }
            if version >= 2:
                counts["n_paper_text_versions"] = "SELECT COUNT(*) FROM paper_text_versions"
                counts["n_fulltext_papers"] = "SELECT COUNT(DISTINCT work_id) FROM paper_text_versions"
            for name, sql in counts.items():
                result[name] = int(connection.execute(sql).fetchone()[0])
            return result

    def backup(self, destination: Path | str) -> Path:
        """Create a consistent SQLite backup; never replace a destination."""
        destination = Path(destination).expanduser().resolve()
        if destination.exists():
            raise FileExistsError(destination)
        if not self.exists:
            raise FileNotFoundError(self.db_path)
        if destination == self.db_path:
            raise ValueError("backup destination must differ from the library database")
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = self._connect(readonly=True)
        target: sqlite3.Connection | None = None
        try:
            self._reject_newer_schema(self._schema_version(source))
            target = sqlite3.connect(destination)
            source.backup(target)
            target.commit()
        except BaseException:
            if target is not None:
                target.close()
                target = None
            destination.unlink(missing_ok=True)
            raise
        finally:
            source.close()
            if target is not None:
                target.close()
        return destination

    # ------------------------------------------------------------------ leases

    def acquire_lease(
        self,
        namespace: str,
        key: str,
        owner: str | None = None,
        ttl: float = 120,
    ) -> Lease | None:
        namespace = _required_text(namespace, "namespace")
        key = _required_text(key, "key")
        ttl = _ttl(ttl)
        owner = _required_text(
            owner or f"{os.getpid()}:{threading.get_ident()}:{uuid.uuid4().hex}",
            "owner",
        )
        now = float(self.clock())
        expires_at = now + ttl
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT owner, token, expires_at FROM leases "
                "WHERE namespace = ? AND key = ?",
                (namespace, key),
            ).fetchone()
            if row is None:
                token = 1
                connection.execute(
                    "INSERT INTO leases(namespace, key, owner, token, expires_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (namespace, key, owner, token, expires_at, now),
                )
            elif float(row["expires_at"]) <= now:
                token = int(row["token"]) + 1
                cursor = connection.execute(
                    "UPDATE leases SET owner = ?, token = ?, expires_at = ?, updated_at = ? "
                    "WHERE namespace = ? AND key = ? AND token = ? "
                    "AND expires_at <= ?",
                    (
                        owner,
                        token,
                        expires_at,
                        now,
                        namespace,
                        key,
                        int(row["token"]),
                        now,
                    ),
                )
                if cursor.rowcount != 1:
                    return None
            else:
                return None
        return Lease(namespace, key, owner, token, expires_at)

    def renew_lease(self, lease: Lease, ttl: float = 120) -> Lease:
        ttl = _ttl(ttl)
        now = float(self.clock())
        expires_at = now + ttl
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE leases SET expires_at = ?, updated_at = ? "
                "WHERE namespace = ? AND key = ? AND owner = ? AND token = ? "
                "AND expires_at > ?",
                (
                    expires_at,
                    now,
                    lease.namespace,
                    lease.key,
                    lease.owner,
                    lease.token,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(
                    f"lease lost for {lease.namespace}:{lease.key} token {lease.token}"
                )
        return Lease(
            lease.namespace, lease.key, lease.owner, lease.token, expires_at
        )

    def release_lease(self, lease: Lease) -> None:
        now = float(self.clock())
        with self.transaction() as connection:
            connection.execute(
                "UPDATE leases SET expires_at = ?, updated_at = ? "
                "WHERE namespace = ? AND key = ? AND owner = ? AND token = ?",
                (
                    now,
                    now,
                    lease.namespace,
                    lease.key,
                    lease.owner,
                    lease.token,
                ),
            )

    def assert_lease(self, connection: sqlite3.Connection, lease: Lease) -> None:
        row = connection.execute(
            "SELECT owner, token, expires_at FROM leases "
            "WHERE namespace = ? AND key = ?",
            (lease.namespace, lease.key),
        ).fetchone()
        now = float(self.clock())
        if (
            row is None
            or row["owner"] != lease.owner
            or int(row["token"]) != lease.token
            or float(row["expires_at"]) <= now
        ):
            raise LeaseLostError(
                f"lease lost for {lease.namespace}:{lease.key} token {lease.token}"
            )

    @contextmanager
    def hold_lease(
        self,
        namespace: str,
        key: str,
        ttl: float = 120,
        wait_timeout: float = 0,
    ) -> Iterator[Lease]:
        """Acquire with a bounded wait and renew on a dedicated thread."""
        ttl = _ttl(ttl)
        wait_timeout = float(wait_timeout)
        if wait_timeout < 0:
            raise ValueError("wait_timeout must be non-negative")
        deadline = time.monotonic() + wait_timeout
        owner = f"{os.getpid()}:{threading.get_ident()}:{uuid.uuid4().hex}"
        lease: Lease | None = None
        while lease is None:
            lease = self.acquire_lease(namespace, key, owner=owner, ttl=ttl)
            if lease is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LibraryBusyError(f"library item is busy: {namespace}:{key}")
            time.sleep(min(0.05, remaining))

        stop = threading.Event()
        lock = threading.Lock()
        current = [lease]
        renewal_error: list[BaseException] = []

        def renew_periodically() -> None:
            interval = max(0.01, ttl / 3.0)
            while not stop.wait(interval):
                try:
                    with lock:
                        active = current[0]
                    renewed = self.renew_lease(active, ttl=ttl)
                    with lock:
                        current[0] = renewed
                except BaseException as exc:  # surfaced after a successful body
                    renewal_error.append(exc)
                    stop.set()
                    return

        thread = threading.Thread(
            target=renew_periodically,
            name=f"library-lease-{namespace}",
            daemon=True,
        )
        thread.start()
        body_failed = False
        try:
            yield lease
        except BaseException:
            body_failed = True
            raise
        finally:
            stop.set()
            thread.join()
            with lock:
                active = current[0]
            try:
                self.release_lease(active)
            except BaseException:
                if not body_failed:
                    raise
            if renewal_error and not body_failed:
                raise renewal_error[0]

    # ------------------------------------------------------- corpus and sources

    def register_corpus(
        self,
        path: Path | str,
        label: str,
        provenance_status: str = "verified",
    ) -> str:
        canonical_path = str(Path(path).expanduser().resolve())
        label = _required_text(label, "label")
        provenance_status = _required_text(provenance_status, "provenance_status")
        corpus_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "astro-pipeline-corpus\0" + canonical_path,
            )
        )
        now = float(self.clock())
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT corpus_id FROM corpora WHERE canonical_path = ?",
                (canonical_path,),
            ).fetchone()
            if row is not None:
                corpus_id = str(row["corpus_id"])
                connection.execute(
                    "UPDATE corpora SET label = ?, provenance_status = ?, updated_at = ? "
                    "WHERE corpus_id = ?",
                    (label, provenance_status, now, corpus_id),
                )
            else:
                connection.execute(
                    "INSERT INTO corpora(corpus_id, canonical_path, label, "
                    "provenance_status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        corpus_id,
                        canonical_path,
                        label,
                        provenance_status,
                        now,
                        now,
                    ),
                )
        return corpus_id

    def register_request(self, contract: Mapping[str, Any]) -> str:
        contract_json = canonical_json(dict(contract))
        request_id = identity_hash(dict(contract), namespace="library-request")
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT contract_json FROM requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is not None and row["contract_json"] != contract_json:
                raise RuntimeError("request hash collision")
            connection.execute(
                "INSERT OR IGNORE INTO requests(request_id, contract_json, created_at) "
                "VALUES (?, ?, ?)",
                (request_id, contract_json, float(self.clock())),
            )
        return request_id

    def start_run(
        self,
        corpus_id: str,
        request_id: str | None = None,
        options: Mapping[str, Any] | None = None,
        run_id: str | None = None,
    ) -> str:
        run_id = _required_text(run_id or str(uuid.uuid4()), "run_id")
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO runs(run_id, corpus_id, request_id, status, started_at, "
                "finished_at, options_json, error, metadata_json) "
                "VALUES (?, ?, ?, 'running', ?, NULL, ?, NULL, ?)",
                (
                    run_id,
                    _required_text(corpus_id, "corpus_id"),
                    request_id,
                    float(self.clock()),
                    canonical_json(dict(options or {})),
                    _JSON_OBJECT,
                ),
            )
        return run_id

    def _merged_run_metadata(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        metadata: Mapping[str, Any] | None,
    ) -> str:
        row = connection.execute(
            "SELECT metadata_json FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown run_id: {run_id}")
        merged = _loads(row["metadata_json"], {})
        merged.update(dict(metadata or {}))
        return canonical_json(merged)

    def finish_run(
        self,
        run_id: str,
        status: str,
        error: Any = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        status = _required_text(status, "status")
        with self.transaction() as connection:
            metadata_json = self._merged_run_metadata(connection, run_id, metadata)
            connection.execute(
                "UPDATE runs SET status = ?, finished_at = ?, error = ?, "
                "metadata_json = ? WHERE run_id = ?",
                (
                    status,
                    float(self.clock()),
                    None if error is None else str(error),
                    metadata_json,
                    run_id,
                ),
            )

    def update_run(
        self, run_id: str, metadata: Mapping[str, Any], *, lease: Lease | None = None,
    ) -> None:
        if not isinstance(metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        with self.transaction() as connection:
            if lease is not None:
                self.assert_lease(connection, lease)
            metadata_json = self._merged_run_metadata(connection, run_id, metadata)
            connection.execute(
                "UPDATE runs SET metadata_json = ? WHERE run_id = ?",
                (metadata_json, run_id),
            )

    def record_run_artifact(
        self,
        run_id: str,
        kind: str,
        path: Path | str,
        sha256: str,
        row_count: int | None = None,
    ) -> str:
        sha256 = str(sha256).lower()
        if not _SHA256_RE.fullmatch(sha256):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        if row_count is not None and int(row_count) < 0:
            raise ValueError("row_count must be non-negative")
        artifact_id = str(uuid.uuid4())
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO run_artifacts(artifact_id, run_id, kind, path, sha256, "
                "row_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    artifact_id,
                    run_id,
                    _required_text(kind, "kind"),
                    str(path),
                    sha256,
                    None if row_count is None else int(row_count),
                    float(self.clock()),
                ),
            )
        return artifact_id

    @staticmethod
    def _source_attributes(record: Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(record.get("attributes"), Mapping):
            candidate = dict(record["attributes"])
        else:
            candidate = dict(record)
        attributes: dict[str, Any] = {}
        for key, value in candidate.items():
            name = str(key)
            lowered = name.lower()
            if lowered in _SOURCE_IDENTITY_KEYS:
                continue
            if lowered in {"member_ordinal", "attributes", "match", "match_json"}:
                continue
            if lowered.startswith(_SOURCE_EXCLUDED_PREFIXES):
                continue
            attributes[name] = json_value(value)
        return attributes

    @staticmethod
    def _member_identity(record: Mapping[str, Any]) -> tuple[str, str]:
        source_id = record.get("source_id", record.get("Gaia"))
        if source_id is None:
            raise ValueError("member must contain source_id or Gaia")
        release = record.get("gaia_release", record.get("release"))
        if release is None:
            raise ValueError("member must contain gaia_release")
        return source_identity(str(release), source_id)

    @staticmethod
    def _input_record(
        record: Mapping[str, Any], fallback_ordinal: int
    ) -> tuple[str, int, str, Any]:
        ordinal = int(record.get("ordinal", fallback_ordinal))
        input_row_id = _required_text(
            record.get("input_row_id", str(ordinal)), "input_row_id"
        )
        status = _required_text(record.get("status", "pending"), "status")
        if "raw_json" in record:
            raw = record["raw_json"]
        elif "raw" in record:
            raw = record["raw"]
        else:
            raw = {
                str(key): value
                for key, value in record.items()
                if key not in {"input_row_id", "ordinal", "status"}
            }
        return input_row_id, ordinal, status, json_value(raw)

    @staticmethod
    def _match_records(
        matches: Sequence[Mapping[str, Any]],
    ) -> dict[int, Mapping[str, Any]]:
        result: dict[int, Mapping[str, Any]] = {}
        for fallback, match in enumerate(matches):
            ordinal = int(match.get("member_ordinal", fallback))
            if ordinal in result:
                raise ValueError(f"duplicate match member_ordinal: {ordinal}")
            result[ordinal] = match
        return result

    def _merge_source(
        self,
        connection: sqlite3.Connection,
        release: str,
        source_id: str,
        attributes: Mapping[str, Any],
        run_id: str,
        verified: bool,
        now: float,
    ) -> None:
        row = connection.execute(
            "SELECT attributes_json, covered_fields_json, field_provenance_json "
            "FROM sources WHERE gaia_release = ? AND source_id = ?",
            (release, source_id),
        ).fetchone()
        if row is None:
            merged: dict[str, Any] = {}
            covered: set[str] = set()
            provenance: dict[str, Any] = {}
        else:
            merged = _loads(row["attributes_json"], {})
            covered = set(_loads(row["covered_fields_json"], []))
            provenance = _loads(row["field_provenance_json"], {})
        for field, value in attributes.items():
            existing = provenance.get(field, {})
            existing_verified = field in covered or (
                isinstance(existing, Mapping) and bool(existing.get("verified"))
            )
            if not verified and existing_verified:
                continue
            merged[field] = json_value(value)
            provenance[field] = {
                "run_id": run_id,
                "verified": bool(verified),
                "updated_at": now,
            }
            if verified:
                covered.add(field)
        payload = (
            canonical_json(merged),
            canonical_json(sorted(covered)),
            canonical_json(provenance),
            now,
            release,
            source_id,
        )
        if row is None:
            connection.execute(
                "INSERT INTO sources(gaia_release, source_id, attributes_json, "
                "covered_fields_json, field_provenance_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    release,
                    source_id,
                    payload[0],
                    payload[1],
                    payload[2],
                    now,
                ),
            )
        else:
            connection.execute(
                "UPDATE sources SET attributes_json = ?, covered_fields_json = ?, "
                "field_provenance_json = ?, updated_at = ? "
                "WHERE gaia_release = ? AND source_id = ?",
                payload,
            )

    def index_members(
        self,
        run_id: str,
        members: Sequence[Mapping[str, Any]],
        input_rows: Sequence[Mapping[str, Any]] = (),
        matches: Sequence[Mapping[str, Any]] = (),
        verified: bool = True,
        lease: Lease | None = None,
    ) -> dict[str, int]:
        normalized_inputs = [
            self._input_record(record, index)
            for index, record in enumerate(input_rows)
        ]
        match_by_ordinal = self._match_records(matches)
        normalized_members: list[
            tuple[int, str, str, str | None, dict[str, Any], dict[str, Any]]
        ] = []
        identities: set[tuple[str, str]] = set()
        ordinals: set[int] = set()
        for fallback, member in enumerate(members):
            member_ordinal = int(member.get("member_ordinal", fallback))
            if member_ordinal in ordinals:
                raise ValueError(f"duplicate member_ordinal: {member_ordinal}")
            ordinals.add(member_ordinal)
            release, source_id = self._member_identity(member)
            identities.add((release, source_id))
            attributes = self._source_attributes(member)
            match = match_by_ordinal.get(member_ordinal)
            if match is None:
                match_payload = member.get("match_json", member.get("match", {}))
                input_row_id = member.get("input_row_id")
            else:
                match_payload = match.get("match_json", match.get("match"))
                if match_payload is None:
                    match_payload = {
                        str(key): value
                        for key, value in match.items()
                        if key not in {"member_ordinal", "input_row_id"}
                    }
                input_row_id = match.get(
                    "input_row_id", member.get("input_row_id")
                )
            normalized_members.append(
                (
                    member_ordinal,
                    release,
                    source_id,
                    None if input_row_id is None else str(input_row_id),
                    attributes,
                    json_value(match_payload or {}),
                )
            )
        now = float(self.clock())
        with self.transaction() as connection:
            if lease is not None:
                self.assert_lease(connection, lease)
            for input_row_id, ordinal, status, raw in normalized_inputs:
                connection.execute(
                    "INSERT INTO input_rows(run_id, input_row_id, ordinal, status, "
                    "raw_json) VALUES (?, ?, ?, ?, ?)",
                    (run_id, input_row_id, ordinal, status, canonical_json(raw)),
                )
            for _, release, source_id, _, attributes, _ in normalized_members:
                self._merge_source(
                    connection,
                    release,
                    source_id,
                    attributes,
                    run_id,
                    bool(verified),
                    now,
                )
            for (
                member_ordinal,
                release,
                source_id,
                input_row_id,
                attributes,
                match,
            ) in normalized_members:
                connection.execute(
                    "INSERT INTO sample_members(run_id, member_ordinal, release, "
                    "source_id, input_row_id, attributes_json, match_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        member_ordinal,
                        release,
                        source_id,
                        input_row_id,
                        canonical_json(attributes),
                        canonical_json(match),
                    ),
                )
        return {
            "n_input_rows": len(normalized_inputs),
            "n_sample_members": len(normalized_members),
            "n_unique_sources": len(identities),
        }

    def source(self, release: str, source_id: Any) -> dict[str, Any] | None:
        release, source_id = source_identity(release, source_id)
        rows = self.read(
            "SELECT gaia_release, source_id, attributes_json, covered_fields_json, "
            "field_provenance_json, updated_at FROM sources "
            "WHERE gaia_release = ? AND source_id = ?",
            (release, source_id),
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "gaia_release": row["gaia_release"],
            "source_id": row["source_id"],
            "attributes": _loads(row["attributes_json"], {}),
            "covered_fields": _loads(row["covered_fields_json"], []),
            "field_provenance": _loads(row["field_provenance_json"], {}),
            "updated_at": row["updated_at"],
        }

    # ----------------------------------------------------------- product blobs

    @staticmethod
    def _blob_record(blob: Blob | Mapping[str, Any]) -> dict[str, Any]:
        record = blob.to_dict() if isinstance(blob, Blob) else dict(blob)
        required = {
            "sha256",
            "relative_path",
            "size_bytes",
            "content_type",
            "extension",
        }
        missing = sorted(required.difference(record))
        if missing:
            raise ValueError(f"blob is missing fields: {', '.join(missing)}")
        record = {key: record[key] for key in required}
        record["sha256"] = str(record["sha256"]).lower()
        if not _SHA256_RE.fullmatch(record["sha256"]):
            raise ValueError("invalid blob sha256")
        record["relative_path"] = str(record["relative_path"])
        record["size_bytes"] = int(record["size_bytes"])
        record["content_type"] = str(record["content_type"])
        record["extension"] = str(record["extension"])
        return record

    def _insert_blob(
        self, connection: sqlite3.Connection, record: Mapping[str, Any]
    ) -> None:
        row = connection.execute(
            "SELECT relative_path, size_bytes, content_type, extension FROM blobs "
            "WHERE sha256 = ?",
            (record["sha256"],),
        ).fetchone()
        expected = (
            record["relative_path"],
            int(record["size_bytes"]),
            record["content_type"],
            record["extension"],
        )
        if row is not None:
            actual = (
                row["relative_path"],
                int(row["size_bytes"]),
                row["content_type"],
                row["extension"],
            )
            if actual != expected:
                raise BlobIntegrityError(
                    f"conflicting metadata for blob {record['sha256']}"
                )
            return
        connection.execute(
            "INSERT INTO blobs(sha256, relative_path, size_bytes, content_type, "
            "extension, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                record["sha256"],
                record["relative_path"],
                int(record["size_bytes"]),
                record["content_type"],
                record["extension"],
                float(self.clock()),
            ),
        )

    def register_blob(self, blob: Blob | Mapping[str, Any]) -> dict[str, Any]:
        record = self._blob_record(blob)
        self.blobs.verify(record)
        with self.transaction() as connection:
            self._insert_blob(connection, record)
        return dict(record)

    def save_product(
        self,
        contract: Mapping[str, Any],
        identifier: Any,
        artifacts: Sequence[Mapping[str, Any]],
        lease: Lease,
        normalizer_version: str = "1",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        contract = dict(contract)
        contract_hash = product_contract_hash(contract)
        identifier = normalize_identifier(identifier)
        normalizer_version = _required_text(
            normalizer_version, "normalizer_version"
        )
        item_key = product_item_key(
            contract,
            identifier,
            normalizer_version=normalizer_version,
        )
        if lease.namespace != "product" or lease.key != item_key:
            raise LeaseLostError("product lease does not match the product item key")
        if not artifacts:
            raise ValueError("a ready product must have at least one artifact")
        normalized_artifacts: list[dict[str, Any]] = []
        slots: set[tuple[str, str]] = set()
        for artifact in artifacts:
            role = str(artifact.get("role", "")).strip().lower()
            if role not in {"normalized", "archive"}:
                raise ValueError("artifact role must be 'normalized' or 'archive'")
            slot = _required_text(artifact.get("slot"), "artifact slot")
            pair = (role, slot)
            if pair in slots:
                raise ValueError(f"duplicate product artifact slot: {role}:{slot}")
            slots.add(pair)
            blob = self._blob_record(artifact.get("blob", {}))
            # Published bytes are checked before opening the SQLite transaction.
            self.blobs.verify(blob)
            normalized_artifacts.append(
                {
                    "role": role,
                    "slot": slot,
                    "blob": blob,
                    "metadata": json_value(artifact.get("metadata", {})),
                }
            )
        if not any(artifact["role"] == "normalized" for artifact in normalized_artifacts):
            raise ValueError("a ready product needs a normalized per-identifier artifact")
        contract_json = canonical_json(contract)
        version_id = str(uuid.uuid4())
        now = float(self.clock())
        with self.transaction() as connection:
            self.assert_lease(connection, lease)
            if contract.get("linking_parameter") == "SOURCE_ID":
                release, source_id = source_identity(contract["release"], identifier)
                self._merge_source(connection, release, source_id, {}, None, False, now)
            contract_row = connection.execute(
                "SELECT contract_json FROM product_contracts WHERE contract_hash = ?",
                (contract_hash,),
            ).fetchone()
            if contract_row is not None and contract_row["contract_json"] != contract_json:
                raise RuntimeError("product contract hash collision")
            connection.execute(
                "INSERT OR IGNORE INTO product_contracts(contract_hash, contract_json, "
                "created_at) VALUES (?, ?, ?)",
                (contract_hash, contract_json, now),
            )
            item_row = connection.execute(
                "SELECT contract_hash, identifier, normalizer_version "
                "FROM product_items WHERE item_key = ?",
                (item_key,),
            ).fetchone()
            if item_row is None:
                connection.execute(
                    "INSERT INTO product_items(item_key, contract_hash, identifier, "
                    "normalizer_version, state, active_version_id, updated_at, last_error) "
                    "VALUES (?, ?, ?, ?, 'pending', NULL, ?, NULL)",
                    (
                        item_key,
                        contract_hash,
                        identifier,
                        normalizer_version,
                        now,
                    ),
                )
            elif (
                item_row["contract_hash"] != contract_hash
                or item_row["identifier"] != identifier
                or item_row["normalizer_version"] != normalizer_version
            ):
                raise RuntimeError("product item key collision")
            connection.execute(
                "INSERT INTO product_versions(version_id, item_key, created_at, "
                "metadata_json) VALUES (?, ?, ?, ?)",
                (version_id, item_key, now, canonical_json(dict(metadata or {}))),
            )
            for artifact in normalized_artifacts:
                self._insert_blob(connection, artifact["blob"])
                connection.execute(
                    "INSERT INTO product_artifacts(artifact_id, version_id, role, slot, "
                    "blob_sha256, metadata_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        str(uuid.uuid4()),
                        version_id,
                        artifact["role"],
                        artifact["slot"],
                        artifact["blob"]["sha256"],
                        canonical_json(artifact["metadata"]),
                    ),
                )
            connection.execute(
                "UPDATE product_items SET state = 'ready', active_version_id = ?, "
                "updated_at = ?, last_error = NULL WHERE item_key = ?",
                (version_id, now, item_key),
            )
        result = self.get_product_version(version_id, verify=True)
        if result is None:  # pragma: no cover - a committed row cannot disappear here
            raise RuntimeError("saved product version disappeared")
        return result

    @staticmethod
    def _product_result(
        version: sqlite3.Row,
        artifacts: Sequence[sqlite3.Row],
    ) -> dict[str, Any]:
        return {
            "item_key": version["item_key"],
            "contract": _loads(version["contract_json"], {}),
            "identifier": version["identifier"],
            "version_id": version["version_id"],
            "created_at": version["created_at"],
            "metadata": _loads(version["metadata_json"], {}),
            "artifacts": [
                {
                    "role": row["role"],
                    "slot": row["slot"],
                    "blob": {
                        "sha256": row["sha256"],
                        "relative_path": row["relative_path"],
                        "size_bytes": row["size_bytes"],
                        "content_type": row["content_type"],
                        "extension": row["extension"],
                    },
                    "metadata": _loads(row["artifact_metadata_json"], {}),
                }
                for row in artifacts
            ],
        }

    def get_product_version(
        self, version_id: str, verify: bool = True
    ) -> dict[str, Any] | None:
        with self._readonly_connection() as connection:
            if connection is None:
                return None
            version = connection.execute(
                "SELECT v.version_id, v.item_key, v.created_at, v.metadata_json, "
                "i.identifier, c.contract_json FROM product_versions AS v "
                "JOIN product_items AS i ON i.item_key = v.item_key "
                "JOIN product_contracts AS c ON c.contract_hash = i.contract_hash "
                "WHERE v.version_id = ?",
                (version_id,),
            ).fetchone()
            if version is None:
                return None
            artifacts = connection.execute(
                "SELECT a.role, a.slot, a.metadata_json AS artifact_metadata_json, "
                "b.sha256, b.relative_path, b.size_bytes, b.content_type, b.extension "
                "FROM product_artifacts AS a "
                "JOIN blobs AS b ON b.sha256 = a.blob_sha256 "
                "WHERE a.version_id = ? ORDER BY a.role, a.slot",
                (version_id,),
            ).fetchall()
            result = self._product_result(version, artifacts)
        if verify:
            for artifact in result["artifacts"]:
                self.blobs.verify(artifact["blob"])
        return result

    def get_product(
        self, item_key: str, verify: bool = True
    ) -> dict[str, Any] | None:
        rows = self.read(
            "SELECT active_version_id FROM product_items "
            "WHERE item_key = ? AND state = 'ready' "
            "AND active_version_id IS NOT NULL",
            (item_key,),
        )
        if not rows:
            return None
        return self.get_product_version(rows[0]["active_version_id"], verify=verify)

    def record_product_error(self, item_key: str, error: Any) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE product_items SET state = CASE "
                "WHEN active_version_id IS NULL THEN 'failed' ELSE 'ready' END, "
                "updated_at = ?, last_error = ? WHERE item_key = ?",
                (float(self.clock()), str(error), item_key),
            )
            return cursor.rowcount == 1

    # --------------------------------------------------------------- attempts

    def record_attempt(
        self,
        namespace: str,
        key: str,
        request: Any,
        run_id: str | None = None,
    ) -> str:
        attempt_id = str(uuid.uuid4())
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO attempts(id, namespace, key, request_json, run_id, status, "
                "started_at, finished_at, error) "
                "VALUES (?, ?, ?, ?, ?, 'running', ?, NULL, NULL)",
                (
                    attempt_id,
                    _required_text(namespace, "namespace"),
                    _required_text(key, "key"),
                    canonical_json(request),
                    run_id,
                    float(self.clock()),
                ),
            )
        return attempt_id

    def finish_attempt(
        self, attempt_id: str, status: str, error: Any = None
    ) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE attempts SET status = ?, finished_at = ?, error = ? "
                "WHERE id = ?",
                (
                    _required_text(status, "status"),
                    float(self.clock()),
                    None if error is None else str(error),
                    attempt_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown attempt id: {attempt_id}")

    # -------------------------------------------------------------- TTL cache

    def get_cache(
        self, namespace: str, key: str, include_stale: bool = False
    ) -> dict[str, Any] | None:
        rows = self.read(
            "SELECT o.id, o.outcome, o.payload_json, o.observed_at, o.expires_at, "
            "o.error FROM cache_current AS c "
            "JOIN cache_observations AS o ON o.id = c.observation_id "
            "WHERE c.namespace = ? AND c.cache_key = ?",
            (namespace, key),
        )
        if not rows:
            return None
        row = rows[0]
        stale = float(row["expires_at"]) <= float(self.clock())
        if stale and not include_stale:
            return None
        return {
            "id": row["id"],
            "payload": _loads(row["payload_json"], None),
            "outcome": row["outcome"],
            "observed_at": row["observed_at"],
            "expires_at": row["expires_at"],
            "stale": stale,
            "error": row["error"],
        }

    def put_cache(
        self,
        namespace: str,
        key: str,
        payload: Any,
        outcome: str = "success",
        ttl_seconds: float = 30 * 24 * 60 * 60,
        lease: Lease | None = None,
        error: Any = None,
    ) -> str:
        namespace = _required_text(namespace, "namespace")
        key = _required_text(key, "key")
        outcome = _required_text(outcome, "outcome")
        ttl_seconds = _ttl(ttl_seconds)
        observation_id = str(uuid.uuid4())
        observed_at = float(self.clock())
        expires_at = observed_at + ttl_seconds
        with self.transaction() as connection:
            if lease is not None:
                self.assert_lease(connection, lease)
            connection.execute(
                "INSERT INTO cache_observations(id, namespace, cache_key, outcome, "
                "payload_json, observed_at, expires_at, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    observation_id,
                    namespace,
                    key,
                    outcome,
                    canonical_json(payload),
                    observed_at,
                    expires_at,
                    None if error is None else str(error),
                ),
            )
            if outcome.lower() not in _FAILURE_OUTCOMES:
                connection.execute(
                    "INSERT INTO cache_current(namespace, cache_key, observation_id) "
                    "VALUES (?, ?, ?) ON CONFLICT(namespace, cache_key) DO UPDATE "
                    "SET observation_id = excluded.observation_id",
                    (namespace, key, observation_id),
                )
        return observation_id

    # ------------------------------------------------------- papers and refs

    def _upsert_paper(
        self, connection: sqlite3.Connection, metadata: Mapping[str, Any]
    ) -> dict[str, Any]:
        supplied = {str(key): json_value(value) for key, value in metadata.items()}
        bibcode = _required_text(supplied.get("bibcode"), "paper bibcode")
        row = connection.execute(
            "SELECT metadata_json FROM papers WHERE bibcode = ?", (bibcode,)
        ).fetchone()
        merged = {} if row is None else _loads(row["metadata_json"], {})
        for key, value in supplied.items():
            if not _is_blank(value):
                merged[key] = value
        merged["bibcode"] = bibcode
        now = float(self.clock())
        if row is None:
            connection.execute(
                "INSERT INTO papers(bibcode, metadata_json, updated_at) VALUES (?, ?, ?)",
                (bibcode, canonical_json(merged), now),
            )
        else:
            connection.execute(
                "UPDATE papers SET metadata_json = ?, updated_at = ? WHERE bibcode = ?",
                (canonical_json(merged), now, bibcode),
            )
        return merged

    def upsert_paper(self, metadata: Mapping[str, Any]) -> dict[str, Any]:
        with self.transaction() as connection:
            return self._upsert_paper(connection, metadata)

    def put_source_references(
        self,
        release: str,
        source_id: Any,
        papers: Sequence[Mapping[str, Any]],
        main_id: str = "",
        outcome: str = "success",
        ttl_seconds: float = 30 * 24 * 60 * 60,
        lease: Lease | None = None,
    ) -> str:
        release, source_id = source_identity(release, source_id)
        outcome = _required_text(outcome, "outcome")
        ttl_seconds = _ttl(ttl_seconds)
        failed = outcome.lower() in _FAILURE_OUTCOMES
        if failed and papers:
            raise ValueError("a failed source-reference observation cannot contain papers")
        snapshot_id = str(uuid.uuid4())
        observed_at = float(self.clock())
        expires_at = observed_at + ttl_seconds
        with self.transaction() as connection:
            if lease is not None:
                self.assert_lease(connection, lease)
            connection.execute(
                "INSERT OR IGNORE INTO sources(gaia_release, source_id, attributes_json, "
                "covered_fields_json, field_provenance_json, updated_at) "
                "VALUES (?, ?, '{}', '[]', '{}', ?)",
                (release, source_id, observed_at),
            )
            connection.execute(
                "INSERT INTO source_ref_snapshots(snapshot_id, release, source_id, "
                "outcome, main_id, observed_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    release,
                    source_id,
                    outcome,
                    str(main_id),
                    observed_at,
                    expires_at,
                ),
            )
            seen: set[str] = set()
            for metadata in papers:
                merged = self._upsert_paper(connection, metadata)
                bibcode = merged["bibcode"]
                if bibcode in seen:
                    continue
                seen.add(bibcode)
                connection.execute(
                    "INSERT INTO source_papers(snapshot_id, bibcode) VALUES (?, ?)",
                    (snapshot_id, bibcode),
                )
            if not failed:
                connection.execute(
                    "INSERT INTO source_ref_current(release, source_id, snapshot_id) "
                    "VALUES (?, ?, ?) ON CONFLICT(release, source_id) DO UPDATE "
                    "SET snapshot_id = excluded.snapshot_id",
                    (release, source_id, snapshot_id),
                )
        return snapshot_id

    def get_source_references(
        self,
        release: str,
        source_id: Any,
        include_stale: bool = False,
    ) -> dict[str, Any] | None:
        release, source_id = source_identity(release, source_id)
        with self._readonly_connection() as connection:
            if connection is None:
                return None
            snapshot = connection.execute(
                "SELECT s.snapshot_id, s.release, s.source_id, s.outcome, s.main_id, "
                "s.observed_at, s.expires_at FROM source_ref_current AS c "
                "JOIN source_ref_snapshots AS s ON s.snapshot_id = c.snapshot_id "
                "WHERE c.release = ? AND c.source_id = ?",
                (release, source_id),
            ).fetchone()
            if snapshot is None:
                return None
            stale = float(snapshot["expires_at"]) <= float(self.clock())
            if stale and not include_stale:
                return None
            paper_rows = connection.execute(
                "SELECT p.metadata_json FROM source_papers AS sp "
                "JOIN papers AS p ON p.bibcode = sp.bibcode "
                "WHERE sp.snapshot_id = ? ORDER BY p.bibcode",
                (snapshot["snapshot_id"],),
            ).fetchall()
            return {
                "snapshot_id": snapshot["snapshot_id"],
                "release": snapshot["release"],
                "source_id": snapshot["source_id"],
                "papers": [_loads(row["metadata_json"], {}) for row in paper_rows],
                "main_id": snapshot["main_id"],
                "outcome": snapshot["outcome"],
                "observed_at": snapshot["observed_at"],
                "expires_at": snapshot["expires_at"],
                "stale": stale,
            }
