from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ConeSearchQuery:
    ra: float
    dec: float
    radius_deg: float
    extra_conditions: dict[str, float] = field(default_factory=dict)
    limit: Optional[int] = None


@dataclass(frozen=True)
class ParameterQuery:
    conditions: dict[str, tuple[str, float]] = field(default_factory=dict)
    limit: Optional[int] = None
    order_by: Optional[str] = None


@dataclass(frozen=True)
class SourceIdQuery:
    source_ids: list[str]
    limit: Optional[int] = None
