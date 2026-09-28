"""Release-aware Gaia DataLink request, artifact, and download primitives."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, TYPE_CHECKING

from astropy.table import Table

from astro_pipeline.gaia_releases import get_gaia_release_profile
from astro_pipeline.io.writer import DownloadCheckpoint

if TYPE_CHECKING:
    from astro_pipeline.library.store import LibraryStore

ProgressCallback = Callable[[dict[str, Any]], None]
DataLinkLoader = Callable[..., dict[str, list[Any]]]
PRODUCT_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
VALID_LINKING_PARAMETERS = frozenset({"SOURCE_ID", "TRANSIT_ID", "IMAGE_ID"})


@dataclass(frozen=True)
class GaiaProductRequest:
    """A validated, release-qualified Gaia DataLink request."""

    release: str
    retrieval_type: str
    identifiers: tuple[str, ...] | list[str] | str | int
    data_structure: str = "INDIVIDUAL"
    output_format: str = "fits"
    linking_parameter: str = "SOURCE_ID"
    valid_data: bool = False
    batch_size: int = 5

    def __post_init__(self) -> None:
        profile = get_gaia_release_profile(self.release)
        product = str(self.retrieval_type).strip().upper()
        structure = str(self.data_structure).strip().upper()
        output_format = str(self.output_format).strip().lower()
        linking = str(self.linking_parameter).strip().upper()
        raw_identifiers = (
            (self.identifiers,)
            if isinstance(self.identifiers, (str, int))
            else self.identifiers
        )
        identifiers = tuple(
            dict.fromkeys(
                str(value).strip()
                for value in raw_identifiers
                if str(value).strip()
            )
        )

        if not PRODUCT_NAME.fullmatch(product):
            raise ValueError(f"invalid Gaia retrieval_type: {self.retrieval_type!r}")
        if product not in profile.datalink_products:
            raise ValueError(
                f"{product!r} is not declared by the {profile.display_name} profile; "
                "refresh the release profile after Archive metadata are published"
            )
        if structure not in profile.datalink_structures:
            raise ValueError(
                f"data_structure must be one of {profile.datalink_structures}; got {structure!r}"
            )
        if output_format not in profile.datalink_formats:
            raise ValueError(
                f"output_format must be one of {profile.datalink_formats}; got {output_format!r}"
            )
        if linking not in VALID_LINKING_PARAMETERS:
            raise ValueError(
                f"linking_parameter must be one of {sorted(VALID_LINKING_PARAMETERS)}; "
                f"got {linking!r}"
            )
        if not identifiers:
            raise ValueError("at least one Gaia DataLink identifier is required")
        if any(not identifier.isascii() or not identifier.isdigit() for identifier in identifiers):
            raise ValueError(
                f"{linking} identifiers must contain digits only; ranges and job references "
                "are not accepted by this bounded downloader"
            )
        identifiers = tuple(
            dict.fromkeys(identifier.lstrip("0") or "0" for identifier in identifiers)
        )
        if self.batch_size < 1 or self.batch_size > 5000:
            raise ValueError("batch_size must be between 1 and 5000")

        object.__setattr__(self, "release", profile.key)
        object.__setattr__(self, "retrieval_type", product)
        object.__setattr__(self, "identifiers", identifiers)
        object.__setattr__(self, "data_structure", structure)
        object.__setattr__(self, "output_format", output_format)
        object.__setattr__(self, "linking_parameter", linking)

    @property
    def archive_tag(self) -> str:
        return get_gaia_release_profile(self.release).archive_tag

    def immutable_contract(self) -> dict[str, Any]:
        return {
            "release": self.release,
            "archive_tag": self.archive_tag,
            "retrieval_type": self.retrieval_type,
            "data_structure": self.data_structure,
            "output_format": self.output_format,
            "linking_parameter": self.linking_parameter,
            "valid_data": self.valid_data,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "archive_tag": self.archive_tag}


@dataclass
class GaiaArtifact:
    """One locally materialized identifier/product result."""

    release: str
    archive_tag: str
    retrieval_type: str
    linking_parameter: str
    identifier: str
    output_format: str
    data_structure: str
    normalized_path: str = ""
    archive_table_path: str = ""
    checksum_sha256: str = ""
    archive_checksum_sha256: str = ""
    content_type: str = ""
    table_key: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    success: bool = True
    error: Optional[str] = None
    item_key: str = ""
    version_id: str | int | None = None
    version_created_at: Any = None
    attempt_id: str | int | None = None
    actual_batch_id: str = ""
    cache_status: str = ""
    normalized_blob: dict[str, Any] = field(default_factory=dict)
    archive_blob: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _SharedCandidate:
    identifier: str
    item_key: str
    previous_version: dict[str, Any] | None = None
    repair: bool = False


def plan_gaia_product_request(
    *,
    release: str,
    retrieval_type: str,
    n_identifiers: int,
    data_structure: str = "INDIVIDUAL",
    output_format: str = "fits",
    linking_parameter: str = "SOURCE_ID",
    valid_data: bool = False,
    batch_size: int = 5,
) -> dict[str, Any]:
    """Validate a product contract without downloading or requiring real IDs."""
    if n_identifiers < 1:
        raise ValueError("n_identifiers must be positive")
    request = GaiaProductRequest(
        release=release,
        retrieval_type=retrieval_type,
        identifiers=("0",),
        data_structure=data_structure,
        output_format=output_format,
        linking_parameter=linking_parameter,
        valid_data=valid_data,
        batch_size=batch_size,
    )
    profile = get_gaia_release_profile(request.release)
    return {
        "writes_data": False,
        "request": request.immutable_contract(),
        "n_identifiers": n_identifiers,
        "n_batches": (n_identifiers + batch_size - 1) // batch_size,
        "batch_size": batch_size,
        "profile_status": profile.status,
        "output_layout": {
            "archive_tables": "<output_dir>/archive_tables/batch-*.{format}",
            "normalized": "<output_dir>/<identifier>.{format}",
            "checkpoint": "<output_dir>/checkpoint.json",
            "provenance": "<output_dir>/product_request.json and artifacts.json",
        },
        "warnings": [
            "A declared product may not yet be published by the live Archive",
            "CROWDED_FIELD_IMAGE binary image payloads require the future blob transport",
        ]
        if profile.status == "provisional"
        else [],
    }


def download_gaia_product_request(
    request: GaiaProductRequest,
    output_dir: Path | str,
    *,
    resume: bool = True,
    loader: Optional[DataLinkLoader] = None,
    progress_callback: Optional[ProgressCallback] = None,
    library: LibraryStore | None = None,
    run_id: str | None = None,
    refresh: bool = False,
    lease_seconds: float | None = None,
    wait_timeout_seconds: float | None = None,
) -> list[GaiaArtifact]:
    """Download bounded DataLink batches and persist release-aware provenance.

    Astroquery returns parsed tables rather than the original HTTP byte stream.
    The ``archive_tables`` copies therefore preserve Archive table semantics and
    metadata but are explicitly marked as re-serialized, not byte-identical raw
    responses. Binary image payload support remains disabled until DR4 is live.
    """
    if library is not None:
        return _download_gaia_product_request_shared(
            request,
            output_dir,
            library=library,
            run_id=run_id,
            refresh=refresh,
            loader=loader,
            progress_callback=progress_callback,
            lease_seconds=lease_seconds,
            wait_timeout_seconds=wait_timeout_seconds,
        )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_dir = output_dir / "archive_tables"
    archive_dir.mkdir(parents=True, exist_ok=True)
    _open_or_create_request_contract(output_dir / "product_request.json", request)

    checkpoint = DownloadCheckpoint(
        output_dir / "checkpoint.json",
        source_name=f"{request.archive_tag}:{request.retrieval_type}",
    )
    identifiers = list(request.identifiers)
    if resume:
        identifiers = [value for value in identifiers if not checkpoint.is_completed(value)]
        extension = _extension(request.output_format)
        identifiers = [
            value for value in identifiers if not (output_dir / f"{value}.{extension}").exists()
        ]
    if not identifiers:
        if progress_callback:
            progress_callback(
                {
                    "retrieval_type": request.retrieval_type,
                    "pending_at_start": 0,
                    "processed": 0,
                    "completed_this_run": 0,
                    "failed_this_run": 0,
                    "done": True,
                }
            )
        return []

    load_data = loader or _default_data_link_loader
    artifacts: list[GaiaArtifact] = []
    total_pending = len(identifiers)
    for start in range(0, total_pending, request.batch_size):
        batch = identifiers[start : start + request.batch_size]
        try:
            response = load_data(
                ids=batch,
                data_release=request.archive_tag,
                data_structure=request.data_structure,
                retrieval_type=request.retrieval_type,
                linking_parameter=request.linking_parameter,
                valid_data=request.valid_data,
                format=request.output_format,
            )
            batch_artifacts = _materialize_tables(
                response or {},
                request=request,
                batch=batch,
                output_dir=output_dir,
                archive_dir=archive_dir,
                batch_index=start // request.batch_size,
            )
        except Exception as exc:
            batch_artifacts = [
                _failed_artifact(request, identifier, str(exc)) for identifier in batch
            ]

        returned = {
            artifact.identifier
            for artifact in batch_artifacts
            if artifact.success and artifact.identifier
        }
        for identifier in batch:
            if identifier not in returned and not any(
                artifact.identifier == identifier and not artifact.success
                for artifact in batch_artifacts
            ):
                batch_artifacts.append(
                    _failed_artifact(
                        request,
                        identifier,
                        f"Gaia DataLink returned no {request.retrieval_type} product",
                    )
                )
        for artifact in batch_artifacts:
            if artifact.success:
                checkpoint.mark_completed(artifact.identifier)
            else:
                checkpoint.mark_failed(artifact.identifier)
        checkpoint.save()
        artifacts.extend(batch_artifacts)
        _merge_artifact_manifest(output_dir / "artifacts.json", batch_artifacts)

        if progress_callback:
            processed = min(start + len(batch), total_pending)
            progress_callback(
                {
                    "release": request.release,
                    "retrieval_type": request.retrieval_type,
                    "pending_at_start": total_pending,
                    "processed": processed,
                    "completed_this_run": sum(item.success for item in artifacts),
                    "failed_this_run": sum(not item.success for item in artifacts),
                    "done": processed >= total_pending,
                }
            )
    checkpoint.save()
    return artifacts


def _default_data_link_loader(**kwargs):
    """Import astroquery only when an actual Archive request is required."""
    from astroquery.gaia import Gaia

    return Gaia.load_data(**kwargs)


def _download_gaia_product_request_shared(
    request: GaiaProductRequest,
    output_dir: Path | str,
    *,
    library: LibraryStore,
    run_id: str | None,
    refresh: bool,
    loader: Optional[DataLinkLoader],
    progress_callback: Optional[ProgressCallback],
    lease_seconds: float | None,
    wait_timeout_seconds: float | None,
) -> list[GaiaArtifact]:
    """Resolve a request through the fenced, content-addressed product library."""
    from astro_pipeline.library.products import product_view_lease_key

    output_dir = Path(output_dir)
    contract = request.immutable_contract()
    _validate_existing_shared_view(output_dir, contract, library)
    library.initialize()
    lease_ttl = float(
        library.lease_seconds if lease_seconds is None else lease_seconds
    )
    wait_timeout = float(
        library.wait_timeout_seconds
        if wait_timeout_seconds is None
        else wait_timeout_seconds
    )
    if lease_ttl <= 0:
        raise ValueError("lease_seconds must be positive")
    if wait_timeout < 0:
        raise ValueError("wait_timeout_seconds cannot be negative")

    with library.hold_lease(
        "product-view",
        product_view_lease_key(output_dir),
        ttl=lease_ttl,
        wait_timeout=wait_timeout,
    ) as view_lease:
        # Recheck after acquiring ownership: another writer may have created the
        # view between the read-only preflight and this lease.
        _validate_existing_shared_view(output_dir, contract, library)
        output_dir.mkdir(parents=True, exist_ok=True)
        _open_or_create_request_contract(
            output_dir / "product_request.json", request
        )
        return _download_shared_under_view_lease(
            request,
            output_dir,
            library=library,
            run_id=run_id,
            refresh=refresh,
            loader=loader or _default_data_link_loader,
            progress_callback=progress_callback,
            lease_seconds=lease_ttl,
            wait_timeout_seconds=wait_timeout,
            view_lease=view_lease,
        )


def _download_shared_under_view_lease(
    request: GaiaProductRequest,
    output_dir: Path,
    *,
    library: LibraryStore,
    run_id: str | None,
    refresh: bool,
    loader: DataLinkLoader,
    progress_callback: Optional[ProgressCallback],
    lease_seconds: float,
    wait_timeout_seconds: float,
    view_lease: Any,
) -> list[GaiaArtifact]:
    from astro_pipeline.library.files import BlobIntegrityError
    from astro_pipeline.library.leases import LeaseBatch
    from astro_pipeline.library.products import (
        ProductViewItem,
        ready_view_item,
        semantic_item_key,
    )
    from astro_pipeline.library.store import LibraryBusyError

    identifiers = list(request.identifiers)
    views: dict[str, ProductViewItem] = {}
    artifacts_by_identifier: dict[str, list[GaiaArtifact]] = {}
    misses: list[_SharedCandidate] = []

    for identifier in identifiers:
        item_key = semantic_item_key(request.immutable_contract(), identifier)
        version: dict[str, Any] | None = None
        unverified: dict[str, Any] | None = None
        repair = False
        try:
            version = library.get_product(item_key, verify=True)
        except BlobIntegrityError:
            repair = True
            unverified = library.get_product(item_key, verify=False)
        previous = version or unverified
        expired_negative = False
        if not refresh and not repair:
            cached_absence = library.get_cache(
                "product", item_key, include_stale=True
            )
            if cached_absence and cached_absence.get("outcome") == "negative":
                if cached_absence.get("stale"):
                    expired_negative = True
                else:
                    view = ProductViewItem(
                        identifier=identifier,
                        item_key=item_key,
                        status="absent",
                        cache_status="negative_hit",
                        error=str(
                            (cached_absence.get("payload") or {}).get(
                                "error",
                                f"Gaia DataLink returned no {request.retrieval_type} product",
                            )
                        ),
                        previous_ready_version_id=(previous or {}).get("version_id"),
                        observed_at=cached_absence.get("observed_at"),
                        expires_at=cached_absence.get("expires_at"),
                    )
                    views[identifier] = view
                    artifacts_by_identifier[identifier] = [
                        _failed_artifact(
                            request,
                            identifier,
                            view.error or "product absent",
                            item_key=item_key,
                            cache_status="negative_hit",
                        )
                    ]
                    continue
        if version is not None and not refresh and not expired_negative:
            view = ready_view_item(
                library,
                version,
                cache_status="hit",
                attempt_id=(version.get("metadata") or {}).get("attempt_id"),
                actual_batch_id=str(
                    (version.get("metadata") or {}).get("actual_batch_id", "")
                ),
            )
            view.identifier = identifier
            views[identifier] = view
            artifacts_by_identifier[identifier] = _gaia_artifacts_from_ready_view(
                request, view
            )
            continue
        misses.append(
            _SharedCandidate(
                identifier=identifier,
                item_key=item_key,
                previous_version=previous,
                repair=repair,
            )
        )

    owned: list[tuple[_SharedCandidate, Any]] = []
    busy: list[_SharedCandidate] = []
    # Claim every immediately available miss in one transaction and renew the
    # full claimed set with one worker.  Process owned claims before boundedly
    # waiting for keys held by another process.
    candidates_by_key = {candidate.item_key: candidate for candidate in misses}
    with LeaseBatch(
        library,
        "product",
        candidates_by_key,
        ttl=lease_seconds,
    ) as lease_batch:
        owned = [
            (candidates_by_key[key], lease)
            for key, lease in lease_batch.leases.items()
        ]
        busy = [candidates_by_key[key] for key in lease_batch.busy]
        for start in range(0, len(owned), request.batch_size):
            _process_shared_batch(
                request,
                owned[start : start + request.batch_size],
                library=library,
                run_id=run_id,
                refresh=refresh,
                loader=loader,
                views=views,
                artifacts_by_identifier=artifacts_by_identifier,
            )
            _emit_shared_progress(
                request,
                identifiers,
                views,
                progress_callback,
                done=False,
            )

    # Owned leases are released before waiting, preventing workers that claimed
    # disjoint subsets from waiting on each other while still holding claims.
    for candidate in busy:
        try:
            with library.hold_lease(
                "product",
                candidate.item_key,
                ttl=lease_seconds,
                wait_timeout=wait_timeout_seconds,
            ) as lease:
                peer_version = None
                try:
                    peer_version = library.get_product(
                        candidate.item_key, verify=True
                    )
                except BlobIntegrityError:
                    candidate.repair = True
                previous_id = (
                    candidate.previous_version or {}
                ).get("version_id")
                expired_negative = False
                if not refresh and not candidate.repair:
                    cached_absence = library.get_cache(
                        "product", candidate.item_key, include_stale=True
                    )
                    if (
                        cached_absence
                        and cached_absence.get("outcome") == "negative"
                    ):
                        if cached_absence.get("stale"):
                            expired_negative = True
                        else:
                            view = ProductViewItem(
                                identifier=candidate.identifier,
                                item_key=candidate.item_key,
                                status="absent",
                                cache_status="negative_peer_hit",
                                error=str(
                                    (cached_absence.get("payload") or {}).get(
                                        "error", "product absent"
                                    )
                                ),
                                previous_ready_version_id=(
                                    candidate.previous_version or {}
                                ).get("version_id"),
                                observed_at=cached_absence.get("observed_at"),
                                expires_at=cached_absence.get("expires_at"),
                            )
                            views[candidate.identifier] = view
                            artifacts_by_identifier[candidate.identifier] = [
                                _failed_artifact(
                                    request,
                                    candidate.identifier,
                                    view.error or "product absent",
                                    item_key=candidate.item_key,
                                    cache_status="negative_peer_hit",
                                )
                            ]
                            continue
                peer_is_new = peer_version is not None and not expired_negative and (
                    not refresh or peer_version.get("version_id") != previous_id
                )
                if peer_is_new:
                    view = ready_view_item(
                        library,
                        peer_version,
                        cache_status="peer_hit" if not refresh else "peer_refresh",
                        attempt_id=(peer_version.get("metadata") or {}).get(
                            "attempt_id"
                        ),
                        actual_batch_id=str(
                            (peer_version.get("metadata") or {}).get(
                                "actual_batch_id", ""
                            )
                        ),
                    )
                    view.identifier = candidate.identifier
                    views[candidate.identifier] = view
                    artifacts_by_identifier[candidate.identifier] = (
                        _gaia_artifacts_from_ready_view(request, view)
                    )
                    continue
                _process_shared_batch(
                    request,
                    [(candidate, lease)],
                    library=library,
                    run_id=run_id,
                    refresh=refresh,
                    loader=loader,
                    views=views,
                    artifacts_by_identifier=artifacts_by_identifier,
                )
        except LibraryBusyError as exc:
            error = f"shared product remained busy after bounded wait: {exc}"
            view = ProductViewItem(
                identifier=candidate.identifier,
                item_key=candidate.item_key,
                status="failed",
                cache_status="busy",
                error=error,
                previous_ready_version_id=(
                    candidate.previous_version or {}
                ).get("version_id"),
            )
            views[candidate.identifier] = view
            artifacts_by_identifier[candidate.identifier] = [
                _failed_artifact(
                    request,
                    candidate.identifier,
                    error,
                    item_key=candidate.item_key,
                    cache_status="busy",
                )
            ]

    updates = [views[value] for value in identifiers if value in views]
    current_artifacts = [
        artifact.to_dict()
        for identifier in identifiers
        for artifact in artifacts_by_identifier.get(identifier, [])
        if identifier in views
    ]
    _publish_shared_view_metadata(
        library,
        view_lease,
        output_dir=output_dir,
        contract=request.immutable_contract(),
        source_name=f"{request.archive_tag}:{request.retrieval_type}",
        updates=updates,
        requested_identifiers=identifiers,
        artifacts=current_artifacts,
    )
    _emit_shared_progress(
        request,
        identifiers,
        views,
        progress_callback,
        done=True,
    )
    return [
        artifact
        for identifier in identifiers
        for artifact in artifacts_by_identifier.get(identifier, [])
    ]


def _publish_shared_view_metadata(
    library: LibraryStore,
    view_lease: Any,
    *,
    output_dir: Path,
    contract: dict[str, Any],
    source_name: str,
    updates: list[Any],
    requested_identifiers: list[str],
    artifacts: list[dict[str, Any]],
) -> None:
    """Fence all label-current replacements in one short write transaction."""
    from astro_pipeline.library.products import (
        merge_artifact_view,
        merge_checkpoint_view,
        merge_product_refs,
    )

    # BEGIN IMMEDIATE prevents a successor from acquiring the expired view lease
    # between this assertion and the atomic file replacements.  The transaction
    # contains only local metadata reads/writes; downloads and blob publication
    # have already completed.
    with library.transaction() as connection:
        library.assert_lease(connection, view_lease)
        merge_product_refs(
            output_dir / "product_refs.json",
            store=library,
            contract=contract,
            updates=updates,
        )
        merge_checkpoint_view(
            output_dir / "checkpoint.json",
            source_name=source_name,
            updates=updates,
        )
        merge_artifact_view(
            output_dir / "artifacts.json",
            requested_identifiers=requested_identifiers,
            artifacts=artifacts,
        )


def _process_shared_batch(
    request: GaiaProductRequest,
    claimed: list[tuple[_SharedCandidate, Any]],
    *,
    library: LibraryStore,
    run_id: str | None,
    refresh: bool,
    loader: DataLinkLoader,
    views: dict[str, Any],
    artifacts_by_identifier: dict[str, list[GaiaArtifact]],
) -> None:
    """Download, normalize, publish, and fence-commit one actually claimed batch."""
    from astro_pipeline.library.identity import identity_hash
    from astro_pipeline.library.products import (
        ProductViewItem,
        StagedProductArtifact,
        ready_view_item,
        save_staged_product,
    )
    from astro_pipeline.library.store import LeaseLostError

    if not claimed:
        return
    candidates = [candidate for candidate, _ in claimed]
    batch = [candidate.identifier for candidate in candidates]
    attempt_request = {
        "contract": request.immutable_contract(),
        "identifiers": batch,
        "archive_tag": request.archive_tag,
        "requested_batch_size": request.batch_size,
        "refresh": refresh,
    }
    attempt_key = identity_hash(
        [request.immutable_contract(), batch], namespace="gaia-product-batch"
    )
    attempt_id = library.record_attempt(
        "gaia-product-batch", attempt_key, attempt_request, run_id=run_id
    )
    actual_batch_id = f"attempt-{attempt_id}"
    library.blobs.staging.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f"gaia-{_safe_file_stem(actual_batch_id)}-",
            dir=library.blobs.staging,
        )
    )
    try:
        response = loader(
            ids=batch,
            data_release=request.archive_tag,
            data_structure=request.data_structure,
            retrieval_type=request.retrieval_type,
            linking_parameter=request.linking_parameter,
            valid_data=request.valid_data,
            format=request.output_format,
        )
        normalized_dir = staging / "normalized"
        archive_dir = staging / "archive_tables"
        normalized_dir.mkdir(parents=True, exist_ok=True)
        archive_dir.mkdir(parents=True, exist_ok=True)
        staged_gaia = _materialize_tables(
            response or {},
            request=request,
            batch=batch,
            output_dir=normalized_dir,
            archive_dir=archive_dir,
            batch_index=0,
            batch_id=actual_batch_id,
            strict_association=True,
        )
    except Exception as exc:
        error = str(exc)
        library.finish_attempt(attempt_id, "failed", error=error)
        for candidate, _lease in claimed:
            library.record_product_error(candidate.item_key, error)
            status = (
                "refresh_failed"
                if refresh and candidate.previous_version is not None
                else "failed"
            )
            view = ProductViewItem(
                identifier=candidate.identifier,
                item_key=candidate.item_key,
                status=status,
                cache_status="refresh_failed" if refresh else "download_failed",
                attempt_id=attempt_id,
                actual_batch_id=actual_batch_id,
                error=error,
                previous_ready_version_id=(
                    candidate.previous_version or {}
                ).get("version_id"),
            )
            views[candidate.identifier] = view
            artifacts_by_identifier[candidate.identifier] = [
                _failed_artifact(
                    request,
                    candidate.identifier,
                    error,
                    item_key=candidate.item_key,
                    attempt_id=attempt_id,
                    actual_batch_id=actual_batch_id,
                    cache_status=view.cache_status,
                )
            ]
        shutil.rmtree(staging, ignore_errors=True)
        return

    by_identifier: dict[str, list[GaiaArtifact]] = {value: [] for value in batch}
    for artifact in staged_gaia:
        if artifact.identifier in by_identifier and artifact.success:
            by_identifier[artifact.identifier].append(artifact)
    archive_paths = sorted(
        {
            Path(artifact.archive_table_path).resolve()
            for artifact in staged_gaia
            if artifact.archive_table_path
        },
        key=lambda value: value.name,
    )
    archive_slots = {
        path: f"archive:{index:03d}" for index, path in enumerate(archive_paths)
    }
    published_by_path: dict[Path, Any] = {}
    failed = 0
    stale = 0

    for candidate, lease in claimed:
        item_artifacts = sorted(
            by_identifier[candidate.identifier],
            key=lambda item: (item.table_key, Path(item.normalized_path).name),
        )
        if not item_artifacts:
            error = f"Gaia DataLink returned no {request.retrieval_type} product"
            try:
                library.put_cache(
                    "product",
                    candidate.item_key,
                    {
                        "contract": request.immutable_contract(),
                        "identifier": candidate.identifier,
                        "error": error,
                        "attempt_id": attempt_id,
                        "actual_batch_id": actual_batch_id,
                    },
                    outcome="negative",
                    ttl_seconds=library.negative_ttl_seconds,
                    lease=lease,
                )
            except LeaseLostError:
                stale += 1
                artifacts_by_identifier[candidate.identifier] = [
                    _failed_artifact(
                        request,
                        candidate.identifier,
                        "product lease was lost before absence could be committed",
                        item_key=candidate.item_key,
                        attempt_id=attempt_id,
                        actual_batch_id=actual_batch_id,
                        cache_status="lease_lost",
                    )
                ]
                continue
            observation = library.get_cache("product", candidate.item_key) or {}
            view = ProductViewItem(
                identifier=candidate.identifier,
                item_key=candidate.item_key,
                status="absent",
                cache_status="negative",
                attempt_id=attempt_id,
                actual_batch_id=actual_batch_id,
                error=error,
                previous_ready_version_id=(
                    candidate.previous_version or {}
                ).get("version_id"),
                observed_at=observation.get("observed_at"),
                expires_at=observation.get("expires_at"),
            )
            views[candidate.identifier] = view
            artifacts_by_identifier[candidate.identifier] = [
                _failed_artifact(
                    request,
                    candidate.identifier,
                    error,
                    item_key=candidate.item_key,
                    attempt_id=attempt_id,
                    actual_batch_id=actual_batch_id,
                    cache_status="negative",
                )
            ]
            continue

        staged_records: list[StagedProductArtifact] = []
        used_archives: dict[Path, GaiaArtifact] = {}
        for index, artifact in enumerate(item_artifacts):
            archive_path = Path(artifact.archive_table_path).resolve()
            archive_slot = archive_slots[archive_path]
            used_archives.setdefault(archive_path, artifact)
            staged_records.append(
                StagedProductArtifact(
                    role="normalized",
                    slot=f"normalized:{index:03d}",
                    path=Path(artifact.normalized_path),
                    extension=_extension(request.output_format),
                    content_type=artifact.content_type,
                    metadata={
                        "is_primary": index == 0,
                        "table_key": artifact.table_key,
                        "archive_slot": archive_slot,
                        "gaia_metadata": artifact.metadata,
                        "preservation": "astroquery_table_reencoded",
                        "staged_checksum_sha256": artifact.checksum_sha256,
                        "attempt_id": attempt_id,
                        "actual_batch_id": actual_batch_id,
                    },
                )
            )
        for archive_path, artifact in sorted(
            used_archives.items(), key=lambda item: archive_slots[item[0]]
        ):
            staged_records.append(
                StagedProductArtifact(
                    role="archive",
                    slot=archive_slots[archive_path],
                    path=archive_path,
                    extension=_extension(request.output_format),
                    content_type=artifact.content_type,
                    metadata={
                        "table_key": artifact.table_key,
                        "preservation": "astroquery_table_reencoded",
                        "staged_checksum_sha256": artifact.archive_checksum_sha256,
                        "attempt_id": attempt_id,
                        "actual_batch_id": actual_batch_id,
                    },
                )
            )
        try:
            # Replace any earlier negative observation once the Archive has
            # explicitly returned a product.  Doing this before publication
            # avoids a committed version being hidden by a stale negative if a
            # later cache write fails.
            library.put_cache(
                "product",
                candidate.item_key,
                {
                    "attempt_id": attempt_id,
                    "actual_batch_id": actual_batch_id,
                    "outcome": "product_returned",
                },
                outcome="success",
                ttl_seconds=library.positive_ttl_seconds,
                lease=lease,
            )
            version = save_staged_product(
                library,
                contract=request.immutable_contract(),
                identifier=candidate.identifier,
                staged_artifacts=staged_records,
                lease=lease,
                metadata={
                    "attempt_id": attempt_id,
                    "actual_batch_id": actual_batch_id,
                    "run_id": run_id,
                    "requested_identifiers": batch,
                    "preservation": "astroquery_table_reencoded",
                },
                # This is only reached after a verified Archive response.  If a
                # content-addressed destination is damaged, BlobStore quarantines
                # the old bytes before atomically repairing it.
                repair=True,
                published_by_path=published_by_path,
            )
        except LeaseLostError:
            stale += 1
            artifacts_by_identifier[candidate.identifier] = [
                _failed_artifact(
                    request,
                    candidate.identifier,
                    "product lease was lost before the version could be committed",
                    item_key=candidate.item_key,
                    attempt_id=attempt_id,
                    actual_batch_id=actual_batch_id,
                    cache_status="lease_lost",
                )
            ]
            continue
        except Exception as exc:
            failed += 1
            error = str(exc)
            library.record_product_error(candidate.item_key, error)
            status = (
                "refresh_failed"
                if refresh and candidate.previous_version is not None
                else "failed"
            )
            view = ProductViewItem(
                identifier=candidate.identifier,
                item_key=candidate.item_key,
                status=status,
                cache_status="refresh_failed" if refresh else "commit_failed",
                attempt_id=attempt_id,
                actual_batch_id=actual_batch_id,
                error=error,
                previous_ready_version_id=(
                    candidate.previous_version or {}
                ).get("version_id"),
            )
            views[candidate.identifier] = view
            artifacts_by_identifier[candidate.identifier] = [
                _failed_artifact(
                    request,
                    candidate.identifier,
                    error,
                    item_key=candidate.item_key,
                    attempt_id=attempt_id,
                    actual_batch_id=actual_batch_id,
                    cache_status=view.cache_status,
                )
            ]
            continue

        cache_status = "refreshed" if refresh else (
            "repaired" if candidate.repair else "downloaded"
        )
        view = ready_view_item(
            library,
            version,
            cache_status=cache_status,
            attempt_id=attempt_id,
            actual_batch_id=actual_batch_id,
        )
        view.identifier = candidate.identifier
        views[candidate.identifier] = view
        artifacts_by_identifier[candidate.identifier] = (
            _gaia_artifacts_from_ready_view(request, view)
        )

    if stale or failed:
        status = "partial" if len(claimed) > stale + failed else "failed"
        error = (
            f"{failed} product commits failed; {stale} product leases were lost"
        )
        library.finish_attempt(attempt_id, status, error=error)
    else:
        library.finish_attempt(attempt_id, "completed")
    shutil.rmtree(staging, ignore_errors=True)
    if stale:
        # A stale owner may leave immutable orphan blobs, but must not project
        # any result into the label-local current view.
        raise LeaseLostError(
            f"lost fencing token for {stale} product item(s) in {actual_batch_id}"
        )


def _gaia_artifacts_from_ready_view(
    request: GaiaProductRequest, view: Any
) -> list[GaiaArtifact]:
    refs = list(view.artifacts)
    archives = {
        str(item.get("slot")): item
        for item in refs
        if item.get("role") == "archive"
    }
    first_archive = next(
        (item for item in refs if item.get("role") == "archive"), None
    )
    normalized = sorted(
        (item for item in refs if item.get("role") == "normalized"),
        key=lambda item: str(item.get("slot", "")),
    )
    results: list[GaiaArtifact] = []
    for normalized_ref in normalized:
        normalized_blob = dict(normalized_ref.get("blob") or {})
        metadata = dict(normalized_ref.get("metadata") or {})
        archive_ref = archives.get(str(metadata.get("archive_slot"))) or first_archive
        archive_blob = dict((archive_ref or {}).get("blob") or {})
        gaia_metadata = dict(metadata.get("gaia_metadata") or {})
        gaia_metadata["shared_library"] = {
            "item_key": view.item_key,
            "version_id": view.version_id,
            "normalized_slot": normalized_ref.get("slot"),
            "archive_slot": (archive_ref or {}).get("slot", ""),
            "cache_status": view.cache_status,
        }
        results.append(
            GaiaArtifact(
                release=request.release,
                archive_tag=request.archive_tag,
                retrieval_type=request.retrieval_type,
                linking_parameter=request.linking_parameter,
                identifier=view.identifier,
                output_format=request.output_format,
                data_structure=request.data_structure,
                normalized_path=str(normalized_ref.get("verified_path", "")),
                archive_table_path=str(
                    (archive_ref or {}).get("verified_path", "")
                ),
                checksum_sha256=str(normalized_blob.get("sha256", "")),
                archive_checksum_sha256=str(archive_blob.get("sha256", "")),
                content_type=str(normalized_blob.get("content_type", "")),
                table_key=str(metadata.get("table_key", "")),
                metadata=gaia_metadata,
                item_key=view.item_key,
                version_id=view.version_id,
                version_created_at=view.created_at,
                attempt_id=view.attempt_id,
                actual_batch_id=view.actual_batch_id,
                cache_status=view.cache_status,
                normalized_blob=normalized_blob,
                archive_blob=archive_blob,
            )
        )
    return results


def _emit_shared_progress(
    request: GaiaProductRequest,
    identifiers: list[str],
    views: dict[str, Any],
    callback: Optional[ProgressCallback],
    *,
    done: bool,
) -> None:
    if callback is None:
        return
    callback(
        {
            "release": request.release,
            "retrieval_type": request.retrieval_type,
            "pending_at_start": len(identifiers),
            "processed": sum(identifier in views for identifier in identifiers),
            "completed_this_run": sum(
                views.get(identifier) is not None
                and views[identifier].status == "ready"
                for identifier in identifiers
            ),
            "failed_this_run": sum(
                views.get(identifier) is not None
                and views[identifier].status != "ready"
                for identifier in identifiers
            ),
            "done": done,
        }
    )


def _validate_existing_shared_view(
    output_dir: Path,
    contract: dict[str, Any],
    library: LibraryStore,
) -> None:
    request_path = output_dir / "product_request.json"
    if (
        output_dir.is_dir()
        and not request_path.exists()
        and any(output_dir.iterdir())
    ):
        raise ValueError(
            "Existing non-empty Gaia product output directory is missing "
            "product_request.json and cannot be silently adopted; use a new "
            "output directory"
        )
    if request_path.exists():
        existing = json.loads(request_path.read_text(encoding="utf-8"))
        if existing.get("contract") != contract:
            raise ValueError(
                "DataLink output directory already contains a different request "
                f"contract: {request_path}"
            )
    refs_path = output_dir / "product_refs.json"
    if refs_path.exists():
        refs = json.loads(refs_path.read_text(encoding="utf-8"))
        if refs.get("schema_version") != "2.0":
            raise ValueError(f"Unsupported product refs schema at {refs_path}")
        if refs.get("contract") != contract:
            raise ValueError(
                f"Product refs contain a different request contract: {refs_path}"
            )
        storage = refs.get("storage") or {}
        root = Path(library.root).resolve()
        absolute_locator = storage.get("library_root")
        relative_locator = storage.get("relative_library_root")
        absolute_matches = bool(absolute_locator) and Path(
            str(absolute_locator)
        ).resolve() == root
        relative_matches = bool(relative_locator) and (
            output_dir / str(relative_locator)
        ).resolve() == root
        if storage.get("kind") != "shared_library" or not (
            absolute_matches or relative_matches
        ):
            raise ValueError(
                f"Product refs point at a different shared library: {refs_path}"
            )
    elif output_dir.exists() and (
        (output_dir / "archive_tables").exists()
        or any(output_dir.glob("*.fits"))
        or any(output_dir.glob("*.vot"))
        or any(output_dir.glob("*.ecsv"))
        or any(output_dir.glob("*.csv"))
    ):
        raise ValueError(
            "Existing label-local product files cannot be silently adopted by a "
            "shared-library view; use a new output directory"
        )


def gaia_product_status(output_dir: Path | str) -> dict[str, Any]:
    """Read a generic DataLink output directory without network access."""
    output_dir = Path(output_dir)
    if not output_dir.exists():
        return {"exists": False, "error": f"no Gaia product output at {output_dir}"}

    def read_json(name: str) -> dict[str, Any]:
        path = output_dir / name
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    try:
        request_data = read_json("product_request.json")
        checkpoint = read_json("checkpoint.json")
        artifact_data = read_json("artifacts.json")
        refs_data = read_json("product_refs.json")
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "exists": True,
            "output_dir": str(output_dir.resolve()),
            "error": f"could not read Gaia product state: {exc}",
        }

    completed = checkpoint.get("completed", [])
    failed_value = checkpoint.get("failed", [])
    failed = list(failed_value) if isinstance(failed_value, dict) else failed_value
    artifacts = artifact_data.get("artifacts", [])
    contract = request_data.get("contract", {})
    shared_archive_blobs = {
        artifact.get("blob", {}).get("sha256")
        for item in refs_data.get("items", [])
        for artifact in item.get("artifacts", [])
        if artifact.get("role") == "archive"
        and artifact.get("blob", {}).get("sha256")
    }
    local_archive_count = len(list((output_dir / "archive_tables").glob("*")))
    return {
        "exists": True,
        "output_dir": str(output_dir.resolve()),
        "contract": contract,
        "n_completed_identifiers": len(completed),
        "n_failed_identifiers": len(failed),
        "completed_identifiers": completed,
        "failed_identifiers": failed,
        "n_artifacts": len(artifacts),
        "n_success_artifacts": sum(bool(item.get("success")) for item in artifacts),
        "n_failed_artifacts": sum(not bool(item.get("success")) for item in artifacts),
        "n_archive_tables": len(shared_archive_blobs) or local_archive_count,
        "storage_kind": (refs_data.get("storage") or {}).get("kind", "label_local"),
        "resumable": bool(contract),
    }


def _materialize_tables(
    response: dict[str, list[Any]],
    *,
    request: GaiaProductRequest,
    batch: list[str],
    output_dir: Path,
    archive_dir: Path,
    batch_index: int,
    batch_id: str | None = None,
    strict_association: bool = False,
) -> list[GaiaArtifact]:
    artifacts: list[GaiaArtifact] = []
    extension = _extension(request.output_format)
    identifier_columns = _identifier_column_candidates(request.linking_parameter)
    table_counter = 0

    for key, values in response.items():
        for value in values:
            table = value if isinstance(value, Table) else Table(value)
            safe_key = _safe_file_stem(str(key) or "product")
            if batch_id:
                archive_name = (
                    f"{_safe_file_stem(batch_id)}-{table_counter:03d}-{safe_key}."
                    f"{extension}"
                )
            else:
                archive_name = (
                    f"batch-{batch_index:05d}-{table_counter:03d}-{safe_key}."
                    f"{extension}"
                )
            archive_path = archive_dir / archive_name
            _write_table_atomic(table, archive_path, request.output_format)
            table_counter += 1

            names = {str(name).lower(): str(name) for name in table.colnames}
            identifier_column = next(
                (names[name] for name in identifier_columns if name in names), None
            )
            groups: list[tuple[str, Table]] = []
            if identifier_column:
                seen: set[str] = set()
                for raw_identifier in table[identifier_column]:
                    identifier = str(raw_identifier).strip()
                    if not identifier or identifier in seen:
                        continue
                    seen.add(identifier)
                    mask = [str(item).strip() == identifier for item in table[identifier_column]]
                    groups.append((identifier, table[mask]))
            else:
                metadata_identifier = _identifier_from_metadata(
                    table.meta, request.linking_parameter
                )
                if metadata_identifier:
                    groups.append((metadata_identifier, table))
                else:
                    key_matches = [identifier for identifier in batch if identifier in str(key)]
                    if len(key_matches) == 1:
                        groups.append((key_matches[0], table))
                if not groups and len(batch) == 1:
                    groups.append((batch[0], table))

            if strict_association and not groups:
                raise ValueError(
                    f"Gaia DataLink table {key!r} could not be associated with "
                    f"any requested {request.linking_parameter} identifier"
                )
            unexpected = [identifier for identifier, _ in groups if identifier not in batch]
            if strict_association and unexpected:
                raise ValueError(
                    f"Gaia DataLink table {key!r} returned unexpected "
                    f"{request.linking_parameter} identifiers: {unexpected[:5]}"
                )

            for identifier, subset in groups:
                normalized_path = output_dir / f"{identifier}.{extension}"
                if normalized_path.exists() and any(
                    item.identifier == identifier and item.success for item in artifacts
                ):
                    normalized_path = output_dir / (
                        f"{identifier}__{safe_key}-{table_counter:03d}.{extension}"
                    )
                _write_table_atomic(subset, normalized_path, request.output_format)
                artifacts.append(
                    GaiaArtifact(
                        release=request.release,
                        archive_tag=request.archive_tag,
                        retrieval_type=request.retrieval_type,
                        linking_parameter=request.linking_parameter,
                        identifier=identifier,
                        output_format=request.output_format,
                        data_structure=request.data_structure,
                        normalized_path=str(normalized_path.resolve()),
                        archive_table_path=str(archive_path.resolve()),
                        checksum_sha256=_sha256(normalized_path),
                        archive_checksum_sha256=_sha256(archive_path),
                        content_type=_content_type(request.output_format),
                        table_key=str(key),
                        metadata={
                            "table_meta": _json_safe(table.meta),
                            "preservation": "astroquery_table_reencoded",
                            "n_rows": len(subset),
                            "columns": [str(name) for name in subset.colnames],
                        },
                    )
                )
    return artifacts


def _failed_artifact(
    request: GaiaProductRequest,
    identifier: str,
    error: str,
    *,
    item_key: str = "",
    attempt_id: str | int | None = None,
    actual_batch_id: str = "",
    cache_status: str = "",
) -> GaiaArtifact:
    return GaiaArtifact(
        release=request.release,
        archive_tag=request.archive_tag,
        retrieval_type=request.retrieval_type,
        linking_parameter=request.linking_parameter,
        identifier=identifier,
        output_format=request.output_format,
        data_structure=request.data_structure,
        success=False,
        error=error,
        item_key=item_key,
        attempt_id=attempt_id,
        actual_batch_id=actual_batch_id,
        cache_status=cache_status,
    )


def _open_or_create_request_contract(path: Path, request: GaiaProductRequest) -> None:
    contract = request.immutable_contract()
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("contract") != contract:
            raise ValueError(
                f"DataLink output directory already contains a different request contract: {path}"
            )
        return
    _write_json_atomic(
        path,
        {
            "schema_version": "1.0",
            "created_at": _utc_now(),
            "contract": contract,
        },
    )


def _merge_artifact_manifest(path: Path, artifacts: Sequence[GaiaArtifact]) -> None:
    data = {"schema_version": "1.0", "updated_at": _utc_now(), "artifacts": []}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    by_key = {
        (
            str(item.get("release")),
            str(item.get("retrieval_type")),
            str(item.get("identifier")),
            str(item.get("normalized_path")),
        ): item
        for item in data.get("artifacts", [])
    }
    for artifact in artifacts:
        item = artifact.to_dict()
        key = (
            artifact.release,
            artifact.retrieval_type,
            artifact.identifier,
            artifact.normalized_path,
        )
        by_key[key] = item
    data["artifacts"] = list(by_key.values())
    data["updated_at"] = _utc_now()
    _write_json_atomic(path, data)


def _identifier_column_candidates(linking_parameter: str) -> tuple[str, ...]:
    return {
        "SOURCE_ID": ("source_id", "sourceid"),
        "TRANSIT_ID": ("transit_id", "transitid"),
        "IMAGE_ID": ("image_id", "sif_observation_id", "imageid"),
    }[linking_parameter]


def _identifier_from_metadata(metadata: dict[str, Any], linking_parameter: str) -> str:
    lowered = {str(key).lower(): value for key, value in metadata.items()}
    for candidate in _identifier_column_candidates(linking_parameter):
        value = lowered.get(candidate)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _write_table_atomic(table: Table, path: Path, output_format: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    kwargs: dict[str, Any] = {
        "format": _astropy_format(output_format),
        "overwrite": True,
    }
    if output_format == "votable_plain":
        kwargs["tabledata_format"] = "tabledata"
    table.write(str(temporary), **kwargs)
    temporary.replace(path)


def _astropy_format(output_format: str) -> str:
    return {
        "fits": "fits",
        "votable": "votable",
        "votable_plain": "votable",
        "csv": "ascii.csv",
        "ecsv": "ascii.ecsv",
    }[output_format]


def _extension(output_format: str) -> str:
    return {
        "fits": "fits",
        "votable": "vot",
        "votable_plain": "vot",
        "csv": "csv",
        "ecsv": "ecsv",
    }[output_format]


def _content_type(output_format: str) -> str:
    return {
        "fits": "application/fits",
        "votable": "application/x-votable+xml",
        "votable_plain": "application/x-votable+xml",
        "csv": "text/csv",
        "ecsv": "text/plain; charset=utf-8",
    }[output_format]


def _safe_file_stem(value: str) -> str:
    stem = Path(value).stem
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("._")
    return safe[:120] or "product"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
