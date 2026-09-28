"""Batched SQLite leases with one renewal worker per claimed key set."""
from __future__ import annotations

import os
import threading
import uuid
from typing import Iterable

from astro_pipeline.library.store import Lease, LeaseLostError, LibraryStore


class LeaseBatch:
    """Claim and renew many independent lease keys as one context.

    Acquisition is nonblocking.  Successfully claimed keys are exposed through
    ``leases`` and keys already owned elsewhere through ``busy``.  All claimed
    rows share one owner and are renewed by one periodic worker using a single
    short SQLite transaction per renewal interval.
    """

    def __init__(
        self,
        store: LibraryStore,
        namespace: str,
        keys: Iterable[str],
        ttl: float | None = None,
    ) -> None:
        namespace = str(namespace).strip()
        if not namespace:
            raise ValueError("namespace must not be blank")
        unique_keys: list[str] = []
        seen: set[str] = set()
        for raw_key in keys:
            key = str(raw_key).strip()
            if not key:
                raise ValueError("lease keys must not be blank")
            if key not in seen:
                seen.add(key)
                unique_keys.append(key)
        seconds = store.lease_seconds if ttl is None else float(ttl)
        if seconds <= 0:
            raise ValueError("ttl must be greater than zero")

        self.store = store
        self.namespace = namespace
        self.keys = tuple(unique_keys)
        self.ttl = float(seconds)
        self.leases: dict[str, Lease] = {}
        self.busy: list[str] = []
        self.lost: list[str] = []

        self._owner = (
            f"{os.getpid()}:{threading.get_ident()}:batch:{uuid.uuid4().hex}"
        )
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._renewal_error: BaseException | None = None
        self._entered = False

    def __enter__(self) -> LeaseBatch:
        if self._entered:
            raise RuntimeError("LeaseBatch contexts cannot be reused")
        self._entered = True
        self._acquire_available()
        if self.leases:
            self._thread = threading.Thread(
                target=self._renew_periodically,
                name=f"library-lease-batch-{self.namespace}",
                daemon=True,
            )
            try:
                self._thread.start()
            except BaseException:
                self._release_all()
                raise
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        body_failed = exc_type is not None
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        release_error: BaseException | None = None
        try:
            self._release_all()
        except BaseException as release_exc:
            release_error = release_exc
        if not body_failed:
            if self._renewal_error is not None:
                raise self._renewal_error
            if release_error is not None:
                raise release_error
        return False

    def _acquire_available(self) -> None:
        if not self.keys:
            return
        now = float(self.store.clock())
        expires_at = now + self.ttl
        with self.store.transaction() as connection:
            for key in self.keys:
                row = connection.execute(
                    "SELECT owner, token, expires_at FROM leases "
                    "WHERE namespace = ? AND key = ?",
                    (self.namespace, key),
                ).fetchone()
                if row is None:
                    token = 1
                    connection.execute(
                        "INSERT INTO leases(namespace, key, owner, token, "
                        "expires_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            self.namespace,
                            key,
                            self._owner,
                            token,
                            expires_at,
                            now,
                        ),
                    )
                elif float(row["expires_at"]) <= now:
                    token = int(row["token"]) + 1
                    cursor = connection.execute(
                        "UPDATE leases SET owner = ?, token = ?, expires_at = ?, "
                        "updated_at = ? WHERE namespace = ? AND key = ? "
                        "AND token = ? AND expires_at <= ?",
                        (
                            self._owner,
                            token,
                            expires_at,
                            now,
                            self.namespace,
                            key,
                            int(row["token"]),
                            now,
                        ),
                    )
                    if cursor.rowcount != 1:
                        self.busy.append(key)
                        continue
                else:
                    self.busy.append(key)
                    continue
                self.leases[key] = Lease(
                    self.namespace,
                    key,
                    self._owner,
                    token,
                    expires_at,
                )

    def renew_now(self) -> None:
        """Renew every still-owned key in one fenced transaction."""
        if not self._entered:
            raise RuntimeError("LeaseBatch must be entered before renewal")
        with self._lock:
            if not self.leases:
                return
            now = float(self.store.clock())
            expires_at = now + self.ttl
            renewed: dict[str, Lease] = {}
            lost: list[str] = []
            with self.store.transaction() as connection:
                for key, lease in self.leases.items():
                    cursor = connection.execute(
                        "UPDATE leases SET expires_at = ?, updated_at = ? "
                        "WHERE namespace = ? AND key = ? AND owner = ? "
                        "AND token = ? AND expires_at > ?",
                        (
                            expires_at,
                            now,
                            lease.namespace,
                            lease.key,
                            lease.owner,
                            lease.token,
                            now,
                        ),
                    )
                    if cursor.rowcount != 1:
                        lost.append(key)
                    else:
                        renewed[key] = Lease(
                            lease.namespace,
                            lease.key,
                            lease.owner,
                            lease.token,
                            expires_at,
                        )
            self.leases.update(renewed)
            for key in lost:
                if key not in self.lost:
                    self.lost.append(key)
            if lost:
                raise LeaseLostError(
                    "lease batch lost fencing ownership for: "
                    + ", ".join(sorted(lost))
                )

    def assert_current(self, key: str) -> Lease:
        """Fence one claimed key and return its current lease value."""
        key = str(key)
        with self._lock:
            lease = self.leases.get(key)
        if lease is None:
            raise LeaseLostError(
                f"lease batch does not own {self.namespace}:{key}"
            )
        with self.store.transaction() as connection:
            self.store.assert_lease(connection, lease)
        return lease

    def _renew_periodically(self) -> None:
        interval = max(0.01, self.ttl / 3.0)
        while not self._stop.wait(interval):
            try:
                self.renew_now()
            except BaseException as exc:
                self._renewal_error = exc
                self._stop.set()
                return

    def _release_all(self) -> None:
        with self._lock:
            leases = list(self.leases.values())
        if not leases:
            return
        now = float(self.store.clock())
        with self.store.transaction() as connection:
            for lease in leases:
                connection.execute(
                    "UPDATE leases SET expires_at = ?, updated_at = ? "
                    "WHERE namespace = ? AND key = ? AND owner = ? AND token = ?",
                    (
                        now,
                        now,
                        lease.namespace,
                        lease.key,
                        lease.owner,
                        lease.token,
                    ),
                )
