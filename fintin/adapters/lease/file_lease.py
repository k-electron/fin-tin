"""Filesystem single-flight lease — the concrete :class:`~fintin.core.lease.Lease`
(AD-12). A self-expiring lock **file** (path from config), NOT a ClickHouse table
(AD-1/AD-18 untouched), guarding "at most one ingestion run at a time".

Mechanism:
- **Acquire** claims the lease by atomically hard-linking a fully-written record
  file into place (``os.link`` fails if the target exists — exclusive *and* never
  leaves a half-written file a racer could misread). A pre-existing lease is read:
  if its heartbeat is older than its TTL (or it is corrupt) it is **stale** — a
  crashed holder — so it is removed and the claim retried; otherwise a **live**
  run holds it and :meth:`acquire` returns ``False`` (coalesce).
- A **background daemon heartbeat thread** rewrites ``heartbeat_at`` every
  ``heartbeat_seconds`` while we hold the lease. Because it is a separate thread it
  keeps beating while the main thread is blocked in the EDGAR cool-down sleep
  (AD-3), so the lease is not reclaimed mid-cool-down (AC-3). A hard crash stops
  the daemon thread with the process, so the lease goes stale and is reclaimable.
- **Release** stops the heartbeat and unlinks the file — only if we still own it
  (token match), so we never delete a lease another run has since reclaimed.

A per-acquire ``token`` (uuid) identifies our hold, so heartbeat/release never
touch a lease we no longer own. All record writes are atomic (write a temp file,
then ``os.link``/``os.replace``) so a concurrent reader never sees a partial file.

Known v1 limitation (single laptop): two triggers reclaiming the *same* stale
lease at the same instant could both proceed (a fully-robust reclaim needs
``fcntl``/``flock``). Negligible on a single-user machine — see deferred-work.md.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger("fintin.lease")


class FileLease:
    """A self-expiring filesystem lease (satisfies :class:`fintin.core.lease.Lease`).

    ``ttl_seconds`` is how long after the last heartbeat the lease is considered
    expired; ``heartbeat_seconds`` (≪ TTL) is how often the background thread
    refreshes it. Construct one per run; :meth:`acquire` then :meth:`release`.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        ttl_seconds: float,
        heartbeat_seconds: float,
    ) -> None:
        self._path = str(path)
        self._ttl_seconds = float(ttl_seconds)
        self._heartbeat_seconds = float(heartbeat_seconds)
        self._token = uuid.uuid4().hex
        self._acquired_at = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- Lease port -------------------------------------------------------------

    def acquire(self) -> bool:
        """Take the lease (``True``) or coalesce (``False`` — a live run holds it).
        Reclaims an expired (stale-heartbeat) or corrupt lease."""
        parent = os.path.dirname(os.path.abspath(self._path)) or "."
        os.makedirs(parent, exist_ok=True)
        for _ in range(3):  # bounded retries across a reclaim race
            now = _now()
            record = {
                "token": self._token,
                "pid": os.getpid(),
                "host": platform.node(),
                "acquired_at": now,
                "heartbeat_at": now,
                "ttl_seconds": self._ttl_seconds,
            }
            tmp = f"{self._path}.{self._token}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(record, fh)
            try:
                # Atomic exclusive claim: link fails if the target already exists,
                # and the target is fully populated the instant it appears.
                os.link(tmp, self._path)
            except FileExistsError:
                _unlink_quietly(tmp)
                existing = self._read_record()
                if existing is not None and not _is_stale(existing, _now()):
                    return False  # a live run holds the lease — coalesce (AD-12)
                _unlink_quietly(self._path)  # stale/corrupt → reclaim, then retry
                continue
            else:
                _unlink_quietly(tmp)  # target is hard-linked; drop the temp name
                self._acquired_at = now
                self._start_heartbeat()
                return True
        return False  # someone reclaimed between our unlink and link — coalesce

    def release(self) -> None:
        """Stop heartbeating and remove the lease file — only if we still hold it.
        Idempotent; a no-op after a failed :meth:`acquire`."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self._heartbeat_seconds + 5.0)
            self._thread = None
        record = self._read_record()
        if record is not None and record.get("token") == self._token:
            _unlink_quietly(self._path)

    # --- internals --------------------------------------------------------------

    def _start_heartbeat(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name="fintin-lease-heartbeat",
            daemon=True,
        )
        self._thread.start()

    def _heartbeat_loop(self) -> None:
        # `wait` returns True the instant `release()` sets the event, so the loop
        # exits promptly; otherwise it times out after one interval and beats.
        while not self._stop.wait(self._heartbeat_seconds):
            try:
                self._touch()
            except Exception:  # a transient FS hiccup must not kill the heartbeat
                logger.debug("lease heartbeat write failed", exc_info=True)

    def _touch(self) -> None:
        record = self._read_record()
        if record is None or record.get("token") != self._token:
            return  # we no longer own the lease (reclaimed elsewhere) — stop
        record["heartbeat_at"] = _now()
        self._atomic_write(record)

    def _atomic_write(self, record: dict) -> None:
        tmp = f"{self._path}.{self._token}.{os.getpid()}.hb.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        os.replace(tmp, self._path)  # atomic in-place update of the held lease

    def _read_record(self) -> dict | None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, OSError, ValueError):
            return None  # missing or corrupt/partial → treat as absent
        return data if isinstance(data, dict) else None


def _now() -> float:
    return time.time()


def _is_stale(record: dict, now: float) -> bool:
    """True if the lease's heartbeat is older than its TTL, or the record is
    unusable — either way a crashed/gone holder we may reclaim."""
    hb = record.get("heartbeat_at")
    ttl = record.get("ttl_seconds")
    if not isinstance(hb, (int, float)) or not isinstance(ttl, (int, float)):
        return True
    return (now - hb) > ttl


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
