"""Shared-library cache policy for source references and paper enrichment.

This module owns cache/lease orchestration only.  Callers provide the existing
network functions, keeping SQLite transactions short and all remote work outside
the store.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable, Mapping, Protocol, Sequence

from astro_pipeline.library.identity import source_cache_key

SOURCE_REFERENCES_NAMESPACE = "simbad-source-references"
PAPER_METADATA_NAMESPACE = "simbad-paper-metadata"
DOI_ARXIV_NAMESPACE = "doi-arxiv"


class _LibraryStore(Protocol):
    clock: Callable[[], float]
    positive_ttl_seconds: float
    negative_ttl_seconds: float
    lease_seconds: float
    wait_timeout_seconds: float

    def get_source_references(
        self, release: str, source_id: Any, include_stale: bool = False
    ) -> dict[str, Any] | None: ...

    def put_source_references(
        self,
        release: str,
        source_id: Any,
        papers: Sequence[Mapping[str, Any]],
        main_id: str = "",
        outcome: str = "success",
        ttl_seconds: float = 30 * 24 * 60 * 60,
        lease: Any = None,
    ) -> str: ...

    def get_cache(
        self, namespace: str, key: str, include_stale: bool = False
    ) -> dict[str, Any] | None: ...

    def put_cache(
        self,
        namespace: str,
        key: str,
        payload: Any,
        outcome: str = "success",
        ttl_seconds: float = 30 * 24 * 60 * 60,
        lease: Any = None,
        error: Any = None,
    ) -> str: ...

    def hold_lease(
        self, namespace: str, key: str, ttl: float = 120, wait_timeout: float = 0
    ): ...

    def upsert_paper(self, metadata: Mapping[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class CacheResult:
    """A cache value plus whether this call performed the remote request."""

    value: Any
    fetched: bool


def _attempt_start(
    store: _LibraryStore,
    *,
    namespace: str,
    key: str,
    request: Mapping[str, Any],
    run_id: str | None,
) -> str | None:
    if run_id is None or not hasattr(store, "record_attempt"):
        return None
    return store.record_attempt(namespace, key, request, run_id=run_id)


def _attempt_finish(
    store: _LibraryStore,
    attempt_id: str | None,
    *,
    status: str,
    error: BaseException | None = None,
) -> None:
    if attempt_id is None:
        return
    store.finish_attempt(attempt_id, status, error=error)


def _changed(current: Mapping[str, Any], previous: Mapping[str, Any] | None, id_key: str) -> bool:
    return previous is None or current.get(id_key) != previous.get(id_key)


def cached_source_references(
    store: _LibraryStore,
    *,
    gaia_release: str,
    source_id: str,
    fetch: Callable[[], Mapping[str, Any]],
    refresh: bool = False,
    run_id: str | None = None,
) -> CacheResult:
    """Return one fresh source-reference snapshot, fetching under a fenced lease.

    ``fetch`` returns ``papers`` plus optional ``main_id`` and ``outcome``.  A
    non-empty successful bibliography uses the positive TTL; explicit empty and
    unresolved outcomes use the negative TTL.  Exceptions append a failed
    snapshot for history but never replace the last completed snapshot.
    """
    current = store.get_source_references(gaia_release, source_id)
    if current is not None and not refresh:
        return CacheResult(current, fetched=False)
    previous = store.get_source_references(
        gaia_release, source_id, include_stale=True
    )
    key = source_cache_key(gaia_release, source_id)
    with store.hold_lease(
        SOURCE_REFERENCES_NAMESPACE,
        key,
        ttl=store.lease_seconds,
        wait_timeout=store.wait_timeout_seconds,
    ) as lease:
        current = store.get_source_references(gaia_release, source_id)
        if current is not None and (
            not refresh or _changed(current, previous, "snapshot_id")
        ):
            return CacheResult(current, fetched=False)

        attempt_id = _attempt_start(
            store,
            namespace=SOURCE_REFERENCES_NAMESPACE,
            key=key,
            request={"gaia_release": gaia_release, "source_id": str(source_id)},
            run_id=run_id,
        )
        try:
            observation = dict(fetch())
            papers = list(observation.get("papers", ()))
            outcome = str(observation.get("outcome", "success"))
            ttl = (
                store.positive_ttl_seconds
                if papers and outcome == "success"
                else store.negative_ttl_seconds
            )
            store.put_source_references(
                gaia_release,
                source_id,
                papers,
                main_id=str(observation.get("main_id", "")),
                outcome=outcome,
                ttl_seconds=ttl,
                lease=lease,
            )
        except Exception as exc:
            # Failure observations are history only: Store never advances the
            # current snapshot for these outcomes.
            store.put_source_references(
                gaia_release,
                source_id,
                (),
                outcome="error",
                ttl_seconds=store.negative_ttl_seconds,
                lease=lease,
            )
            _attempt_finish(store, attempt_id, status="failed", error=exc)
            raise
        _attempt_finish(store, attempt_id, status="completed")

        current = store.get_source_references(gaia_release, source_id)
        if current is None:
            raise RuntimeError("completed source-reference snapshot is not readable")
        return CacheResult(current, fetched=True)


def cached_paper_metadata(
    store: _LibraryStore,
    *,
    bibcode: str,
    fetch: Callable[[], Mapping[str, Any]],
    refresh: bool = False,
    run_id: str | None = None,
) -> CacheResult:
    """Return basic paper metadata independently of DOI-to-arXiv resolution."""
    bibcode = str(bibcode).strip()
    if not bibcode:
        raise ValueError("bibcode must not be blank")
    current = store.get_cache(PAPER_METADATA_NAMESPACE, bibcode)
    if current is not None and not refresh:
        payload = dict(current.get("payload") or {})
        payload["bibcode"] = bibcode
        store.upsert_paper(payload)
        return CacheResult(payload, fetched=False)
    previous = store.get_cache(
        PAPER_METADATA_NAMESPACE, bibcode, include_stale=True
    )

    with store.hold_lease(
        PAPER_METADATA_NAMESPACE,
        bibcode,
        ttl=store.lease_seconds,
        wait_timeout=store.wait_timeout_seconds,
    ) as lease:
        current = store.get_cache(PAPER_METADATA_NAMESPACE, bibcode)
        if current is not None and (
            not refresh or _changed(current, previous, "id")
        ):
            payload = dict(current.get("payload") or {})
            payload["bibcode"] = bibcode
            store.upsert_paper(payload)
            return CacheResult(payload, fetched=False)

        attempt_id = _attempt_start(
            store,
            namespace=PAPER_METADATA_NAMESPACE,
            key=bibcode,
            request={"bibcode": bibcode},
            run_id=run_id,
        )
        try:
            payload = dict(fetch())
            payload["bibcode"] = bibcode
            # No title or DOI is an explicit, short-lived negative metadata
            # result rather than a transport failure.
            found = bool(str(payload.get("title", "")).strip()) or bool(
                str(payload.get("doi", "")).strip()
            )
            outcome = "success" if found else "not_found"
            ttl = (
                store.positive_ttl_seconds
                if found
                else store.negative_ttl_seconds
            )
            store.put_cache(
                PAPER_METADATA_NAMESPACE,
                bibcode,
                payload,
                outcome=outcome,
                ttl_seconds=ttl,
                lease=lease,
            )
            payload = store.upsert_paper(payload)
        except Exception as exc:
            store.put_cache(
                PAPER_METADATA_NAMESPACE,
                bibcode,
                {"bibcode": bibcode},
                outcome="error",
                ttl_seconds=store.negative_ttl_seconds,
                lease=lease,
                error=exc,
            )
            _attempt_finish(store, attempt_id, status="failed", error=exc)
            raise
        _attempt_finish(store, attempt_id, status="completed")
        return CacheResult(payload, fetched=True)


_DOI_PREFIX_RE = re.compile(r"^(?:doi:\s*|https?://(?:dx\.)?doi\.org/)", re.IGNORECASE)


def normalize_doi(doi: str) -> str:
    """Normalize equivalent DOI spellings for one shared cache key."""
    value = _DOI_PREFIX_RE.sub("", str(doi).strip()).strip().lower()
    if not value:
        raise ValueError("doi must not be blank")
    return value


def cached_doi_arxiv(
    store: _LibraryStore,
    *,
    doi: str,
    resolve: Callable[[], str | None],
    refresh: bool = False,
    run_id: str | None = None,
) -> CacheResult:
    """Resolve DOI to arXiv with distinct found/not-found cache outcomes."""
    key = normalize_doi(doi)
    current = store.get_cache(DOI_ARXIV_NAMESPACE, key)
    if current is not None and not refresh:
        payload = current.get("payload") or {}
        return CacheResult(payload.get("arxiv_id"), fetched=False)
    previous = store.get_cache(DOI_ARXIV_NAMESPACE, key, include_stale=True)

    with store.hold_lease(
        DOI_ARXIV_NAMESPACE,
        key,
        ttl=store.lease_seconds,
        wait_timeout=store.wait_timeout_seconds,
    ) as lease:
        current = store.get_cache(DOI_ARXIV_NAMESPACE, key)
        if current is not None and (
            not refresh or _changed(current, previous, "id")
        ):
            payload = current.get("payload") or {}
            return CacheResult(payload.get("arxiv_id"), fetched=False)

        attempt_id = _attempt_start(
            store,
            namespace=DOI_ARXIV_NAMESPACE,
            key=key,
            request={"doi": key},
            run_id=run_id,
        )
        try:
            arxiv_id = resolve()
            found = bool(arxiv_id)
            payload = {"doi": key, "arxiv_id": str(arxiv_id) if found else None}
            store.put_cache(
                DOI_ARXIV_NAMESPACE,
                key,
                payload,
                outcome="found" if found else "not_found",
                ttl_seconds=(
                    store.positive_ttl_seconds
                    if found
                    else store.negative_ttl_seconds
                ),
                lease=lease,
            )
        except Exception as exc:
            store.put_cache(
                DOI_ARXIV_NAMESPACE,
                key,
                {"doi": key},
                outcome="error",
                ttl_seconds=store.negative_ttl_seconds,
                lease=lease,
                error=exc,
            )
            _attempt_finish(store, attempt_id, status="failed", error=exc)
            raise
        _attempt_finish(store, attempt_id, status="completed")
        return CacheResult(payload["arxiv_id"], fetched=True)
