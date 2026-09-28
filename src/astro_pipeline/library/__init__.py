"""Local, release-qualified library; importing this package performs no I/O."""
from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astro_pipeline.config import PipelineConfig
    from astro_pipeline.library.store import LibraryStore


def library_root(config: PipelineConfig | None = None) -> Path:
    if config is None:
        from astro_pipeline.config import PipelineConfig
        config = PipelineConfig.load()
    override = os.environ.get("ASTRO_PIPELINE_LIBRARY_DIR")
    return Path(override or config.library.root or config.output.base_dir / "_library").expanduser().resolve()


def get_library(
    config: PipelineConfig | None = None, *, create: bool = False,
) -> LibraryStore | None:
    if config is None:
        from astro_pipeline.config import PipelineConfig
        config = PipelineConfig.load()
    if not config.library.enabled:
        return None
    from astro_pipeline.library.store import LibraryStore
    store = LibraryStore(library_root(config), busy_timeout_ms=config.library.busy_timeout_ms)
    for name in (
        "lease_seconds", "wait_timeout_seconds", "positive_ttl_seconds", "negative_ttl_seconds",
    ):
        setattr(store, name, getattr(config.library, name))
    if create:
        store.initialize()
    return store
