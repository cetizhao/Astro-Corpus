from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd


@dataclass
class CatalogResult:
    source_name: str
    data: pd.DataFrame
    row_count: int
    query_description: str = ""
    output_path: Optional[Path] = None


@dataclass
class SpectrumResult:
    source_id: str
    path: Path
    success: bool
    error: Optional[str] = None
