from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


class PaperEdgeTable:
    """Many-to-many mapping between source IDs and arxiv paper IDs."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path
        if path and path.exists():
            self.df = pd.read_parquet(path)
            logger.info("Loaded %d paper edges from %s", len(self.df), path)
        else:
            self.df = pd.DataFrame(columns=["source_id", "arxiv_id"])

    def add_link(self, source_id: str, arxiv_id: str) -> None:
        self._ensure_normalized()
        if not ((self.df["source_id"] == source_id) & (self.df["arxiv_id"] == arxiv_id)).any():
            self.df = pd.concat(
                [self.df, pd.DataFrame([{"source_id": source_id, "arxiv_id": arxiv_id}])],
                ignore_index=True,
            )

    def add_links(self, links: list[tuple[str, str]]) -> None:
        for sid, aid in links:
            self.add_link(sid, aid)

    def add_links_batch(self, source_ids: list[str], arxiv_ids: list[str]) -> None:
        for sid in source_ids:
            for aid in arxiv_ids:
                self.add_link(sid, aid)

    def get_papers(self, source_id: str) -> list[str]:
        mask = self.df["source_id"] == source_id
        return sorted(self.df.loc[mask, "arxiv_id"].unique().tolist())

    def get_sources(self, arxiv_id: str) -> list[str]:
        mask = self.df["arxiv_id"] == arxiv_id
        return sorted(self.df.loc[mask, "source_id"].unique().tolist())

    def get_paper_map(self) -> dict[str, list[str]]:
        grouped = self.df.groupby("source_id")["arxiv_id"].apply(lambda x: sorted(x.unique()))
        return grouped.to_dict()

    def get_source_map(self) -> dict[str, list[str]]:
        grouped = self.df.groupby("arxiv_id")["source_id"].apply(lambda x: sorted(x.unique()))
        return grouped.to_dict()

    def remove_link(self, source_id: str, arxiv_id: str) -> None:
        mask = (self.df["source_id"] == source_id) & (self.df["arxiv_id"] == arxiv_id)
        self.df = self.df[~mask].reset_index(drop=True)

    def save(self, path: Optional[Path] = None) -> None:
        target = path or self.path
        if target is None:
            raise ValueError("No path specified for saving paper edges")
        target.parent.mkdir(parents=True, exist_ok=True)
        self.df.to_parquet(target, engine="pyarrow", index=False)
        logger.info("Saved %d edges to %s", len(self.df), target)
        self.path = target

    def __len__(self) -> int:
        return len(self.df)

    def _ensure_normalized(self) -> None:
        if "source_id" not in self.df.columns:
            self.df["source_id"] = ""
        if "arxiv_id" not in self.df.columns:
            self.df["arxiv_id"] = ""


def add_arxiv_ids_column(
    catalog_df: pd.DataFrame,
    paper_edges: PaperEdgeTable,
    id_col: str = "source_id",
) -> pd.DataFrame:
    df = catalog_df.copy()
    arxiv_map = paper_edges.get_paper_map()
    df["arxiv_ids"] = df[id_col].astype(str).apply(lambda sid: [a for a in arxiv_map.get(sid, []) if a])
    df["num_papers"] = df["arxiv_ids"].apply(len)
    return df


def add_paper_columns(
    catalog_df: pd.DataFrame,
    paper_edges: PaperEdgeTable,
    id_col: str = "source_id",
) -> pd.DataFrame:
    """Attach ``arxiv_ids``, ``bibcodes`` and ``num_papers`` to a catalog.

    ``bibcodes`` keeps every SIMBAD reference (journal-only papers with no
    arXiv id included), so no link is ever silently dropped. ``arxiv_ids``
    holds only the subset that resolves to an arXiv id.
    """
    df = catalog_df.copy()
    df["_sid"] = df[id_col].astype(str)
    edge_df = paper_edges.df.copy()
    if not edge_df.empty:
        edge_df["_sid"] = edge_df["source_id"].astype(str)
        arxiv_grp = (
            edge_df[edge_df["arxiv_id"].astype(str) != ""]
            .groupby("_sid")["arxiv_id"].apply(lambda s: sorted({str(x) for x in s}))
        )
        bib_grp = (
            edge_df[edge_df["bibcode"].astype(str) != ""]
            .groupby("_sid")["bibcode"].apply(lambda s: sorted({str(x) for x in s}))
        )
        df["arxiv_ids"] = df["_sid"].map(arxiv_grp).apply(lambda v: list(v) if isinstance(v, list) else [])
        df["bibcodes"] = df["_sid"].map(bib_grp).apply(lambda v: list(v) if isinstance(v, list) else [])
    else:
        df["arxiv_ids"] = [[] for _ in range(len(df))]
        df["bibcodes"] = [[] for _ in range(len(df))]
    df["num_papers"] = df["arxiv_ids"].apply(len)
    return df.drop(columns=["_sid"])
