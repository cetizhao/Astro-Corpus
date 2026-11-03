"""Read-only, revision-pinned queries for the global library viewer.

The viewer intentionally has its own compact query layer instead of calling the
point-at-a-time :mod:`astro_pipeline.library.query` helpers.  Every response is
assembled from a bounded number of SQL statements in one short SQLite read
transaction, so page size does not turn into an N+1 query pattern.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import base64
import binascii
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import unicodedata
from typing import Any, Iterator, Mapping, Sequence

from astro_pipeline.gaia_releases import get_gaia_release_profile, normalize_gaia_release
from astro_pipeline.library.availability import NAMESPACE as XP_AVAILABILITY_NAMESPACE
from astro_pipeline.library.identity import canonical_json, source_cache_key, source_identity
from astro_pipeline.library.refs import ProductReferenceError, active_corpus_dir
from astro_pipeline.library.store import LibraryStore

DEFAULT_LIMIT = 100
MAX_LIMIT = 500
SCOPES = frozenset({"all_indexed", "current_datasets"})
SPECTRUM_STATES = frozenset(
    {
        "all",
        "ready",
        "pending",
        "failed",
        "not_downloaded",
        "unavailable",
        "unknown",
        "missing",
        "not_ready",
        # Backward-compatible query alias; DTOs always emit not_downloaded.
        "available",
    }
)
_PRODUCT_SUMMARY_FIELDS = (
    "item_key",
    "retrieval_type",
    "state",
    "active_version_id",
    "updated_at",
    "has_error",
    "normalizer_version",
    "data_structure",
    "output_format",
)


class SnapshotChanged(RuntimeError):
    """Raised when a client continues pagination against a changed database."""

    def __init__(self, current_revision: str) -> None:
        super().__init__("library snapshot changed; restart pagination")
        self.current_revision = current_revision


class InvalidCursor(ValueError):
    """Raised for malformed cursors or cursors used with different filters."""


@dataclass(frozen=True)
class SourceFilters:
    """Validated filters shared by source-table and sky pages."""

    q: str | None = None
    release: str | None = None
    corpus_id: str | None = None
    scope: str = "all_indexed"
    spectrum_state: str = "all"
    evidence_state: str = "all"

    @classmethod
    def create(
        cls,
        *,
        q: str | None = None,
        release: str | None = None,
        corpus_id: str | None = None,
        scope: str = "all_indexed",
        spectrum_state: str = "all",
        evidence_state: str = "all",
    ) -> SourceFilters:
        query = None if q is None else str(q).strip()
        if query == "":
            query = None
        if query is not None and len(query) > 200:
            raise ValueError("q must be at most 200 characters")
        normalized_release = (
            None if release is None or not str(release).strip() else normalize_gaia_release(release)
        )
        corpus = None if corpus_id is None else str(corpus_id).strip()
        if corpus == "":
            corpus = None
        if corpus is not None and len(corpus) > 128:
            raise ValueError("corpus_id must be at most 128 characters")
        normalized_scope = str(scope).strip().lower()
        if normalized_scope not in SCOPES:
            raise ValueError(f"scope must be one of: {', '.join(sorted(SCOPES))}")
        normalized_spectrum = str(spectrum_state).strip().lower()
        if normalized_spectrum not in SPECTRUM_STATES:
            raise ValueError(
                "spectrum_state must be one of: "
                + ", ".join(sorted(SPECTRUM_STATES))
            )
        if normalized_spectrum == "available":
            normalized_spectrum = "not_downloaded"
        if evidence_state not in {"all", "recorded", "not_assessed"}:
            raise ValueError("evidence_state must be all, recorded or not_assessed")
        return cls(
            q=query,
            release=normalized_release,
            corpus_id=corpus,
            scope=normalized_scope,
            spectrum_state=normalized_spectrum,
            evidence_state=evidence_state,
        )

    @property
    def fingerprint(self) -> str:
        payload = {
            "q": self.q,
            "release": self.release,
            "corpus_id": self.corpus_id,
            "scope": self.scope,
            "spectrum_state": self.spectrum_state,
            "evidence_state": self.evidence_state,
        }
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:24]


def _current_run_pin(canonical_path: str) -> str | None:
    """Return the authoritative run, None for legacy fallback, or '' on failure."""
    corpus = Path(canonical_path).expanduser().resolve()
    manifest_path = corpus / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return ""
    if not isinstance(manifest, dict):
        return ""
    snapshot = manifest.get("snapshot_run_id")
    if snapshot not in (None, ""):
        return (
            snapshot
            if isinstance(snapshot, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", snapshot)
            else ""
        )
    storage = manifest.get("storage")
    if not isinstance(storage, dict) or storage.get("kind") != "shared_library":
        return None
    try:
        effective = active_corpus_dir(corpus).resolve()
    except (ProductReferenceError, OSError, ValueError):
        return ""
    run_root = (corpus / "runs").resolve()
    return effective.name if effective.parent == run_root else ""


class RevisionMonitor:
    """Serialize access to one persistent read-only ``data_version`` connection.

    SQLite's ``PRAGMA data_version`` is meaningful only when successive reads use
    the same connection.  The monitor therefore never compares values from fresh
    connections.  File identity is checked separately so atomic database
    replacement starts a new generation even if the replacement happens to have
    the same SQLite data-version counter.
    """

    def __init__(self, store: LibraryStore | None) -> None:
        self.store = store
        self.server_nonce = secrets.token_urlsafe(12)
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self._file_identity: tuple[int, int] | None = None
        self._generation = 0

    @staticmethod
    def _identity(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        if not path.is_file():
            return None
        return int(stat.st_dev), int(stat.st_ino)

    def _close_locked(self) -> None:
        if self._connection is not None:
            self._connection.close()
        self._connection = None

    def _connect_locked(self, identity: tuple[int, int]) -> None:
        assert self.store is not None
        uri = self.store.db_path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=self.store.busy_timeout_ms / 1000.0,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.store.busy_timeout_ms}")
        try:
            self.store._reject_newer_schema(self.store._schema_version(connection))
            self.store._validate_owner(connection)
        except BaseException:
            connection.close()
            raise
        self._connection = connection
        self._file_identity = identity

    def current(self) -> tuple[str, bool]:
        with self._lock:
            if self.store is None:
                return f"{self.server_nonce}.0.absent", False
            identity = self._identity(self.store.db_path)
            if identity != self._file_identity:
                self._close_locked()
                self._file_identity = identity
                self._generation += 1
            if identity is None:
                return f"{self.server_nonce}.{self._generation}.absent", False
            if self._connection is None:
                self._connect_locked(identity)
            assert self._connection is not None
            data_version = int(
                self._connection.execute("PRAGMA data_version").fetchone()[0]
            )
            # Root manifests can be repinned without a SQLite write.  Include
            # their effective selections so old pagination revisions still fail.
            pins = [
                (str(row["corpus_id"]), _current_run_pin(str(row["canonical_path"])))
                for row in self._connection.execute(
                    "SELECT corpus_id, canonical_path FROM corpora ORDER BY corpus_id"
                ).fetchall()
            ]
            pin_revision = hashlib.sha256(canonical_json(pins).encode("utf-8")).hexdigest()[:16]
            revision = f"{self.server_nonce}.{self._generation}.{data_version}.{pin_revision}"
            return revision, True

    def require(self, requested: str | None) -> tuple[str, bool]:
        revision, exists = self.current()
        if requested is not None and requested != revision:
            raise SnapshotChanged(revision)
        return revision, exists

    def ensure_unchanged(self, revision: str) -> None:
        current, _ = self.current()
        if current != revision:
            raise SnapshotChanged(current)

    def close(self) -> None:
        with self._lock:
            self._close_locked()
            self._file_identity = None


# Manifest pins select the published member snapshot.  Timestamp ordering is
# only a fallback for legacy corpora without a shared/snapshot manifest pin.
# Invalid pins and unpublished runs must never expose a different dataset.
_CURRENT_RUNS_CTE = """
ranked_current_runs AS (
    SELECT
        r.run_id,
        r.corpus_id,
        r.request_id,
        r.status,
        r.started_at,
        r.finished_at,
        r.options_json,
        r.error,
        r.metadata_json,
        c.label,
        c.canonical_path,
        c.provenance_status,
        ROW_NUMBER() OVER (
            PARTITION BY r.corpus_id
            ORDER BY COALESCE(r.finished_at, r.started_at) DESC,
                     r.started_at DESC,
                     r.run_id DESC
        ) AS current_rank
    FROM runs AS r
    JOIN corpora AS c ON c.corpus_id = r.corpus_id
    WHERE json_extract(r.metadata_json, '$.members_indexed') = 1
      AND json_extract(r.metadata_json, '$.published') = 1
      AND (current_run_pin(c.canonical_path) IS NULL
           OR r.run_id = current_run_pin(c.canonical_path))
),
current_runs AS (
    SELECT * FROM ranked_current_runs WHERE current_rank = 1
)
"""

_NAME_FIELDS = ("main_id", "simbad_main_id", "name", "Name", "source_name", "object_name")
_CATALOG_NAME_FIELDS = frozenset(
    field.lower() for field in (*_NAME_FIELDS, *(f"member_{field}" for field in _NAME_FIELDS))
)


def _search_key(value: Any) -> str:
    """Normalize spelling for lookup only; never merge source identities."""
    return "".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _catalog_names_json(document: str | None) -> str:
    # Some immutable input snapshots store the original row as a JSON string.
    # Read both representations without rewriting the snapshot or its checksum.
    parsed = _loads(document, {})
    if isinstance(parsed, str):
        parsed = _loads(parsed, {})
    if not isinstance(parsed, dict):
        return "[]"
    return json.dumps([
        {"field": field, "name": value.strip()}
        for field, value in parsed.items()
        if field.lower() in _CATALOG_NAME_FIELDS and isinstance(value, str) and value.strip()
    ], ensure_ascii=False)


_CURRENT_NAMES_CTE = """
current_name_documents AS (
    SELECT sm.release, sm.source_id, sm.input_row_id,
           cr.run_id, cr.corpus_id, cr.label,
           ir.raw_json AS document, 'input_row' AS origin
    FROM sample_members sm JOIN current_runs cr ON cr.run_id = sm.run_id
    JOIN input_rows ir ON ir.run_id = sm.run_id AND ir.input_row_id = sm.input_row_id
    UNION ALL
    SELECT sm.release, sm.source_id, sm.input_row_id,
           cr.run_id, cr.corpus_id, cr.label,
           sm.attributes_json AS document, 'sample_member' AS origin
    FROM sample_members sm JOIN current_runs cr ON cr.run_id = sm.run_id
),
catalog_names AS (
    SELECT d.release, d.source_id, d.input_row_id,
           d.run_id, d.corpus_id, d.label, d.origin,
           json_extract(n.value, '$.field') AS field,
           json_extract(n.value, '$.name') AS name
    FROM current_name_documents d, json_each(catalog_names_json(d.document)) n
),
catalog_name_groups AS (
    SELECT release, source_id, json_group_array(json_object(
        'name', name, 'field', field, 'origin', origin,
        'run_id', run_id, 'corpus_id', corpus_id, 'label', label,
        'input_row_id', input_row_id
    )) AS catalog_names_json
    FROM (SELECT DISTINCT * FROM catalog_names
          ORDER BY name, label, run_id, input_row_id, origin, field)
    GROUP BY release, source_id
)
"""

_PRODUCT_RELEASE_SQL = """
CASE lower(replace(trim(json_extract(pc.contract_json, '$.release')), '-', ' '))
    WHEN '3' THEN 'dr3'
    WHEN 'dr3' THEN 'dr3'
    WHEN 'gaiadr3' THEN 'dr3'
    WHEN 'gaia dr3' THEN 'dr3'
    WHEN 'gaia_dr3' THEN 'dr3'
    WHEN '4' THEN 'dr4'
    WHEN 'dr4' THEN 'dr4'
    WHEN 'gaiadr4' THEN 'dr4'
    WHEN 'gaia dr4' THEN 'dr4'
    WHEN 'gaia_dr4' THEN 'dr4'
    ELSE lower(trim(json_extract(pc.contract_json, '$.release')))
END
"""

_SOURCE_VIEW_CTE = f"""
{_CURRENT_RUNS_CTE},
{_CURRENT_NAMES_CTE},
xp_product_states AS (
    SELECT
        {_PRODUCT_RELEASE_SQL} AS release,
        pi.identifier AS source_id,
        MAX(CASE WHEN pi.state = 'ready'
                  AND pi.active_version_id IS NOT NULL
                 THEN 1 ELSE 0 END) AS is_ready,
        MAX(CASE WHEN pi.state = 'pending' THEN 1 ELSE 0 END) AS is_pending,
        MAX(CASE WHEN pi.state = 'failed' THEN 1 ELSE 0 END) AS is_failed
    FROM product_items AS pi
    JOIN product_contracts AS pc ON pc.contract_hash = pi.contract_hash
    WHERE upper(COALESCE(json_extract(pc.contract_json, '$.linking_parameter'), '')) = 'SOURCE_ID'
      AND upper(COALESCE(json_extract(pc.contract_json, '$.retrieval_type'), ''))
          IN ('XP_SAMPLED', 'XP_CONTINUOUS')
    GROUP BY release, pi.identifier
),
source_view AS (
    SELECT
        s.gaia_release,
        s.source_id,
        s.attributes_json,
        s.covered_fields_json,
        s.field_provenance_json,
        s.updated_at,
        COALESCE(names.catalog_names_json, '[]') AS catalog_names_json,
        ref.snapshot_id AS ref_snapshot_id,
        ref.outcome AS ref_outcome,
        ref.main_id AS ref_main_id,
        ref.observed_at AS ref_observed_at,
        ref.expires_at AS ref_expires_at,
        obs.id AS availability_id,
        obs.outcome AS availability_outcome,
        obs.payload_json AS availability_payload_json,
        obs.observed_at AS availability_observed_at,
        obs.expires_at AS availability_expires_at,
        obs.error AS availability_error,
        CASE
            WHEN COALESCE(xp.is_ready, 0) = 1 THEN 'ready'
            WHEN COALESCE(xp.is_pending, 0) = 1 THEN 'pending'
            WHEN COALESCE(xp.is_failed, 0) = 1 THEN 'failed'
            WHEN json_extract(s.attributes_json, '$.has_xp_sampled') = 1
              OR json_extract(s.attributes_json, '$.has_xp_continuous') = 1
              OR json_extract(obs.payload_json, '$.has_xp_sampled') = 1
              OR json_extract(obs.payload_json, '$.has_xp_continuous') = 1
                THEN 'not_downloaded'
            WHEN (
                    json_type(s.attributes_json, '$.has_xp_sampled') IS NOT NULL
                 OR json_type(s.attributes_json, '$.has_xp_continuous') IS NOT NULL
                 )
              AND COALESCE(json_extract(s.attributes_json, '$.has_xp_sampled'), 0) = 0
              AND COALESCE(json_extract(s.attributes_json, '$.has_xp_continuous'), 0) = 0
                THEN 'unavailable'
            WHEN obs.id IS NOT NULL
              AND COALESCE(json_extract(obs.payload_json, '$.has_xp_sampled'), 0) = 0
              AND COALESCE(json_extract(obs.payload_json, '$.has_xp_continuous'), 0) = 0
                THEN 'unavailable'
            ELSE 'unknown'
        END AS spectrum_status
    FROM sources AS s
    LEFT JOIN catalog_name_groups names
      ON names.release = s.gaia_release AND names.source_id = s.source_id
    LEFT JOIN source_ref_current AS ref_current
      ON ref_current.release = s.gaia_release
     AND ref_current.source_id = s.source_id
    LEFT JOIN source_ref_snapshots AS ref
      ON ref.snapshot_id = ref_current.snapshot_id
    LEFT JOIN cache_current AS availability_current
      ON availability_current.namespace = '{XP_AVAILABILITY_NAMESPACE}'
     AND availability_current.cache_key = source_cache_key(s.gaia_release, s.source_id)
    LEFT JOIN cache_observations AS obs
      ON obs.id = availability_current.observation_id
    LEFT JOIN xp_product_states AS xp
      ON xp.release = s.gaia_release AND xp.source_id = s.source_id
)
"""

def _source_view_cte(connection: sqlite3.Connection) -> str:
    # Schema 1/2 remain readable without creating or migrating evidence tables.
    base = _SOURCE_VIEW_CTE.replace("source_view AS (", "base_source_view AS (")
    present = connection.execute("PRAGMA user_version").fetchone()[0] >= 3
    counts = "SELECT NULL AS release, NULL AS source_id, 0 AS annotation_count, 0 AS annotation_paper_count WHERE 0"
    if present:
        counts = """SELECT a.release, a.source_id, COUNT(*) AS annotation_count,
            COUNT(DISTINCT v.work_id) AS annotation_paper_count
            FROM paper_annotations a JOIN paper_annotation_batches b USING(batch_id)
            JOIN paper_text_versions v ON v.version_id = b.paper_version_id
            GROUP BY a.release, a.source_id"""
    return base + f""", evidence_counts AS ({counts}), source_view AS (
        SELECT sv.*, COALESCE(e.annotation_count, 0) AS annotation_count,
        COALESCE(e.annotation_paper_count, 0) AS annotation_paper_count
        FROM base_source_view sv LEFT JOIN evidence_counts e
        ON e.release = sv.gaia_release AND e.source_id = sv.source_id)"""


_DR3_VALID_POSITION_SQL = """
sv.gaia_release = 'dr3'
AND json_type(sv.attributes_json, '$.RAICRS') IN ('integer', 'real')
AND json_type(sv.attributes_json, '$.DEICRS') IN ('integer', 'real')
AND CAST(json_extract(sv.attributes_json, '$.RAICRS') AS REAL) >= 0.0
AND CAST(json_extract(sv.attributes_json, '$.RAICRS') AS REAL) < 360.0
AND CAST(json_extract(sv.attributes_json, '$.DEICRS') AS REAL) >= -90.0
AND CAST(json_extract(sv.attributes_json, '$.DEICRS') AS REAL) <= 90.0
"""

# DR4 has no activated project-level canonical aliases yet.  Native, explicitly
# named ``ra``/``dec`` values can be displayed, but DR3-suffixed fields are never
# guessed for a DR4 row.
_DR4_VALID_POSITION_SQL = """
sv.gaia_release = 'dr4'
AND json_type(sv.attributes_json, '$.ra') IN ('integer', 'real')
AND json_type(sv.attributes_json, '$.dec') IN ('integer', 'real')
AND CAST(json_extract(sv.attributes_json, '$.ra') AS REAL) >= 0.0
AND CAST(json_extract(sv.attributes_json, '$.ra') AS REAL) < 360.0
AND CAST(json_extract(sv.attributes_json, '$.dec') AS REAL) >= -90.0
AND CAST(json_extract(sv.attributes_json, '$.dec') AS REAL) <= 90.0
"""
_VALID_POSITION_SQL = f"(({_DR3_VALID_POSITION_SQL}) OR ({_DR4_VALID_POSITION_SQL}))"


def _loads(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default
    return parsed


def _object(value: str | None) -> dict[str, Any]:
    parsed = _loads(value, {})
    return parsed if isinstance(parsed, dict) else {}


def _list(value: str | None) -> list[Any]:
    parsed = _loads(value, [])
    return parsed if isinstance(parsed, list) else []


_PUBLIC_RUN_METADATA = frozenset(
    {
        "kind",
        "members_indexed",
        "published",
        "prepared",
        "view_pending",
        "legacy_unverified",
        "n_input_rows",
        "n_sample_members",
        "n_unique_sources",
    }
)


def _public_run_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    """Expose lifecycle/count fields without browser-visible local paths."""

    return {
        key: value[key]
        for key in _PUBLIC_RUN_METADATA
        if key in value
        and (
            value[key] is None
            or isinstance(value[key], (str, bool, int, float))
        )
    }


def _without_local_paths(value: Any) -> Any:
    """Remove path-bearing keys and absolute local locators from detail JSON."""

    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            folded = key.casefold().replace("-", "_")
            if (
                "path" in folded
                or "root" in folded
                or "directory" in folded
                or folded == "dir"
                or folded.endswith("_dir")
            ):
                continue
            cleaned[key] = _without_local_paths(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_without_local_paths(item) for item in value]
    if isinstance(value, str) and (
        value.startswith(("/", "file://"))
        or re.match(r"^[A-Za-z]:[\\\\/]", value) is not None
    ):
        return None
    return value


def _finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _display_name(
    release: str, source_id: str, attributes: Mapping[str, Any], main_id: Any,
    catalog_names: Sequence[Mapping[str, Any]] = (),
) -> str:
    candidates = (
        main_id,
        *(attributes.get(field) for field in _NAME_FIELDS),
        *(item["name"] for item in catalog_names),
    )
    for candidate in candidates:
        if candidate is not None and str(candidate).strip():
            return str(candidate).strip()
    return f"Gaia {release.upper()} {source_id}"


def _position_and_astrometry(
    release: str,
    attributes: Mapping[str, Any],
    covered_fields: Sequence[Any] = (),
    field_provenance: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if release == "dr3":
        profile = get_gaia_release_profile("dr3")
        ra_field, dec_field = "RAICRS", "DEICRS"
        ra = _finite_number(attributes.get(ra_field))
        dec = _finite_number(attributes.get(dec_field))
        astrometry = {
            "parallax_mas": _finite_number(attributes.get("Plx_gdr3")),
            "pmra_masyr": _finite_number(attributes.get("pmRA_gdr3")),
            "pmdec_masyr": _finite_number(attributes.get("pmDE_gdr3")),
            "radial_velocity_kms": _finite_number(attributes.get("RV_gdr3")),
            "g_mag": _finite_number(attributes.get("Gmag_gdr3")),
            "bp_mag": _finite_number(attributes.get("BPmag_gdr3")),
            "rp_mag": _finite_number(attributes.get("RPmag_gdr3")),
            "ruwe": _finite_number(attributes.get("RUWE_gdr3")),
        }
        trusted_epoch_status = "release_reference_epoch"
    else:
        # Native DR4 names are explicit; never reinterpret *_gdr3 fields.
        profile = get_gaia_release_profile(release)
        ra_field, dec_field = "ra", "dec"
        ra = _finite_number(attributes.get(ra_field))
        dec = _finite_number(attributes.get(dec_field))
        astrometry = {
            "parallax_mas": _finite_number(attributes.get("parallax")),
            "pmra_masyr": _finite_number(attributes.get("pmra")),
            "pmdec_masyr": _finite_number(attributes.get("pmdec")),
            "radial_velocity_kms": _finite_number(attributes.get("radial_velocity")),
            "g_mag": _finite_number(attributes.get("phot_g_mean_mag")),
            "bp_mag": _finite_number(attributes.get("phot_bp_mean_mag")),
            "rp_mag": _finite_number(attributes.get("phot_rp_mean_mag")),
            "ruwe": _finite_number(attributes.get("ruwe")),
        }
        trusted_epoch_status = "provisional_release_reference_epoch"
    valid = bool(
        ra is not None
        and dec is not None
        and 0.0 <= ra < 360.0
        and -90.0 <= dec <= 90.0
    )
    provenance = field_provenance or {}
    covered = {str(field) for field in covered_fields}

    def field_is_verified(field: str) -> bool:
        evidence = provenance.get(field)
        return field in covered or (
            isinstance(evidence, Mapping) and evidence.get("verified") is True
        )

    coordinates_verified = bool(
        valid and field_is_verified(ra_field) and field_is_verified(dec_field)
    )
    if not valid:
        epoch = None
        epoch_status = "unavailable"
    elif coordinates_verified:
        epoch = profile.reference_epoch
        epoch_status = trusted_epoch_status
    else:
        epoch = None
        epoch_status = "legacy_or_unverified"
    return (
        {
            "ra_deg": ra if valid else None,
            "dec_deg": dec if valid else None,
            "frame": "ICRS",
            "epoch": epoch,
            "epoch_status": epoch_status,
            "valid": valid,
        },
        astrometry,
    )


def _provenance_status(
    datasets: Sequence[Mapping[str, Any]], working_datasets: Sequence[Mapping[str, Any]]
) -> str:
    statuses = {
        str(dataset.get("provenance_status") or "").strip()
        for dataset in datasets
        if str(dataset.get("provenance_status") or "").strip()
    }
    if not statuses:
        statuses = {
            str(dataset.get("provenance_status") or "").strip()
            for dataset in working_datasets
            if str(dataset.get("provenance_status") or "").strip()
        }
    if not statuses:
        return "unassociated"
    if len(statuses) == 1:
        return next(iter(statuses))
    return "mixed"


def _source_dto(
    row: Mapping[str, Any],
    datasets: Sequence[Mapping[str, Any]],
    working_datasets: Sequence[Mapping[str, Any]],
    products: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    release = str(row["gaia_release"])
    source_id = str(row["source_id"])
    attributes = _object(row.get("attributes_json"))
    catalog_names = _list(row.get("catalog_names_json"))
    position, astrometry = _position_and_astrometry(
        release,
        attributes,
        _list(row.get("covered_fields_json")),
        _object(row.get("field_provenance_json")),
    )
    return {
        "key": f"{release}:{source_id}",
        "release": release,
        "source_id": source_id,
        "name": _display_name(release, source_id, attributes, row.get("ref_main_id"), catalog_names),
        "catalog_names": catalog_names,
        "annotation_count": row.get("annotation_count", 0),
        "annotation_paper_count": row.get("annotation_paper_count", 0),
        "position": position,
        "astrometry": astrometry,
        "datasets": list(datasets),
        "working_datasets": list(working_datasets),
        "provenance_status": _provenance_status(datasets, working_datasets),
        "spectrum_status": str(row.get("spectrum_status") or "unknown"),
        "products": [
            {field: product.get(field) for field in _PRODUCT_SUMMARY_FIELDS}
            for product in products
        ],
    }


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _encode_cursor(release: str, source_id: str, fingerprint: str) -> str:
    payload = canonical_json(
        {"v": 1, "release": release, "source_id": source_id, "filters": fingerprint}
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str, fingerprint: str) -> tuple[str, str]:
    text = str(cursor).strip()
    if not text or len(text) > 1024:
        raise InvalidCursor("invalid cursor")
    try:
        padded = text + "=" * (-len(text) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        if not isinstance(value, dict) or value.get("v") != 1:
            raise ValueError
        if value.get("filters") != fingerprint:
            raise InvalidCursor("cursor does not match the current filters")
        return source_identity(value["release"], value["source_id"])
    except InvalidCursor:
        raise
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeError,
        binascii.Error,
        json.JSONDecodeError,
    ) as exc:
        raise InvalidCursor("invalid cursor") from exc


def _validate_limit(limit: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    if value < 1 or value > MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    return value


class LibraryOverview:
    """Global source enumeration and DTO assembly for the local viewer."""

    def __init__(self, store: LibraryStore | None) -> None:
        self.store = store
        self.monitor = RevisionMonitor(store)

    def close(self) -> None:
        self.monitor.close()

    @contextmanager
    def _snapshot(
        self, requested_revision: str | None = None
    ) -> Iterator[tuple[str, sqlite3.Connection | None]]:
        revision, exists = self.monitor.require(requested_revision)
        if not exists or self.store is None:
            yield revision, None
            self.monitor.ensure_unchanged(revision)
            return
        connection = self.store._connect(readonly=True)
        try:
            self.store._reject_newer_schema(self.store._schema_version(connection))
            self.store._validate_owner(connection)
            connection.create_function(
                "source_cache_key", 2, source_cache_key, deterministic=True
            )
            pins: dict[str, str | None] = {}

            def current_run_pin(path: str) -> str | None:
                if path not in pins:
                    pins[path] = _current_run_pin(path)
                return pins[path]

            connection.create_function("current_run_pin", 1, current_run_pin, deterministic=True)
            connection.create_function("search_key", 1, _search_key, deterministic=True)
            connection.create_function("catalog_names_json", 1, _catalog_names_json, deterministic=True)
            connection.execute("BEGIN")
            try:
                yield revision, connection
            finally:
                connection.rollback()
        finally:
            connection.close()
        self.monitor.ensure_unchanged(revision)

    @staticmethod
    def _where(
        filters: SourceFilters,
        *,
        after: tuple[str, str] | None = None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if filters.release is not None:
            clauses.append("sv.gaia_release = ?")
            params.append(filters.release)
        if filters.q is not None:
            pattern = f"%{_escape_like(_search_key(filters.q))}%"
            searchable = [
                "search_key('Gaia ' || sv.gaia_release || ' ' || sv.source_id) LIKE ? ESCAPE '\\'",
                "search_key(sv.ref_main_id) LIKE ? ESCAPE '\\'",
                *(f"search_key(json_extract(sv.attributes_json, '$.{field}')) LIKE ? ESCAPE '\\'"
                  for field in _NAME_FIELDS),
                "EXISTS (SELECT 1 FROM catalog_names cn WHERE cn.release = sv.gaia_release "
                "AND cn.source_id = sv.source_id AND search_key(cn.name) LIKE ? ESCAPE '\\')",
                "EXISTS (SELECT 1 FROM sample_members sm JOIN current_runs cr ON cr.run_id = sm.run_id "
                "WHERE sm.release = sv.gaia_release AND sm.source_id = sv.source_id "
                "AND search_key(cr.label) LIKE ? ESCAPE '\\')",
            ]
            clauses.append("(" + " OR ".join(searchable) + ")")
            params.extend([pattern] * len(searchable))
        if filters.scope == "current_datasets" or filters.corpus_id is not None:
            corpus_clause = ""
            if filters.corpus_id is not None:
                corpus_clause = " AND cr.corpus_id = ?"
                params.append(filters.corpus_id)
            clauses.append(
                "EXISTS (SELECT 1 FROM sample_members AS sm "
                "JOIN current_runs AS cr ON cr.run_id = sm.run_id "
                "WHERE sm.release = sv.gaia_release "
                "AND sm.source_id = sv.source_id"
                + corpus_clause
                + ")"
            )
        if filters.spectrum_state in {
            "ready",
            "pending",
            "failed",
            "not_downloaded",
            "unavailable",
            "unknown",
        }:
            clauses.append("sv.spectrum_status = ?")
            params.append(filters.spectrum_state)
        elif filters.spectrum_state in {"missing", "not_ready"}:
            clauses.append("sv.spectrum_status <> 'ready'")
        if filters.evidence_state == "recorded":
            clauses.append("sv.annotation_count > 0")
        elif filters.evidence_state == "not_assessed":
            clauses.append("sv.annotation_count = 0")
        if after is not None:
            clauses.append(
                "(sv.gaia_release > ? OR "
                "(sv.gaia_release = ? AND sv.source_id > ?))"
            )
            params.extend([after[0], after[0], after[1]])
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    @staticmethod
    def _page_rows(
        connection: sqlite3.Connection,
        filters: SourceFilters,
        *,
        limit: int,
        after: tuple[str, str] | None,
    ) -> tuple[int, int, list[dict[str, Any]], bool]:
        count_where, count_params = LibraryOverview._where(filters)
        counts = connection.execute(
            f"WITH {_source_view_cte(connection)} SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN sv.spectrum_status = 'ready' THEN 1 ELSE 0 END) "
            f"AS registered_xp_ready_sources FROM source_view AS sv{count_where}",
            count_params,
        ).fetchone()
        total = int(counts["total"])
        registered_xp_ready_sources = int(
            counts["registered_xp_ready_sources"] or 0
        )
        page_where, page_params = LibraryOverview._where(filters, after=after)
        rows = connection.execute(
            f"WITH {_source_view_cte(connection)} "
            "SELECT sv.* FROM source_view AS sv"
            f"{page_where} ORDER BY sv.gaia_release, sv.source_id LIMIT ?",
            (*page_params, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        return (
            total,
            registered_xp_ready_sources,
            [dict(row) for row in rows[:limit]],
            has_more,
        )

    @staticmethod
    def _membership_rows(
        connection: sqlite3.Connection,
        keys: Sequence[tuple[str, str]],
        *,
        include_members: bool = False,
    ) -> dict[tuple[str, str], tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
        if not keys:
            return {}
        source_ids = list(dict.fromkeys(source_id for _, source_id in keys))
        placeholders = ",".join("?" for _ in source_ids)
        if include_members:
            member_columns = (
                ", sm.member_ordinal, sm.input_row_id, "
                "sm.attributes_json AS member_attributes_json, sm.match_json"
            )
            grouping = ""
        else:
            member_columns = ", COUNT(*) AS n_member_rows"
            grouping = (
                " GROUP BY sm.release, sm.source_id, r.run_id, r.corpus_id, r.request_id, "
                "r.status, r.started_at, r.finished_at, r.error, r.metadata_json, "
                "c.label, c.canonical_path, c.provenance_status, cr.run_id"
            )
        rows = connection.execute(
            f"WITH {_CURRENT_RUNS_CTE} "
            "SELECT sm.release, sm.source_id, r.run_id, r.corpus_id, r.request_id, "
            "r.status, r.started_at, r.finished_at, r.error, r.metadata_json, "
            "c.label, c.canonical_path, c.provenance_status, "
            "CASE WHEN cr.run_id IS NOT NULL THEN 'current' "
            "WHEN json_extract(r.metadata_json, '$.published') = 1 "
            "THEN 'historical' ELSE 'pending' END AS snapshot_state"
            f"{member_columns} FROM sample_members AS sm "
            "JOIN runs AS r ON r.run_id = sm.run_id "
            "JOIN corpora AS c ON c.corpus_id = r.corpus_id "
            "LEFT JOIN current_runs AS cr ON cr.run_id = r.run_id "
            f"WHERE sm.source_id IN ({placeholders})"
            f"{grouping} ORDER BY sm.release, sm.source_id, c.label, r.started_at, "
            "r.run_id"
            + (", sm.member_ordinal" if include_members else ""),
            source_ids,
        ).fetchall()
        selected = set(keys)
        grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
        for raw in rows:
            row = dict(raw)
            key = (str(row["release"]), str(row["source_id"]))
            if key not in selected:
                continue
            group_key = (*key, str(row["run_id"]))
            dataset = grouped.get(group_key)
            if dataset is None:
                metadata = _object(row.get("metadata_json"))
                dataset = {
                    "corpus_id": str(row["corpus_id"]),
                    "label": str(row["label"]),
                    "provenance_status": str(row["provenance_status"]),
                    "run_id": str(row["run_id"]),
                    "request_id": row.get("request_id"),
                    "status": str(row["status"]),
                    "snapshot_state": str(row["snapshot_state"]),
                    "started_at": row["started_at"],
                    "finished_at": row.get("finished_at"),
                    "has_error": row.get("error") is not None,
                    "metadata": _public_run_metadata(metadata),
                    "n_member_rows": int(row.get("n_member_rows") or 0),
                }
                if include_members:
                    dataset["members"] = []
                grouped[group_key] = dataset
            if include_members:
                dataset["n_member_rows"] += 1
                dataset["members"].append(
                    {
                        "member_ordinal": int(row["member_ordinal"]),
                        "input_row_id": row.get("input_row_id"),
                        "attributes": _without_local_paths(
                            _object(row.get("member_attributes_json"))
                        ),
                        "match": _without_local_paths(
                            _object(row.get("match_json"))
                        ),
                    }
                )
        result: dict[
            tuple[str, str], tuple[list[dict[str, Any]], list[dict[str, Any]]]
        ] = {key: ([], []) for key in keys}
        for (release, source_id, _), dataset in grouped.items():
            current, working = result[(release, source_id)]
            if dataset["snapshot_state"] == "current":
                current.append(dataset)
            else:
                working.append(dataset)
        return result

    @staticmethod
    def _product_rows(
        connection: sqlite3.Connection,
        keys: Sequence[tuple[str, str]],
        *,
        include_artifacts: bool = False,
    ) -> tuple[
        dict[tuple[str, str], list[dict[str, Any]]],
        dict[str, list[dict[str, Any]]],
    ]:
        if not keys:
            return {}, {}
        source_ids = list(dict.fromkeys(source_id for _, source_id in keys))
        placeholders = ",".join("?" for _ in source_ids)
        rows = connection.execute(
            "SELECT pi.item_key, pi.identifier, pi.normalizer_version, pi.state, "
            "pi.active_version_id, pi.updated_at, pi.last_error, pc.contract_json, "
            "pv.created_at AS version_created_at, pv.metadata_json AS version_metadata_json "
            "FROM product_items AS pi "
            "JOIN product_contracts AS pc ON pc.contract_hash = pi.contract_hash "
            "LEFT JOIN product_versions AS pv ON pv.version_id = pi.active_version_id "
            f"WHERE pi.identifier IN ({placeholders}) "
            "ORDER BY pi.identifier, pi.updated_at DESC, pi.item_key",
            source_ids,
        ).fetchall()
        selected = set(keys)
        result: dict[tuple[str, str], list[dict[str, Any]]] = {
            key: [] for key in keys
        }
        active_versions: list[str] = []
        for raw in rows:
            row = dict(raw)
            contract = _object(row.get("contract_json"))
            if str(contract.get("linking_parameter", "")).strip().upper() != "SOURCE_ID":
                continue
            try:
                release = normalize_gaia_release(contract.get("release", ""))
            except ValueError:
                continue
            key = (release, str(row["identifier"]))
            if key not in selected:
                continue
            retrieval_type = str(contract.get("retrieval_type", "")).strip().upper()
            summary = {
                "item_key": str(row["item_key"]),
                "retrieval_type": retrieval_type,
                "state": str(row["state"]),
                "active_version_id": row.get("active_version_id"),
                "updated_at": row["updated_at"],
                "has_error": row.get("last_error") is not None,
                "normalizer_version": str(row["normalizer_version"]),
                "data_structure": contract.get("data_structure"),
                "output_format": contract.get("output_format"),
            }
            if include_artifacts:
                summary["contract"] = _without_local_paths(contract)
                summary["identifier"] = str(row["identifier"])
                summary["version_id"] = row.get("active_version_id")
                summary["created_at"] = row.get("version_created_at")
                summary["metadata"] = _without_local_paths(
                    _object(row.get("version_metadata_json"))
                )
                summary["artifacts"] = []
                if row.get("active_version_id") is not None:
                    active_versions.append(str(row["active_version_id"]))
            result[key].append(summary)
        artifacts: dict[str, list[dict[str, Any]]] = {}
        if include_artifacts and active_versions:
            unique_versions = list(dict.fromkeys(active_versions))
            placeholders = ",".join("?" for _ in unique_versions)
            artifact_rows = connection.execute(
                "SELECT pa.version_id, pa.role, pa.slot, pa.metadata_json, "
                "b.sha256, b.relative_path, b.size_bytes, b.content_type, b.extension "
                "FROM product_artifacts AS pa "
                "JOIN blobs AS b ON b.sha256 = pa.blob_sha256 "
                f"WHERE pa.version_id IN ({placeholders}) "
                "ORDER BY pa.version_id, pa.role, pa.slot",
                unique_versions,
            ).fetchall()
            for raw in artifact_rows:
                row = dict(raw)
                artifacts.setdefault(str(row["version_id"]), []).append(
                    {
                        "role": str(row["role"]),
                        "slot": str(row["slot"]),
                        "blob": {
                            "sha256": str(row["sha256"]),
                            "size_bytes": int(row["size_bytes"]),
                            "content_type": str(row["content_type"]),
                            "extension": str(row["extension"]),
                        },
                        "metadata": _without_local_paths(
                            _object(row.get("metadata_json"))
                        ),
                    }
                )
            for products in result.values():
                for product in products:
                    version_id = product.get("active_version_id")
                    if version_id is not None:
                        product["artifacts"] = artifacts.get(str(version_id), [])
        return result, artifacts

    @staticmethod
    def _availability(row: Mapping[str, Any], now: float) -> dict[str, Any] | None:
        if row.get("availability_id") is None:
            return None
        expires_at = float(row["availability_expires_at"])
        return {
            "id": str(row["availability_id"]),
            "payload": _loads(row.get("availability_payload_json"), None),
            "outcome": str(row["availability_outcome"]),
            "observed_at": row["availability_observed_at"],
            "expires_at": expires_at,
            "stale": expires_at <= now,
            "has_error": row.get("availability_error") is not None,
        }

    @staticmethod
    def _bibliography(
        connection: sqlite3.Connection, row: Mapping[str, Any], now: float
    ) -> dict[str, Any] | None:
        snapshot_id = row.get("ref_snapshot_id")
        if snapshot_id is None:
            return None
        paper_rows = connection.execute(
            "SELECT p.metadata_json FROM source_papers AS sp "
            "JOIN papers AS p ON p.bibcode = sp.bibcode "
            "WHERE sp.snapshot_id = ? ORDER BY p.bibcode",
            (snapshot_id,),
        ).fetchall()
        expires_at = float(row["ref_expires_at"])
        return {
            "snapshot_id": str(snapshot_id),
            "release": str(row["gaia_release"]),
            "source_id": str(row["source_id"]),
            "papers": [
                _without_local_paths(_object(item["metadata_json"]))
                for item in paper_rows
            ],
            "main_id": str(row.get("ref_main_id") or ""),
            "outcome": str(row["ref_outcome"]),
            "observed_at": row["ref_observed_at"],
            "expires_at": expires_at,
            "stale": expires_at <= now,
        }

    def meta(self) -> dict[str, Any]:
        with self._snapshot() as (revision, connection):
            if connection is None:
                return {
                    "revision": revision,
                    "exists": False,
                    "total_sources": 0,
                    "total_positions": 0,
                    "registered_xp_ready_sources": 0,
                    "releases": [],
                    "datasets": [],
                    "runs": [],
                }
            aggregate = connection.execute(
                f"WITH {_source_view_cte(connection)} SELECT COUNT(*) AS total_sources, "
                f"SUM(CASE WHEN {_VALID_POSITION_SQL} THEN 1 ELSE 0 END) AS total_positions "
                "FROM source_view AS sv"
            ).fetchone()
            releases = [
                {
                    "release": str(row["gaia_release"]),
                    "total_sources": int(row["total_sources"]),
                    "total_positions": int(row["total_positions"] or 0),
                }
                for row in connection.execute(
                    f"WITH {_source_view_cte(connection)} SELECT sv.gaia_release, "
                    "COUNT(*) AS total_sources, "
                    f"SUM(CASE WHEN {_VALID_POSITION_SQL} THEN 1 ELSE 0 END) AS total_positions "
                    "FROM source_view AS sv GROUP BY sv.gaia_release ORDER BY sv.gaia_release"
                ).fetchall()
            ]
            ready = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM (SELECT {_PRODUCT_RELEASE_SQL} AS release, "
                    "pi.identifier FROM product_items AS pi "
                    "JOIN product_contracts AS pc ON pc.contract_hash = pi.contract_hash "
                    "JOIN sources AS s ON s.gaia_release = "
                    f"({_PRODUCT_RELEASE_SQL}) AND s.source_id = pi.identifier "
                    "WHERE pi.state = 'ready' AND pi.active_version_id IS NOT NULL "
                    "AND upper(COALESCE(json_extract("
                    "pc.contract_json, '$.linking_parameter'), '')) = 'SOURCE_ID' "
                    "AND upper(COALESCE(json_extract(pc.contract_json, '$.retrieval_type'), '')) "
                    "IN ('XP_SAMPLED', 'XP_CONTINUOUS') "
                    "GROUP BY release, pi.identifier)"
                ).fetchone()[0]
            )
            corpus_rows = connection.execute(
                f"WITH {_CURRENT_RUNS_CTE} "
                "SELECT c.corpus_id, c.canonical_path, c.label, c.provenance_status, "
                "c.created_at AS corpus_created_at, c.updated_at AS corpus_updated_at, "
                "r.run_id, r.request_id, r.status, r.started_at, r.finished_at, "
                "r.options_json, r.error, r.metadata_json, cr.run_id AS current_run_id "
                "FROM corpora AS c LEFT JOIN runs AS r ON r.corpus_id = c.corpus_id "
                "LEFT JOIN current_runs AS cr ON cr.corpus_id = c.corpus_id "
                "ORDER BY lower(c.label), c.corpus_id, r.started_at, r.run_id"
            ).fetchall()
            by_corpus: dict[str, dict[str, Any]] = {}
            run_values: list[dict[str, Any]] = []
            for raw in corpus_rows:
                row = dict(raw)
                corpus_id = str(row["corpus_id"])
                corpus = by_corpus.setdefault(
                    corpus_id,
                    {
                        "corpus_id": corpus_id,
                        "label": str(row["label"]),
                        "provenance_status": str(row["provenance_status"]),
                        "created_at": row["corpus_created_at"],
                        "updated_at": row["corpus_updated_at"],
                        "current_run_id": row.get("current_run_id"),
                    },
                )
                if row.get("run_id") is None:
                    continue
                metadata = _object(row.get("metadata_json"))
                run = {
                    "run_id": str(row["run_id"]),
                    "corpus_id": corpus_id,
                    "label": str(row["label"]),
                    "request_id": row.get("request_id"),
                    "status": str(row["status"]),
                    "started_at": row["started_at"],
                    "finished_at": row.get("finished_at"),
                    "has_error": row.get("error") is not None,
                    "metadata": _public_run_metadata(metadata),
                    "snapshot_state": "pending",
                }
                run_values.append(run)
            for run in run_values:
                current_id = by_corpus[run["corpus_id"]]["current_run_id"]
                if run["run_id"] == current_id:
                    run["snapshot_state"] = "current"
                elif (
                    run["metadata"].get("members_indexed") is True
                    and run["metadata"].get("published") is True
                ):
                    run["snapshot_state"] = "historical"
                elif run["metadata"].get("members_indexed") is not True:
                    run["snapshot_state"] = "non_dataset"
            datasets = [
                corpus
                for corpus in by_corpus.values()
                if corpus["current_run_id"] is not None
            ]
            return {
                "revision": revision,
                "exists": True,
                "total_sources": int(aggregate["total_sources"]),
                "total_positions": int(aggregate["total_positions"] or 0),
                "registered_xp_ready_sources": ready,
                "releases": releases,
                "datasets": datasets,
                "runs": run_values,
            }

    def sources(
        self,
        *,
        filters: SourceFilters | None = None,
        limit: int = DEFAULT_LIMIT,
        cursor: str | None = None,
        revision: str | None = None,
    ) -> dict[str, Any]:
        filters = filters or SourceFilters()
        limit = _validate_limit(limit)
        if cursor is not None and revision is None:
            raise ValueError("revision is required when cursor is provided")
        after = (
            None
            if cursor is None
            else _decode_cursor(cursor, filters.fingerprint)
        )
        with self._snapshot(revision) as (actual_revision, connection):
            if connection is None:
                return {
                    "revision": actual_revision,
                    "total": 0,
                    "registered_xp_ready_sources": 0,
                    "returned": 0,
                    "sources": [],
                    "next_cursor": None,
                    "has_more": False,
                }
            total, registered_xp_ready_sources, rows, has_more = self._page_rows(
                connection, filters, limit=limit, after=after
            )
            keys = [
                (str(row["gaia_release"]), str(row["source_id"])) for row in rows
            ]
            memberships = self._membership_rows(connection, keys)
            products, _ = self._product_rows(connection, keys)
            sources: list[dict[str, Any]] = []
            for row, key in zip(rows, keys):
                datasets, working = memberships.get(key, ([], []))
                sources.append(
                    _source_dto(row, datasets, working, products.get(key, []))
                )
            next_cursor = None
            if has_more and rows:
                last = rows[-1]
                next_cursor = _encode_cursor(
                    str(last["gaia_release"]),
                    str(last["source_id"]),
                    filters.fingerprint,
                )
            return {
                "revision": actual_revision,
                "total": total,
                "registered_xp_ready_sources": registered_xp_ready_sources,
                "returned": len(sources),
                "sources": sources,
                "next_cursor": next_cursor,
                "has_more": has_more,
            }

    def sky(
        self,
        *,
        filters: SourceFilters | None = None,
        limit: int = DEFAULT_LIMIT,
        cursor: str | None = None,
        revision: str | None = None,
    ) -> dict[str, Any]:
        filters = filters or SourceFilters()
        limit = _validate_limit(limit)
        if cursor is not None and revision is None:
            raise ValueError("revision is required when cursor is provided")
        after = (
            None
            if cursor is None
            else _decode_cursor(cursor, filters.fingerprint)
        )
        with self._snapshot(revision) as (actual_revision, connection):
            if connection is None:
                return {
                    "revision": actual_revision,
                    "total_sources": 0,
                    "total_positions": 0,
                    "missing_positions": 0,
                    "registered_xp_ready_sources": 0,
                    "returned": 0,
                    "scanned_count": 0,
                    "points": [],
                    "next_cursor": None,
                    "has_more": False,
                }
            count_where, count_params = self._where(filters)
            counts = connection.execute(
                f"WITH {_source_view_cte(connection)} SELECT COUNT(*) AS total_sources, "
                f"SUM(CASE WHEN {_VALID_POSITION_SQL} THEN 1 ELSE 0 END) AS total_positions, "
                "SUM(CASE WHEN sv.spectrum_status = 'ready' THEN 1 ELSE 0 END) "
                f"AS registered_xp_ready_sources FROM source_view AS sv{count_where}",
                count_params,
            ).fetchone()
            page_where, page_params = self._where(filters, after=after)
            rows = connection.execute(
                f"WITH {_source_view_cte(connection)} SELECT sv.* FROM source_view AS sv"
                f"{page_where} ORDER BY sv.gaia_release, sv.source_id LIMIT ?",
                (*page_params, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            page = [dict(row) for row in rows[:limit]]
            points: list[dict[str, Any]] = []
            for row in page:
                release = str(row["gaia_release"])
                source_id = str(row["source_id"])
                attributes = _object(row.get("attributes_json"))
                position, _ = _position_and_astrometry(
                    release,
                    attributes,
                    _list(row.get("covered_fields_json")),
                    _object(row.get("field_provenance_json")),
                )
                if not position["valid"]:
                    continue
                points.append(
                    {
                        "key": f"{release}:{source_id}",
                        "release": release,
                        "source_id": source_id,
                        "name": _display_name(
                            release, source_id, attributes, row.get("ref_main_id"),
                            _list(row.get("catalog_names_json")),
                        ),
                        "ra_deg": position["ra_deg"],
                        "dec_deg": position["dec_deg"],
                        "spectrum_status": str(
                            row.get("spectrum_status") or "unknown"
                        ),
                    }
                )
            next_cursor = None
            if has_more and page:
                last = page[-1]
                next_cursor = _encode_cursor(
                    str(last["gaia_release"]),
                    str(last["source_id"]),
                    filters.fingerprint,
                )
            total_sources = int(counts["total_sources"])
            total_positions = int(counts["total_positions"] or 0)
            return {
                "revision": actual_revision,
                "total_sources": total_sources,
                "total_positions": total_positions,
                "missing_positions": total_sources - total_positions,
                "registered_xp_ready_sources": int(
                    counts["registered_xp_ready_sources"] or 0
                ),
                "returned": len(points),
                "scanned_count": len(page),
                "points": points,
                "next_cursor": next_cursor,
                "has_more": has_more,
            }

    def source(
        self,
        release: str,
        source_id: Any,
        *,
        revision: str | None = None,
    ) -> dict[str, Any]:
        release, source_id = source_identity(release, source_id)
        query = {"release": release, "source_id": source_id}
        with self._snapshot(revision) as (actual_revision, connection):
            if connection is None:
                return {
                    "revision": actual_revision,
                    "exists": False,
                    "scope": "global_library",
                    "resolved": False,
                    "query": query,
                }
            row = connection.execute(
                f"WITH {_source_view_cte(connection)} SELECT sv.* FROM source_view AS sv "
                "WHERE sv.gaia_release = ? AND sv.source_id = ?",
                (release, source_id),
            ).fetchone()
            if row is None:
                availability_row = connection.execute(
                    "SELECT obs.id AS availability_id, "
                    "obs.outcome AS availability_outcome, "
                    "obs.payload_json AS availability_payload_json, "
                    "obs.observed_at AS availability_observed_at, "
                    "obs.expires_at AS availability_expires_at, "
                    "obs.error AS availability_error "
                    "FROM cache_current AS current "
                    "JOIN cache_observations AS obs "
                    "ON obs.id = current.observation_id "
                    "WHERE current.namespace = ? AND current.cache_key = ?",
                    (
                        XP_AVAILABILITY_NAMESPACE,
                        source_cache_key(release, source_id),
                    ),
                ).fetchone()
                now = float(self.store.clock()) if self.store is not None else 0.0
                return {
                    "revision": actual_revision,
                    "exists": True,
                    "scope": "global_library",
                    "resolved": False,
                    "query": query,
                    "reason": "source_not_found",
                    "datasets": [],
                    "working_datasets": [],
                    "active_bibliography": None,
                    "xp_availability": (
                        None
                        if availability_row is None
                        else self._availability(dict(availability_row), now)
                    ),
                    "active_products": [],
                    "dto": None,
                }
            value = dict(row)
            key = (release, source_id)
            memberships = self._membership_rows(
                connection, [key], include_members=True
            )
            datasets, working = memberships.get(key, ([], []))
            products, _ = self._product_rows(
                connection, [key], include_artifacts=True
            )
            product_values = products.get(key, [])
            active_products = [
                {**product, "integrity": "unchecked"}
                for product in product_values
                if product.get("state") == "ready"
                and product.get("active_version_id") is not None
            ]
            now = float(self.store.clock()) if self.store is not None else 0.0
            raw_source = {
                "gaia_release": release,
                "source_id": source_id,
                "attributes": _without_local_paths(
                    _object(value.get("attributes_json"))
                ),
                "covered_fields": _list(value.get("covered_fields_json")),
                "field_provenance": _object(value.get("field_provenance_json")),
                "updated_at": value["updated_at"],
            }
            return {
                "revision": actual_revision,
                "exists": True,
                "scope": "global_library",
                "resolved": True,
                "query": query,
                "source": raw_source,
                "datasets": datasets,
                "working_datasets": working,
                "active_bibliography": self._bibliography(
                    connection, value, now
                ),
                "xp_availability": self._availability(value, now),
                "active_products": active_products,
                "dto": _source_dto(
                    value, datasets, working, product_values
                ),
            }


__all__ = [
    "DEFAULT_LIMIT",
    "InvalidCursor",
    "LibraryOverview",
    "MAX_LIMIT",
    "RevisionMonitor",
    "SCOPES",
    "SPECTRUM_STATES",
    "SnapshotChanged",
    "SourceFilters",
]
