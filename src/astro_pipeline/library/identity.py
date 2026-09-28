"""Stable identities and JSON values used by the local library."""
from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

from astro_pipeline.gaia_releases import normalize_gaia_release

NORMALIZER_VERSION = "1"


def json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [json_value(item) for item in sorted(value, key=str)]
    if type(value).__name__ in {"NAType", "NaTType", "MaskedConstant"}:
        return None
    if hasattr(value, "tolist"):
        return json_value(value.tolist())
    if hasattr(value, "item"):
        return json_value(value.item())
    raise TypeError(f"Unsupported library JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        json_value(value), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    )


def identity_hash(value: Any, *, namespace: str = "") -> str:
    return hashlib.sha256((namespace + "\0" + canonical_json(value)).encode()).hexdigest()


def normalize_identifier(value: Any) -> str:
    if isinstance(value, (float, bool)):
        raise ValueError("Identifiers must be integer strings, never floating-point values")
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]+", text):
        raise ValueError("Identifiers must contain ASCII digits only")
    return text.lstrip("0") or "0"


def source_identity(release: str, source_id: Any) -> tuple[str, str]:
    return normalize_gaia_release(release), normalize_identifier(source_id)


def source_cache_key(release: str, source_id: Any) -> str:
    return identity_hash(source_identity(release, source_id), namespace="gaia-source")


def product_contract_hash(contract: dict) -> str:
    return identity_hash(contract, namespace="gaia-product-contract")


def product_item_key(
    contract: dict, identifier: Any, *, normalizer_version: str = NORMALIZER_VERSION,
) -> str:
    return identity_hash(
        [product_contract_hash(contract), normalize_identifier(identifier), normalizer_version],
        namespace="gaia-product-item",
    )
