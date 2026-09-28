"""Per-source XP availability observations, not sky-coverage inference."""
from __future__ import annotations

import time

import pandas as pd

from astro_pipeline.library.identity import normalize_identifier, source_cache_key

NAMESPACE = "gaia-xp-availability:v1"
FLAGS = ("has_xp_sampled", "has_xp_continuous")


def _flag(value) -> bool:
    return str(value).strip().lower() in {"true", "1"}


def cached_availability(gaia, identifiers, store, *, release="dr3", refresh=False):
    from astro_pipeline.library.leases import LeaseBatch
    ids = list(dict.fromkeys(normalize_identifier(value) for value in identifiers))
    keys = {source_id: source_cache_key(release, source_id) for source_id in ids}
    rows = {}
    cache_hits = set()

    def cached(source_id):
        observation = None if refresh else store.get_cache(NAMESPACE, keys[source_id])
        if observation is not None:
            rows[source_id] = observation["payload"]
            cache_hits.add(source_id)
            return True
        return False

    missing = [source_id for source_id in ids if not cached(source_id)]

    def fetch(selected, leases):
        pending = [source_id for source_id in selected if not cached(source_id)]
        if not pending:
            return
        result = gaia.query_xp_availability(pending)
        frame = result.data
        if "source_id" not in frame or any(flag not in frame for flag in FLAGS):
            raise RuntimeError("Gaia returned an incomplete XP availability schema")
        result_ids = frame["source_id"].astype(str).tolist()
        if len(result_ids) != len(set(result_ids)) or set(result_ids) != set(pending):
            raise RuntimeError("Gaia did not return one availability row per requested identifier")
        for raw in frame.to_dict(orient="records"):
            source_id = normalize_identifier(raw["source_id"])
            row = {
                "source_id": source_id,
                "gaia_source_found": _flag(raw.get("gaia_source_found", True)),
                **{flag: _flag(raw[flag]) for flag in FLAGS},
            }
            positive = row["gaia_source_found"] and any(row[flag] for flag in FLAGS)
            store.put_cache(
                NAMESPACE, keys[source_id], row,
                outcome="success" if positive else "not_found",
                ttl_seconds=store.positive_ttl_seconds if positive else store.negative_ttl_seconds,
                lease=leases[keys[source_id]],
            )
            rows[source_id] = row

    if missing:
        with LeaseBatch(store, NAMESPACE, [keys[source_id] for source_id in missing]) as batch:
            owned = [source_id for source_id in missing if keys[source_id] in batch.leases]
            fetch(owned, batch.leases)
            busy = [source_id for source_id in missing if keys[source_id] not in batch.leases]
        deadline = time.monotonic() + store.wait_timeout_seconds
        for source_id in busy:
            if cached(source_id):
                continue
            with store.hold_lease(
                NAMESPACE, keys[source_id], ttl=store.lease_seconds,
                wait_timeout=max(0.0, deadline - time.monotonic()),
            ) as lease:
                fetch([source_id], {keys[source_id]: lease})
    result = pd.DataFrame([rows[source_id] for source_id in ids], columns=["source_id", "gaia_source_found", *FLAGS])
    return result, len(cache_hits)
