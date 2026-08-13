"""Atomic, resumable manifests for corpus and bulk Gaia downloads."""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from astro_pipeline import __version__

MANIFEST_SCHEMA_VERSION = "1.0"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CorpusManifest:
    """Small JSON state machine written atomically after every build stage."""

    def __init__(self, path: Path, data: dict[str, Any]) -> None:
        self.path = path
        self.data = data

    @classmethod
    def open_or_create(
        cls,
        path: Path | str,
        *,
        label: str,
        input_mode: str,
        input_summary: str,
        requested_products: dict[str, bool],
    ) -> "CorpusManifest":
        path = Path(path)
        now = utc_now()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            previous = data.get("corpus", {})
            if (
                previous.get("input_mode") not in (None, input_mode)
                or previous.get("input_summary") not in (None, input_summary)
            ):
                raise ValueError(
                    f"Corpus label {label!r} already has a manifest for a different "
                    f"input ({previous.get('input_mode')}: "
                    f"{previous.get('input_summary')}). Choose a new label or remove "
                    "the old corpus directory after reviewing it."
                )
            data.setdefault("requested_products", {}).update(requested_products)
            data["status"] = "running"
            data["updated_at"] = now
            data.setdefault("runs", []).append({"started_at": now, "status": "running"})
        else:
            data = {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "pipeline_version": __version__,
                "corpus": {
                    "label": label,
                    "input_mode": input_mode,
                    "input_summary": input_summary,
                },
                "status": "running",
                "created_at": now,
                "updated_at": now,
                "requested_products": dict(requested_products),
                "stages": {},
                "artifacts": {},
                "warnings": [],
                "errors": [],
                "runs": [{"started_at": now, "status": "running"}],
            }
        manifest = cls(path, data)
        manifest.save()
        return manifest

    def update_stage(self, name: str, **updates: Any) -> None:
        stage = self.data.setdefault("stages", {}).setdefault(name, {})
        status = updates.get("status")
        if status == "running" and "started_at" not in stage:
            stage["started_at"] = utc_now()
        stage.update({key: value for key, value in updates.items() if value is not None})
        if status in {"completed", "partial", "failed", "skipped", "not_requested"}:
            stage["finished_at"] = utc_now()
        self.save()

    def add_artifact(self, name: str, path: Path | str, **metadata: Any) -> None:
        artifact = {"path": str(Path(path).resolve()), **metadata}
        self.data.setdefault("artifacts", {})[name] = artifact
        self.save()

    def add_warning(self, message: str) -> None:
        warnings = self.data.setdefault("warnings", [])
        if message not in warnings:
            warnings.append(message)
        self.save()

    def add_error(self, message: str) -> None:
        errors = self.data.setdefault("errors", [])
        if message not in errors:
            errors.append(message)
        self.save()

    def finish(self, status: str) -> None:
        if status not in {"completed", "partial", "failed"}:
            raise ValueError(f"Invalid terminal manifest status: {status}")
        now = utc_now()
        self.data["status"] = status
        self.data["updated_at"] = now
        runs = self.data.setdefault("runs", [])
        if runs:
            runs[-1].update({"finished_at": now, "status": status})
        self.save()

    def snapshot(self) -> dict[str, Any]:
        return deepcopy(self.data)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data["updated_at"] = utc_now()
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)


def read_manifest(path: Path | str) -> Optional[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def make_download_plan(
    *,
    label: str,
    input_mode: str,
    input_summary: str,
    expected_sources: Optional[int],
    skip_simbad: bool,
    xp_sampled: bool,
    xp_continuous: bool,
) -> dict[str, Any]:
    """Return a read-only preflight plan suitable for an agent/user review."""
    n_sources = expected_sources if expected_sources and expected_sources > 0 else None
    stages: list[dict[str, Any]] = [
        {
            "stage": "resolve_members",
            "action": "Resolve Gaia DR3 sources from the selected input",
            "network": True,
            "resumable": False,
        },
        {
            "stage": "save_members",
            "action": "Write members.csv and members.parquet",
            "network": False,
            "resumable": True,
        },
    ]
    if xp_sampled or xp_continuous:
        stages.append(
            {
                "stage": "gaia_xp_availability",
                "action": "Query has_xp_sampled/has_xp_continuous for member IDs",
                "network": True,
                "resumable": True,
            }
        )
    if xp_sampled:
        stages.append(
            {
                "stage": "gaia_xp_sampled",
                "action": "Download one Gaia XP_SAMPLED FITS per available source",
                "network": True,
                "resumable": True,
            }
        )
    if xp_continuous:
        stages.append(
            {
                "stage": "gaia_xp_continuous",
                "action": "Download one Gaia XP_CONTINUOUS FITS per available source",
                "network": True,
                "resumable": True,
            }
        )
    stages.append(
        {
            "stage": "simbad_papers",
            "action": "Skip paper lookup" if skip_simbad else "Build SIMBAD source-paper edges",
            "network": not skip_simbad,
            "resumable": True,
            "enabled": not skip_simbad,
        }
    )
    stages.append(
        {
            "stage": "visualization",
            "action": "Skip HTML" if skip_simbad else "Build self-contained corpus HTML",
            "network": False,
            "resumable": True,
            "enabled": not skip_simbad,
        }
    )
    cautions = [
        "A named region/cone returns geometric field sources, not physical members.",
        "Gaia and CDS/SIMBAD services can throttle or queue requests; checkpointed stages can be rerun.",
    ]
    if xp_sampled or xp_continuous:
        cautions.append(
            "Gaia DataLink may require credentials and can be intermittently unavailable; astrometry remains usable if XP is partial."
        )
    if not skip_simbad and n_sources:
        cautions.append(
            f"SIMBAD performs roughly one source lookup per member ({n_sources} expected) plus paper metadata requests."
        )
    return {
        "label": label,
        "input_mode": input_mode,
        "input_summary": input_summary,
        "expected_sources": n_sources,
        "output_dir": str((Path("data") / label).resolve()),
        "manifest": str((Path("data") / label / "manifest.json").resolve()),
        "requested_products": {
            "members": True,
            "simbad_papers": not skip_simbad,
            "visualization": not skip_simbad,
            "gaia_xp_sampled": xp_sampled,
            "gaia_xp_continuous": xp_continuous,
        },
        "stages": stages,
        "cautions": cautions,
        "writes_data": False,
        "next_step": "Call download_corpus or download_membership_corpus with matching options.",
    }
