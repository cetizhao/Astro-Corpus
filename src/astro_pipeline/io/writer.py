from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pandas as pd
from astropy.table import Table


def save_catalog_parquet(df: pd.DataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, engine="pyarrow", index=False)
    return path


def save_catalog_fits(df: pd.DataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = Table.from_pandas(df)
    table.write(str(path), overwrite=True)
    return path


class DownloadCheckpoint:
    def __init__(self, path: Path, source_name: str = "") -> None:
        self.path = path
        self.source_name = source_name
        self.completed: set[str] = set()
        self.failed: set[str] = set()
        if path.exists():
            self._load()

    def _load(self) -> None:
        data = json.loads(self.path.read_text())
        self.source_name = data.get("source_name", self.source_name)
        self.completed = set(data.get("completed", []))
        self.failed = set(data.get("failed", []))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "source_name": self.source_name,
            "completed": sorted(self.completed),
            "failed": sorted(self.failed),
        }
        self.path.write_text(json.dumps(data, indent=2))

    def mark_completed(self, identifier: str) -> None:
        self.completed.add(identifier)
        self.failed.discard(identifier)

    def mark_failed(self, identifier: str) -> None:
        self.failed.add(identifier)

    def is_completed(self, identifier: str) -> bool:
        return identifier in self.completed
