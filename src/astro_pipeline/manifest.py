"""Atomic, resumable manifests for corpus and bulk Gaia downloads."""
from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from astro_pipeline import __version__
from astro_pipeline.gaia_releases import (
    get_gaia_release_profile,
    normalize_gaia_release,
)

MANIFEST_SCHEMA_VERSION = "1.1"
SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_label(label: str) -> str:
    """Validate a corpus directory label (never a user-controlled path)."""
    label = label.strip()
    if not SAFE_LABEL.fullmatch(label):
        raise ValueError(
            "label must use only letters, digits, dot, underscore or hyphen and "
            "must not contain a path separator"
        )
    return label


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
        gaia_release: str = "dr3",
    ) -> "CorpusManifest":
        path = Path(path)
        label = validate_label(label)
        gaia_release = normalize_gaia_release(gaia_release)
        now = utc_now()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            previous = data.get("corpus", {})
            previous_release = normalize_gaia_release(
                previous.get("gaia_release", "dr3")
            )
            if (
                previous.get("label") not in (None, label)
                or previous.get("input_mode") not in (None, input_mode)
                or previous.get("input_summary") not in (None, input_summary)
                or previous_release != gaia_release
            ):
                raise ValueError(
                    f"Corpus label {label!r} already has a manifest for a different "
                    f"input ({previous.get('input_mode')}: "
                    f"{previous.get('input_summary')}). Choose a new label or remove "
                    "the old corpus directory after reviewing it."
                )
            previous["gaia_release"] = gaia_release
            stored_products = data.setdefault("requested_products", {})
            for product, requested in requested_products.items():
                # A corpus may add optional products on a later run, but an
                # already-requested product remains part of its durable scope.
                stored_products[product] = bool(stored_products.get(product)) or bool(
                    requested
                )
            data["status"] = "running"
            data["updated_at"] = now
            data.setdefault("runs", []).append(
                {
                    "started_at": now,
                    "status": "running",
                    "requested_products": dict(requested_products),
                    "gaia_release": gaia_release,
                }
            )
        else:
            data = {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "pipeline_version": __version__,
                "corpus": {
                    "label": label,
                    "input_mode": input_mode,
                    "input_summary": input_summary,
                    "gaia_release": gaia_release,
                },
                "status": "running",
                "created_at": now,
                "updated_at": now,
                "requested_products": dict(requested_products),
                "stages": {},
                "artifacts": {},
                "warnings": [],
                "errors": [],
                "runs": [
                    {
                        "started_at": now,
                        "status": "running",
                        "requested_products": dict(requested_products),
                        "gaia_release": gaia_release,
                    }
                ],
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
    gaia_release: str = "dr3",
) -> dict[str, Any]:
    """Return a read-only preflight plan suitable for an agent/user review."""
    label = validate_label(label)
    profile = get_gaia_release_profile(gaia_release)
    if (xp_sampled or xp_continuous) and not {
        "XP_SAMPLED",
        "XP_CONTINUOUS",
    }.issubset(set(profile.datalink_products)):
        raise ValueError(
            f"{profile.display_name} does not declare DR3-style XP products; "
            "use plan_gaia_product_download with a release-specific product"
        )
    n_sources = expected_sources if expected_sources and expected_sources > 0 else None
    stages: list[dict[str, Any]] = [
        {
            "stage": "resolve_members",
            "action": f"Resolve {profile.display_name} sources from the selected input",
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
    if profile.status != "stable":
        cautions.append(
            f"{profile.display_name} profile is provisional: ordinary corpus "
            "resolution is disabled until its schema and canonical column mapping are confirmed."
        )
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
        "gaia_release": profile.key,
        "gaia_archive_tag": profile.archive_tag,
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
        "executable": profile.status == "stable",
        "next_step": (
            "Call download_corpus or download_membership_corpus with matching options."
            if profile.status == "stable"
            else "Call probe_gaia_archive, describe_gaia_table, and list_gaia_products; do not run a DR4 corpus build yet."
        ),
    }
