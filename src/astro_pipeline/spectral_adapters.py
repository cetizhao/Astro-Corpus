"""Release-qualified adapters for spectra already stored in the local library.

This module is deliberately read-only.  It selects either a shared-library
active product or an exact corpus-run pin, verifies the selected blob, and only
then invokes a registered science parser.  It never downloads a product,
changes a product pointer, or creates/migrates SQLite state.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import threading
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from astro_pipeline import spectra as _spectra
from astro_pipeline.gaia_releases import normalize_gaia_release
from astro_pipeline.library.files import BlobIntegrityError
from astro_pipeline.library.identity import (
    json_value,
    normalize_identifier,
    product_item_key,
    source_identity,
)
from astro_pipeline.library.refs import (
    ProductReferenceError,
    active_corpus_dir,
    read_reference_documents,
    resolve_product,
)
from astro_pipeline.library.store import LibraryStore


SpectrumParser = Callable[[Path | str], dict[str, Any]]

DR3_XP_FLUX_UNIT = "W m-2 nm-1"
SPECTRUM_STATUSES = frozenset(
    {
        "ready",
        "unknown",
        "unavailable",
        "not_downloaded",
        "pending",
        "failed",
        "corrupt",
        "unsupported",
    }
)


@dataclass(frozen=True)
class SpectrumAdapter:
    """One exact release/product parser contract."""

    release: str
    retrieval_type: str
    parser: SpectrumParser
    parser_version: str
    series_name: str
    wavelength_unit: str
    flux_unit: str
    output_formats: frozenset[str]


def _read_dr3_xp_continuous(path: Path | str) -> dict[str, Any]:
    # Resolve through the module at call time so tests and downstream users can
    # replace the heavy parser without replacing the registry itself.
    return _spectra.read_xp_continuous(path)


def _read_dr3_xp_sampled(path: Path | str) -> dict[str, Any]:
    return _spectra.read_xp_sampled(path)


SPECTRUM_ADAPTERS: Mapping[tuple[str, str], SpectrumAdapter] = MappingProxyType(
    {
        ("dr3", "XP_CONTINUOUS"): SpectrumAdapter(
            release="dr3",
            retrieval_type="XP_CONTINUOUS",
            parser=_read_dr3_xp_continuous,
            parser_version="gaia-dr3-xp-continuous-v1",
            series_name="XP combined",
            wavelength_unit="nm",
            flux_unit=DR3_XP_FLUX_UNIT,
            output_formats=frozenset({"fits"}),
        ),
        ("dr3", "XP_SAMPLED"): SpectrumAdapter(
            release="dr3",
            retrieval_type="XP_SAMPLED",
            parser=_read_dr3_xp_sampled,
            parser_version="gaia-dr3-xp-sampled-v1",
            series_name="XP combined",
            wavelength_unit="nm",
            flux_unit=DR3_XP_FLUX_UNIT,
            output_formats=frozenset({"fits"}),
        ),
    }
)

# This order is part of the public selection contract.  DR4 products remain
# absent until the Archive publishes a real product/serialization contract and
# a parser is validated against it; their provisional names are not DR3 aliases.
_SPECTRUM_PREFERENCE: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {"dr3": ("XP_CONTINUOUS", "XP_SAMPLED")}
)

_PARSED_CACHE_MAXSIZE = 128
_PARSED_CACHE: OrderedDict[
    tuple[str, str, str, str, str, str | None], dict[str, Any]
] = OrderedDict()
_PARSED_CACHE_LOCK = threading.RLock()


@dataclass(frozen=True)
class _ProductCandidate:
    item_key: str
    state: str
    active_version_id: str | None
    updated_at: Any
    last_error: str | None
    contract: dict[str, Any]


@dataclass(frozen=True)
class _CorpusSelection:
    corpus_id: str
    run_id: str
    directory: Path
    explicit_run: bool


def _empty_payload(
    *,
    status: str,
    release: str,
    source_id: str,
    scope: str,
    adapter: SpectrumAdapter | None = None,
    product_type: str | None = None,
    version_id: str | None = None,
    artifact_sha256: str | None = None,
    provenance: Mapping[str, Any] | None = None,
    warning: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    if status not in SPECTRUM_STATUSES:  # pragma: no cover - internal invariant
        raise ValueError(f"invalid spectrum status: {status}")
    return {
        "status": status,
        "release": release,
        "source_id": source_id,
        "product_type": product_type or (
            adapter.retrieval_type if adapter is not None else None
        ),
        "scope": scope,
        "version_id": version_id,
        "artifact_sha256": artifact_sha256,
        "parser_version": adapter.parser_version if adapter is not None else None,
        "wavelength_unit": (
            adapter.wavelength_unit if adapter is not None else "nm"
        ),
        "flux_unit": adapter.flux_unit if adapter is not None else None,
        "series": [],
        "quality": {},
        "provenance": dict(provenance or {}),
        "warning": warning,
        "error": error,
    }


def _join_warnings(*values: str | None) -> str | None:
    warnings = [str(value).strip() for value in values if str(value or "").strip()]
    return " ".join(warnings) or None


def _json_numeric_array(values: Any, *, field: str) -> list[float | None]:
    if values is None:
        return []
    try:
        array = np.asarray(values, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a numeric array") from exc
    if array.ndim != 1:
        raise ValueError(f"{field} must be one-dimensional")
    return [float(value) if np.isfinite(value) else None for value in array]


def _normalize_series(series: Mapping[str, Any], *, default_name: str) -> dict[str, Any]:
    wavelength = _json_numeric_array(series.get("wavelength"), field="wavelength")
    flux = _json_numeric_array(series.get("flux"), field="flux")
    if not wavelength or not flux:
        raise ValueError("a spectrum must contain wavelength and flux samples")
    if len(wavelength) != len(flux):
        raise ValueError("wavelength and flux arrays must have equal lengths")

    raw_error = series.get("flux_error")
    if raw_error is None:
        flux_error: list[float | None] = [None] * len(flux)
    else:
        flux_error = _json_numeric_array(raw_error, field="flux_error")
        if not flux_error:
            flux_error = [None] * len(flux)
        elif len(flux_error) != len(flux):
            raise ValueError("flux and flux_error arrays must have equal lengths")

    name = str(series.get("name") or default_name).strip() or default_name
    return {
        "name": name,
        "wavelength": wavelength,
        "flux": flux,
        "flux_error": flux_error,
    }


def normalize_spectrum_payload(
    parsed_spectrum: Mapping[str, Any],
    *,
    series_name: str = "XP combined",
) -> dict[str, Any]:
    """Normalize parser output into JSON-safe, equal-length series arrays.

    Current DR3 XP readers return one combined calibrated series, so this helper
    preserves it as one series.  A future adapter may return an explicit
    ``series`` list (for genuinely distinct channels); those names are retained
    rather than inferred from a product name.
    """
    if not isinstance(parsed_spectrum, Mapping):
        raise ValueError("spectrum parser must return an object")

    explicit = parsed_spectrum.get("series")
    if explicit is None:
        series_values: Sequence[Mapping[str, Any]] = [parsed_spectrum]
    elif isinstance(explicit, Sequence) and not isinstance(
        explicit, (str, bytes, bytearray)
    ):
        if not explicit:
            raise ValueError("spectrum parser returned no series")
        if not all(isinstance(item, Mapping) for item in explicit):
            raise ValueError("every spectrum series must be an object")
        series_values = explicit
    else:
        raise ValueError("spectrum series must be an array")

    quality_value = parsed_spectrum.get("quality", {})
    if quality_value is None:
        quality: dict[str, Any] = {}
    elif isinstance(quality_value, Mapping):
        normalized_quality = json_value(dict(quality_value))
        quality = normalized_quality if isinstance(normalized_quality, dict) else {}
    else:
        raise ValueError("spectrum quality must be an object")

    return {
        "series": [
            _normalize_series(item, default_name=series_name)
            for item in series_values
        ],
        "quality": quality,
    }


def _clear_parsed_spectrum_cache() -> None:
    """Clear the bounded parser cache (used by deterministic unit tests)."""
    with _PARSED_CACHE_LOCK:
        _PARSED_CACHE.clear()


def _parse_verified_artifact(
    adapter: SpectrumAdapter,
    path: Path,
    *,
    release: str,
    source_id: str,
    artifact_sha256: str,
    version_id: str | None,
) -> tuple[dict[str, Any], bool]:
    key = (
        release,
        source_id,
        adapter.retrieval_type,
        adapter.parser_version,
        artifact_sha256,
        version_id,
    )
    with _PARSED_CACHE_LOCK:
        cached = _PARSED_CACHE.get(key)
        if cached is not None:
            _PARSED_CACHE.move_to_end(key)
            return deepcopy(cached), True

    parsed = adapter.parser(path)
    parser_source_id = parsed.get("source_id") if isinstance(parsed, Mapping) else None
    if parser_source_id not in (None, ""):
        try:
            parsed_source_id = normalize_identifier(parser_source_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("the parsed spectrum has an invalid source identifier") from exc
        if parsed_source_id != source_id:
            raise ValueError("the parsed spectrum belongs to a different source")
    normalized = normalize_spectrum_payload(parsed, series_name=adapter.series_name)

    with _PARSED_CACHE_LOCK:
        _PARSED_CACHE[key] = deepcopy(normalized)
        _PARSED_CACHE.move_to_end(key)
        while len(_PARSED_CACHE) > _PARSED_CACHE_MAXSIZE:
            _PARSED_CACHE.popitem(last=False)
    return normalized, False


def _ready_payload(
    *,
    release: str,
    source_id: str,
    scope: str,
    adapter: SpectrumAdapter,
    version_id: str | None,
    artifact_sha256: str,
    parsed: Mapping[str, Any],
    provenance: Mapping[str, Any],
    warning: str | None,
) -> dict[str, Any]:
    result = _empty_payload(
        status="ready",
        release=release,
        source_id=source_id,
        scope=scope,
        adapter=adapter,
        version_id=version_id,
        artifact_sha256=artifact_sha256,
        provenance=provenance,
        warning=warning,
    )
    result["series"] = deepcopy(list(parsed["series"]))
    result["quality"] = deepcopy(dict(parsed["quality"]))
    return result


def _normalized_artifact(version: Mapping[str, Any]) -> Mapping[str, Any]:
    artifacts = version.get("artifacts")
    if not isinstance(artifacts, Sequence) or isinstance(
        artifacts, (str, bytes, bytearray)
    ):
        raise ValueError("product version has no artifact list")
    normalized = [
        item
        for item in artifacts
        if isinstance(item, Mapping)
        and str(item.get("role", "")).strip().lower() == "normalized"
    ]
    if len(normalized) == 1:
        return normalized[0]
    primary = [
        item
        for item in normalized
        if isinstance(item.get("metadata"), Mapping)
        and item["metadata"].get("is_primary") is True
    ]
    if len(primary) == 1:
        return primary[0]
    raise ValueError("product version has no unique primary normalized artifact")


def _contract_adapter(
    contract: Mapping[str, Any], release: str
) -> SpectrumAdapter | None:
    try:
        contract_release = normalize_gaia_release(str(contract.get("release", "")))
    except ValueError:
        return None
    if contract_release != release:
        return None
    if str(contract.get("linking_parameter", "")).strip().upper() != "SOURCE_ID":
        return None
    retrieval_type = str(contract.get("retrieval_type", "")).strip().upper()
    adapter = SPECTRUM_ADAPTERS.get((release, retrieval_type))
    if adapter is None:
        return None
    output_format = str(contract.get("output_format", "")).strip().lower()
    if output_format not in adapter.output_formats:
        return None
    return adapter


def _global_candidates(
    store: LibraryStore,
    release: str,
    source_id: str,
) -> tuple[dict[str, list[_ProductCandidate]], set[str]]:
    rows = store.read(
        "SELECT i.item_key, i.state, i.active_version_id, i.updated_at, "
        "i.last_error, c.contract_json FROM product_items AS i "
        "JOIN product_contracts AS c ON c.contract_hash = i.contract_hash "
        "WHERE i.identifier = ? ORDER BY i.updated_at DESC, i.item_key",
        (source_id,),
    )
    candidates: dict[str, list[_ProductCandidate]] = {}
    unsupported_formats: set[str] = set()
    for row in rows:
        try:
            contract_value = row.get("contract_json")
            contract = (
                json.loads(contract_value)
                if isinstance(contract_value, str)
                else dict(contract_value or {})
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        try:
            contract_release = normalize_gaia_release(str(contract.get("release", "")))
        except ValueError:
            continue
        if contract_release != release:
            continue
        retrieval_type = str(contract.get("retrieval_type", "")).strip().upper()
        adapter = SPECTRUM_ADAPTERS.get((release, retrieval_type))
        if adapter is None:
            continue
        if str(contract.get("linking_parameter", "")).strip().upper() != "SOURCE_ID":
            continue
        output_format = str(contract.get("output_format", "")).strip().lower()
        if output_format not in adapter.output_formats:
            unsupported_formats.add(retrieval_type)
            continue
        candidates.setdefault(retrieval_type, []).append(
            _ProductCandidate(
                item_key=str(row.get("item_key", "")),
                state=str(row.get("state", "")).strip().lower(),
                active_version_id=(
                    None
                    if row.get("active_version_id") in (None, "")
                    else str(row["active_version_id"])
                ),
                updated_at=row.get("updated_at"),
                last_error=(
                    None
                    if row.get("last_error") in (None, "")
                    else str(row["last_error"])
                ),
                contract=contract,
            )
        )
    return candidates, unsupported_formats


def _status_from_product_state(state: str) -> str:
    normalized = str(state).strip().lower()
    if normalized in {"absent", "unavailable", "negative", "not_available"}:
        return "unavailable"
    if normalized in {"pending", "running", "queued", "in_progress"}:
        return "pending"
    if normalized in {"failed", "failure", "error", "refresh_failed"}:
        return "failed"
    if normalized in {"corrupt", "damaged"}:
        return "corrupt"
    if normalized in {"not_downloaded", "missing"}:
        return "not_downloaded"
    if normalized == "unsupported":
        return "unsupported"
    return "unknown"


def _candidate_unready_payload(
    candidate: _ProductCandidate,
    *,
    release: str,
    source_id: str,
    adapter: SpectrumAdapter,
) -> dict[str, Any]:
    status = _status_from_product_state(candidate.state)
    error_messages = {
        "unavailable": "The Archive reported no product for this source.",
        "pending": "The spectrum product is still pending.",
        "failed": "The spectrum product could not be prepared.",
        "corrupt": "The spectrum product failed integrity checks.",
        "not_downloaded": "The spectrum product has not been downloaded.",
        "unsupported": "The stored spectrum product is not supported.",
        "unknown": "The stored spectrum product has an unknown state.",
    }
    return _empty_payload(
        status=status,
        release=release,
        source_id=source_id,
        scope="global",
        adapter=adapter,
        version_id=candidate.active_version_id,
        provenance={
            "selection": "active",
            "item_key": candidate.item_key or None,
            "corpus_id": None,
            "run_id": None,
        },
        error=error_messages[status],
    )


def _read_global_candidate(
    store: LibraryStore,
    candidate: _ProductCandidate,
    *,
    release: str,
    source_id: str,
    adapter: SpectrumAdapter,
) -> dict[str, Any]:
    provenance = {
        "selection": "active",
        "item_key": candidate.item_key or None,
        "corpus_id": None,
        "run_id": None,
    }
    if candidate.state != "ready" or candidate.active_version_id is None:
        return _candidate_unready_payload(
            candidate,
            release=release,
            source_id=source_id,
            adapter=adapter,
        )

    try:
        version = store.get_product(candidate.item_key, verify=True)
    except (BlobIntegrityError, FileNotFoundError, OSError, KeyError, ValueError):
        return _empty_payload(
            status="corrupt",
            release=release,
            source_id=source_id,
            scope="global",
            adapter=adapter,
            version_id=candidate.active_version_id,
            provenance=provenance,
            error="The active spectrum artifact failed integrity verification.",
        )
    except Exception:
        return _empty_payload(
            status="failed",
            release=release,
            source_id=source_id,
            scope="global",
            adapter=adapter,
            version_id=candidate.active_version_id,
            provenance=provenance,
            error="The active spectrum product could not be read.",
        )
    if not isinstance(version, Mapping):
        return _empty_payload(
            status="corrupt",
            release=release,
            source_id=source_id,
            scope="global",
            adapter=adapter,
            version_id=candidate.active_version_id,
            provenance=provenance,
            error="The active product pointer does not resolve to a version.",
        )

    version_id = str(version.get("version_id") or candidate.active_version_id)
    try:
        if normalize_identifier(version.get("identifier")) != source_id:
            raise ValueError("source mismatch")
        version_adapter = _contract_adapter(dict(version.get("contract") or {}), release)
        if version_adapter is None or version_adapter.retrieval_type != adapter.retrieval_type:
            raise ValueError("contract mismatch")
        artifact = _normalized_artifact(version)
        blob = artifact.get("blob")
        if not isinstance(blob, Mapping):
            raise ValueError("missing blob metadata")
        artifact_sha256 = str(blob.get("sha256") or "").lower()
        if len(artifact_sha256) != 64:
            raise ValueError("invalid blob hash")
        path = Path(store.blobs.verify(dict(blob)))
    except (BlobIntegrityError, FileNotFoundError, OSError, KeyError, TypeError, ValueError):
        return _empty_payload(
            status="corrupt",
            release=release,
            source_id=source_id,
            scope="global",
            adapter=adapter,
            version_id=version_id,
            provenance=provenance,
            error="The active spectrum artifact failed integrity verification.",
        )

    try:
        parsed, cache_hit = _parse_verified_artifact(
            adapter,
            path,
            release=release,
            source_id=source_id,
            artifact_sha256=artifact_sha256,
            version_id=version_id,
        )
    except (ValueError, TypeError, KeyError, IndexError):
        return _empty_payload(
            status="corrupt",
            release=release,
            source_id=source_id,
            scope="global",
            adapter=adapter,
            version_id=version_id,
            artifact_sha256=artifact_sha256,
            provenance=provenance,
            error="The verified spectrum artifact could not be parsed.",
        )
    except Exception:
        return _empty_payload(
            status="failed",
            release=release,
            source_id=source_id,
            scope="global",
            adapter=adapter,
            version_id=version_id,
            artifact_sha256=artifact_sha256,
            provenance=provenance,
            error="The registered spectrum parser could not run.",
        )

    provenance = {
        **provenance,
        "parse_cache": "hit" if cache_hit else "miss",
    }
    refresh_warning = (
        "The latest refresh failed; serving the last verified active version."
        if candidate.last_error
        else None
    )
    return _ready_payload(
        release=release,
        source_id=source_id,
        scope="global",
        adapter=adapter,
        version_id=version_id,
        artifact_sha256=artifact_sha256,
        parsed=parsed,
        provenance=provenance,
        warning=refresh_warning,
    )


def _negative_global_payload(
    store: LibraryStore,
    *,
    release: str,
    source_id: str,
    adapter: SpectrumAdapter,
) -> dict[str, Any] | None:
    """Find a current explicit-negative cache observation without writing state."""
    try:
        rows = store.read(
            "SELECT contract_json FROM product_contracts ORDER BY contract_hash"
        )
    except Exception:
        return None
    for row in rows:
        value = row.get("contract_json")
        try:
            contract = json.loads(value) if isinstance(value, str) else dict(value or {})
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        matched = _contract_adapter(contract, release)
        if matched is None or matched.retrieval_type != adapter.retrieval_type:
            continue
        item_key = product_item_key(contract, source_id)
        try:
            observation = store.get_cache("product", item_key, include_stale=True)
        except Exception:
            continue
        if observation and str(observation.get("outcome", "")).lower() == "negative":
            warning = (
                "The cached Archive-unavailable observation is stale."
                if observation.get("stale")
                else None
            )
            return _empty_payload(
                status="unavailable",
                release=release,
                source_id=source_id,
                scope="global",
                adapter=adapter,
                provenance={
                    "selection": "active",
                    "item_key": item_key,
                    "corpus_id": None,
                    "run_id": None,
                    "availability_cache": "stale" if observation.get("stale") else "current",
                },
                warning=warning,
                error="The Archive reported no product for this source.",
            )
    return None


def _fallback_warning(
    skipped: Sequence[tuple[str, str]], selected: str
) -> str | None:
    if not skipped:
        return None
    labels = ", ".join(f"{name} ({status})" for name, status in skipped)
    return f"Using {selected} as a fallback because {labels} was not ready."


def _get_global_spectrum(
    store: LibraryStore | None,
    *,
    release: str,
    source_id: str,
    adapters: Sequence[SpectrumAdapter],
    requested_product: str | None,
) -> dict[str, Any]:
    if store is None or (hasattr(store, "exists") and not store.exists):
        return _empty_payload(
            status="unknown",
            release=release,
            source_id=source_id,
            scope="global",
            adapter=adapters[0] if requested_product else None,
            product_type=requested_product,
            provenance={
                "selection": "active",
                "item_key": None,
                "corpus_id": None,
                "run_id": None,
            },
            error="The local library index is unavailable.",
        )

    try:
        by_product, unsupported_formats = _global_candidates(store, release, source_id)
    except Exception:
        return _empty_payload(
            status="failed",
            release=release,
            source_id=source_id,
            scope="global",
            adapter=adapters[0] if requested_product else None,
            product_type=requested_product,
            provenance={
                "selection": "active",
                "item_key": None,
                "corpus_id": None,
                "run_id": None,
            },
            error="The local product index could not be read.",
        )

    skipped: list[tuple[str, str]] = []
    failures: list[dict[str, Any]] = []
    for adapter in adapters:
        candidates = by_product.get(adapter.retrieval_type, [])
        if not candidates:
            if adapter.retrieval_type in unsupported_formats:
                unsupported = _empty_payload(
                    status="unsupported",
                    release=release,
                    source_id=source_id,
                    scope="global",
                    adapter=adapter,
                    provenance={
                        "selection": "active",
                        "item_key": None,
                        "corpus_id": None,
                        "run_id": None,
                    },
                    error="The stored serialization is not supported by this parser.",
                )
                failures.append(unsupported)
                skipped.append((adapter.retrieval_type, "unsupported"))
                continue
            negative = _negative_global_payload(
                store,
                release=release,
                source_id=source_id,
                adapter=adapter,
            )
            if negative is not None:
                failures.append(negative)
                skipped.append((adapter.retrieval_type, "unavailable"))
            else:
                skipped.append((adapter.retrieval_type, "not_downloaded"))
            continue

        product_failures: list[dict[str, Any]] = []
        for candidate in candidates:
            outcome = _read_global_candidate(
                store,
                candidate,
                release=release,
                source_id=source_id,
                adapter=adapter,
            )
            if outcome["status"] == "ready":
                outcome["warning"] = _join_warnings(
                    _fallback_warning(skipped, adapter.retrieval_type),
                    outcome.get("warning"),
                )
                if skipped:
                    outcome["provenance"]["fallback_from"] = [
                        name for name, _ in skipped
                    ]
                return outcome
            product_failures.append(outcome)
        chosen = product_failures[0]
        failures.append(chosen)
        skipped.append((adapter.retrieval_type, chosen["status"]))

    if failures:
        return failures[0]
    return _empty_payload(
        status="not_downloaded",
        release=release,
        source_id=source_id,
        scope="global",
        adapter=adapters[0] if requested_product else None,
        product_type=requested_product,
        provenance={
            "selection": "active",
            "item_key": None,
            "corpus_id": None,
            "run_id": None,
        },
        error="No supported spectrum product has been downloaded for this source.",
    )


def _select_corpus_run(
    store: LibraryStore | None,
    *,
    corpus_id: str,
    run_id: str | None,
) -> _CorpusSelection | None:
    if store is None or (hasattr(store, "exists") and not store.exists):
        return None
    corpora = store.read(
        "SELECT corpus_id, canonical_path FROM corpora WHERE corpus_id = ?",
        (corpus_id,),
    )
    if len(corpora) != 1:
        return None
    corpus_path = Path(str(corpora[0]["canonical_path"])).expanduser().resolve()

    rows = store.read(
        "SELECT run_id, status, started_at, finished_at, metadata_json FROM runs "
        "WHERE corpus_id = ?",
        (corpus_id,),
    )
    if run_id is not None:
        matching = [row for row in rows if str(row.get("run_id")) == run_id]
        if len(matching) != 1:
            return None
        selected = matching[0]
        directory = (corpus_path / "runs" / run_id).resolve()
    else:
        published: list[dict[str, Any]] = []
        for row in rows:
            try:
                metadata_value = row.get("metadata_json")
                metadata = (
                    json.loads(metadata_value)
                    if isinstance(metadata_value, str)
                    else dict(metadata_value or {})
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not (
                metadata.get("members_indexed") is True
                and metadata.get("published") is True
            ):
                continue
            published.append(row)
        if not published:
            return None

        # A published corpus-root manifest is the authoritative current-view
        # pointer.  Database timestamps are only a fallback for registered
        # legacy corpora that have no such pin.
        effective_directory = active_corpus_dir(corpus_path).resolve()
        pinned_run_id: str | None = None
        if effective_directory != corpus_path:
            run_root = (corpus_path / "runs").resolve()
            if effective_directory.parent != run_root:
                return None
            pinned_run_id = effective_directory.name
        else:
            manifest_path = corpus_path / "manifest.json"
            if manifest_path.is_file():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    return None
                if not isinstance(manifest, dict):
                    return None
                snapshot_run_id = manifest.get("snapshot_run_id")
                if snapshot_run_id not in (None, ""):
                    pinned_run_id = str(snapshot_run_id)

        if pinned_run_id is not None:
            matching = [
                row
                for row in published
                if str(row.get("run_id")) == pinned_run_id
            ]
            if len(matching) != 1:
                return None
            selected = matching[0]
            directory = effective_directory
        else:
            selected = max(
                published,
                key=lambda row: (
                    float(row.get("finished_at") or row.get("started_at") or 0.0),
                    float(row.get("started_at") or 0.0),
                    str(row.get("run_id") or ""),
                ),
            )
            directory = (
                corpus_path / "runs" / str(selected.get("run_id") or "")
            ).resolve()
    selected_run_id = str(selected.get("run_id") or "")
    if not selected_run_id:
        return None
    run_root = (corpus_path / "runs").resolve()
    if directory != corpus_path and directory.parent != run_root:
        return None
    if not directory.is_dir():
        return None
    return _CorpusSelection(
        corpus_id=corpus_id,
        run_id=selected_run_id,
        directory=directory,
        explicit_run=run_id is not None,
    )


def _document_contract(document: Mapping[str, Any]) -> dict[str, Any]:
    value = document.get("contract")
    return dict(value) if isinstance(value, Mapping) else {}


def _document_for_adapter(
    documents: Sequence[Mapping[str, Any]],
    adapter: SpectrumAdapter,
) -> tuple[Mapping[str, Any] | None, bool]:
    matching_type: list[Mapping[str, Any]] = []
    matching_supported: list[Mapping[str, Any]] = []
    for document in documents:
        contract = _document_contract(document)
        try:
            release = normalize_gaia_release(str(contract.get("release", "")))
        except ValueError:
            continue
        retrieval_type = str(contract.get("retrieval_type", "")).strip().upper()
        if release != adapter.release or retrieval_type != adapter.retrieval_type:
            continue
        if str(contract.get("linking_parameter", "")).strip().upper() != "SOURCE_ID":
            continue
        matching_type.append(document)
        if str(contract.get("output_format", "")).strip().lower() in adapter.output_formats:
            matching_supported.append(document)
    if len(matching_supported) > 1:
        raise ValueError("corpus references contain duplicate spectrum product documents")
    if matching_supported:
        return matching_supported[0], False
    return None, bool(matching_type)


def _document_item(
    document: Mapping[str, Any], source_id: str
) -> Mapping[str, Any] | None:
    items = document.get("items")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
        raise ValueError("corpus product references have no item list")
    matching: list[Mapping[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("corpus product item must be an object")
        try:
            identifier = normalize_identifier(item.get("identifier"))
        except (TypeError, ValueError):
            continue
        if identifier == source_id:
            matching.append(item)
    if len(matching) > 1:
        raise ValueError("corpus product references duplicate the source")
    return matching[0] if matching else None


def _corpus_item_unready_payload(
    item: Mapping[str, Any] | None,
    *,
    selection: _CorpusSelection,
    release: str,
    source_id: str,
    adapter: SpectrumAdapter,
) -> dict[str, Any]:
    status = "not_downloaded" if item is None else _status_from_product_state(
        str(item.get("status", ""))
    )
    version_id = None if item is None or item.get("version_id") in (None, "") else str(
        item.get("version_id")
    )
    item_key = None if item is None or item.get("item_key") in (None, "") else str(
        item.get("item_key")
    )
    errors = {
        "unavailable": "The pinned corpus product is unavailable for this source.",
        "pending": "The pinned corpus product is still pending.",
        "failed": "The pinned corpus product is marked failed.",
        "corrupt": "The pinned corpus product is marked corrupt.",
        "unsupported": "The pinned corpus product is unsupported.",
        "unknown": "The pinned corpus product has an unknown state.",
        "not_downloaded": "The corpus run does not pin this spectrum product.",
    }
    return _empty_payload(
        status=status,
        release=release,
        source_id=source_id,
        scope="corpus",
        adapter=adapter,
        version_id=version_id,
        provenance={
            "selection": "pinned",
            "item_key": item_key,
            "corpus_id": selection.corpus_id,
            "run_id": selection.run_id,
            "explicit_run": selection.explicit_run,
        },
        error=errors[status],
    )


def _read_corpus_adapter(
    selection: _CorpusSelection,
    documents: Sequence[Mapping[str, Any]],
    *,
    release: str,
    source_id: str,
    adapter: SpectrumAdapter,
) -> dict[str, Any]:
    provenance = {
        "selection": "pinned",
        "item_key": None,
        "corpus_id": selection.corpus_id,
        "run_id": selection.run_id,
        "explicit_run": selection.explicit_run,
    }
    try:
        document, unsupported_format = _document_for_adapter(documents, adapter)
    except ValueError:
        return _empty_payload(
            status="corrupt",
            release=release,
            source_id=source_id,
            scope="corpus",
            adapter=adapter,
            provenance=provenance,
            error="The corpus product reference document is malformed.",
        )
    if unsupported_format:
        return _empty_payload(
            status="unsupported",
            release=release,
            source_id=source_id,
            scope="corpus",
            adapter=adapter,
            provenance=provenance,
            error="The pinned spectrum serialization is not supported by this parser.",
        )

    item: Mapping[str, Any] | None = None
    if document is not None:
        try:
            item = _document_item(document, source_id)
        except ValueError:
            return _empty_payload(
                status="corrupt",
                release=release,
                source_id=source_id,
                scope="corpus",
                adapter=adapter,
                provenance=provenance,
                error="The corpus product reference document is malformed.",
            )
        if item is None or str(item.get("status", "")).strip().lower() != "ready":
            return _corpus_item_unready_payload(
                item,
                selection=selection,
                release=release,
                source_id=source_id,
                adapter=adapter,
            )

    try:
        resolved = resolve_product(
            selection.directory,
            source_id,
            adapter.retrieval_type,
            release=release,
        )
    except ProductReferenceError:
        if document is not None:
            version_id = None if item is None else item.get("version_id")
            return _empty_payload(
                status="corrupt",
                release=release,
                source_id=source_id,
                scope="corpus",
                adapter=adapter,
                version_id=None if version_id in (None, "") else str(version_id),
                provenance=provenance,
                error="The pinned spectrum reference could not be resolved.",
            )
        return _corpus_item_unready_payload(
            item,
            selection=selection,
            release=release,
            source_id=source_id,
            adapter=adapter,
        )
    except (BlobIntegrityError, FileNotFoundError, OSError, KeyError, TypeError, ValueError):
        version_id = None if item is None else item.get("version_id")
        return _empty_payload(
            status="corrupt",
            release=release,
            source_id=source_id,
            scope="corpus",
            adapter=adapter,
            version_id=None if version_id in (None, "") else str(version_id),
            provenance=provenance,
            error="The pinned spectrum artifact failed integrity verification.",
        )

    version_id = (
        None if resolved.get("version_id") in (None, "") else str(resolved["version_id"])
    )
    artifact_sha256 = str(resolved.get("sha256") or "").lower()
    provenance = {
        **provenance,
        "item_key": (
            None if resolved.get("item_key") in (None, "") else str(resolved["item_key"])
        ),
        "storage_kind": str(resolved.get("storage_kind") or ""),
    }
    if len(artifact_sha256) != 64:
        return _empty_payload(
            status="corrupt",
            release=release,
            source_id=source_id,
            scope="corpus",
            adapter=adapter,
            version_id=version_id,
            provenance=provenance,
            error="The pinned spectrum artifact has invalid integrity metadata.",
        )
    try:
        parsed, cache_hit = _parse_verified_artifact(
            adapter,
            Path(resolved["path"]),
            release=release,
            source_id=source_id,
            artifact_sha256=artifact_sha256,
            version_id=version_id,
        )
    except (ValueError, TypeError, KeyError, IndexError):
        return _empty_payload(
            status="corrupt",
            release=release,
            source_id=source_id,
            scope="corpus",
            adapter=adapter,
            version_id=version_id,
            artifact_sha256=artifact_sha256,
            provenance=provenance,
            error="The verified pinned spectrum artifact could not be parsed.",
        )
    except Exception:
        return _empty_payload(
            status="failed",
            release=release,
            source_id=source_id,
            scope="corpus",
            adapter=adapter,
            version_id=version_id,
            artifact_sha256=artifact_sha256,
            provenance=provenance,
            error="The registered spectrum parser could not run.",
        )
    provenance["parse_cache"] = "hit" if cache_hit else "miss"
    return _ready_payload(
        release=release,
        source_id=source_id,
        scope="corpus",
        adapter=adapter,
        version_id=version_id,
        artifact_sha256=artifact_sha256,
        parsed=parsed,
        provenance=provenance,
        warning=None,
    )


def _get_corpus_spectrum(
    store: LibraryStore | None,
    *,
    release: str,
    source_id: str,
    corpus_id: str,
    run_id: str | None,
    adapters: Sequence[SpectrumAdapter],
    requested_product: str | None,
) -> dict[str, Any]:
    try:
        selection = _select_corpus_run(
            store,
            corpus_id=corpus_id,
            run_id=run_id,
        )
    except Exception:
        selection = None
    if selection is None:
        return _empty_payload(
            status="unknown",
            release=release,
            source_id=source_id,
            scope="corpus",
            adapter=adapters[0] if requested_product else None,
            product_type=requested_product,
            provenance={
                "selection": "pinned",
                "item_key": None,
                "corpus_id": corpus_id,
                "run_id": run_id,
                "explicit_run": run_id is not None,
            },
            error="The requested registered corpus run could not be resolved.",
        )
    try:
        documents = read_reference_documents(selection.directory)
    except (BlobIntegrityError, ProductReferenceError, OSError, ValueError):
        return _empty_payload(
            status="corrupt",
            release=release,
            source_id=source_id,
            scope="corpus",
            adapter=adapters[0] if requested_product else None,
            product_type=requested_product,
            provenance={
                "selection": "pinned",
                "item_key": None,
                "corpus_id": corpus_id,
                "run_id": selection.run_id,
                "explicit_run": selection.explicit_run,
            },
            error="The corpus product reference document is malformed.",
        )

    skipped: list[tuple[str, str]] = []
    failures: list[dict[str, Any]] = []
    for adapter in adapters:
        outcome = _read_corpus_adapter(
            selection,
            documents,
            release=release,
            source_id=source_id,
            adapter=adapter,
        )
        if outcome["status"] == "ready":
            outcome["warning"] = _join_warnings(
                _fallback_warning(skipped, adapter.retrieval_type),
                outcome.get("warning"),
            )
            if skipped:
                outcome["provenance"]["fallback_from"] = [
                    name for name, _ in skipped
                ]
            return outcome
        failures.append(outcome)
        skipped.append((adapter.retrieval_type, outcome["status"]))
    if failures:
        return failures[0]
    return _empty_payload(
        status="not_downloaded",
        release=release,
        source_id=source_id,
        scope="corpus",
        product_type=requested_product,
        provenance={
            "selection": "pinned",
            "item_key": None,
            "corpus_id": corpus_id,
            "run_id": selection.run_id,
            "explicit_run": selection.explicit_run,
        },
        error="The corpus run does not pin a supported spectrum product.",
    )


def get_library_spectrum(
    store: LibraryStore | None,
    release: str,
    source_id: Any,
    *,
    scope: str = "global",
    corpus_id: str | None = None,
    run_id: str | None = None,
    retrieval_type: str | None = None,
) -> dict[str, Any]:
    """Return one verified, parsed library spectrum as a JSON-safe object.

    Valid data outcomes are represented by ``status`` rather than exceptions.
    ``ValueError`` is reserved for invalid request arguments so an HTTP adapter
    can map those to a client error.  No call path performs a download or a
    SQLite write.
    """
    release, source_id = source_identity(release, source_id)
    normalized_scope = str(scope).strip().lower()
    if normalized_scope not in {"global", "corpus"}:
        raise ValueError("scope must be 'global' or 'corpus'")
    if normalized_scope == "global":
        if corpus_id is not None or run_id is not None:
            raise ValueError("corpus_id/run_id require scope='corpus'")
        normalized_corpus_id = None
        normalized_run_id = None
    else:
        if corpus_id is None or not str(corpus_id).strip():
            raise ValueError("corpus_id is required for corpus scope")
        normalized_corpus_id = str(corpus_id).strip()
        normalized_run_id = None
        if run_id is not None:
            normalized_run_id = str(run_id).strip()
            if not normalized_run_id:
                raise ValueError("run_id must not be blank")

    requested_product: str | None = None
    if retrieval_type is not None:
        requested_product = str(retrieval_type).strip().upper()
        if not requested_product:
            raise ValueError("retrieval_type must not be blank")
        adapter = SPECTRUM_ADAPTERS.get((release, requested_product))
        if adapter is None:
            return _empty_payload(
                status="unsupported",
                release=release,
                source_id=source_id,
                scope=normalized_scope,
                product_type=requested_product,
                provenance={
                    "selection": "active" if normalized_scope == "global" else "pinned",
                    "item_key": None,
                    "corpus_id": normalized_corpus_id,
                    "run_id": normalized_run_id,
                },
                error=(
                    f"No parser is registered for {release.upper()} "
                    f"{requested_product}."
                ),
            )
        adapters = (adapter,)
    else:
        adapters = tuple(
            SPECTRUM_ADAPTERS[(release, product)]
            for product in _SPECTRUM_PREFERENCE.get(release, ())
        )
        if not adapters:
            return _empty_payload(
                status="unsupported",
                release=release,
                source_id=source_id,
                scope=normalized_scope,
                provenance={
                    "selection": "active" if normalized_scope == "global" else "pinned",
                    "item_key": None,
                    "corpus_id": normalized_corpus_id,
                    "run_id": normalized_run_id,
                },
                error=f"No spectrum parser is registered for {release.upper()}.",
            )

    if normalized_scope == "global":
        return _get_global_spectrum(
            store,
            release=release,
            source_id=source_id,
            adapters=adapters,
            requested_product=requested_product,
        )

    return _get_corpus_spectrum(
        store,
        release=release,
        source_id=source_id,
        corpus_id=normalized_corpus_id,
        run_id=normalized_run_id,
        adapters=adapters,
        requested_product=requested_product,
    )


__all__ = [
    "DR3_XP_FLUX_UNIT",
    "SPECTRUM_ADAPTERS",
    "SPECTRUM_STATUSES",
    "SpectrumAdapter",
    "get_library_spectrum",
    "normalize_spectrum_payload",
]
