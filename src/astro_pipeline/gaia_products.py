"""Release-aware Gaia DataLink request, artifact, and download primitives."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from astropy.table import Table
from astroquery.gaia import Gaia

from astro_pipeline.gaia_releases import get_gaia_release_profile
from astro_pipeline.io.writer import DownloadCheckpoint

ProgressCallback = Callable[[dict[str, Any]], None]
DataLinkLoader = Callable[..., dict[str, list[Any]]]
PRODUCT_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


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
        valid_linking = {str(value).upper() for value in Gaia.VALID_LINKING_PARAMETERS}
        if linking not in valid_linking:
            raise ValueError(
                f"linking_parameter must be one of {sorted(valid_linking)}; got {linking!r}"
            )
        if not identifiers:
            raise ValueError("at least one Gaia DataLink identifier is required")
        if any(not identifier.isdigit() for identifier in identifiers):
            raise ValueError(
                f"{linking} identifiers must contain digits only; ranges and job references "
                "are not accepted by this bounded downloader"
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
) -> list[GaiaArtifact]:
    """Download bounded DataLink batches and persist release-aware provenance.

    Astroquery returns parsed tables rather than the original HTTP byte stream.
    The ``archive_tables`` copies therefore preserve Archive table semantics and
    metadata but are explicitly marked as re-serialized, not byte-identical raw
    responses. Binary image payload support remains disabled until DR4 is live.
    """
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

    load_data = loader or Gaia.load_data
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
        "n_archive_tables": len(list((output_dir / "archive_tables").glob("*"))),
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
) -> list[GaiaArtifact]:
    artifacts: list[GaiaArtifact] = []
    extension = _extension(request.output_format)
    identifier_columns = _identifier_column_candidates(request.linking_parameter)
    table_counter = 0

    for key, values in response.items():
        for value in values:
            table = value if isinstance(value, Table) else Table(value)
            safe_key = _safe_file_stem(str(key) or "product")
            archive_path = archive_dir / (
                f"batch-{batch_index:05d}-{table_counter:03d}-{safe_key}.{extension}"
            )
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
    request: GaiaProductRequest, identifier: str, error: str
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
