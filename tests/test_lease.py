"""Single-flight lease tests — the filesystem adapter (real temp files, fast
TTL/heartbeat) and the pure `run_single_flight` combinator (fake lease). No EDGAR,
no ClickHouse (NFR-7 trivially).
"""

from __future__ import annotations

import ast
import os
import time
from pathlib import Path

import pytest

from fintin.adapters.lease.file_lease import FileLease
from fintin.core.lease import run_single_flight


# --- FileLease: acquire / coalesce / reclaim / heartbeat / release --------------


def test_free_lease_is_acquired(tmp_path):
    p = str(tmp_path / "x.lease")
    lease = FileLease(p, ttl_seconds=5, heartbeat_seconds=1)
    assert lease.acquire() is True
    assert os.path.exists(p)  # the lock file exists while held
    lease.release()


def test_second_acquire_of_a_live_lease_coalesces(tmp_path):
    p = str(tmp_path / "x.lease")
    holder = FileLease(p, ttl_seconds=5, heartbeat_seconds=1)
    assert holder.acquire() is True
    contender = FileLease(p, ttl_seconds=5, heartbeat_seconds=1)
    assert contender.acquire() is False  # a live run holds it → coalesce (AD-12)
    contender.release()  # no-op — must not disturb the holder's lease
    assert os.path.exists(p)
    holder.release()


def test_stale_lease_is_reclaimed(tmp_path):
    # A holder whose heartbeat interval far exceeds its TTL never refreshes in time
    # — it stands in for a crashed run whose lease has gone stale.
    p = str(tmp_path / "x.lease")
    crashed = FileLease(p, ttl_seconds=0.1, heartbeat_seconds=30.0)
    assert crashed.acquire() is True
    time.sleep(0.2)  # > ttl, and the (30s) heartbeat has not fired
    reclaimer = FileLease(p, ttl_seconds=0.1, heartbeat_seconds=30.0)
    assert reclaimer.acquire() is True  # reclaimed the expired lease (AC-2)
    reclaimer.release()
    crashed.release()  # token no longer matches → does not delete reclaimer's file


def test_heartbeat_keeps_a_live_lease_fresh(tmp_path):
    # AC-3 mechanism: the background heartbeat thread refreshes the lease every
    # 0.05s, so even after sleeping past the 0.3s TTL the lease is NOT reclaimable.
    p = str(tmp_path / "x.lease")
    holder = FileLease(p, ttl_seconds=0.3, heartbeat_seconds=0.05)
    assert holder.acquire() is True
    time.sleep(0.5)  # well past the TTL — but the heartbeat kept beating
    contender = FileLease(p, ttl_seconds=0.3, heartbeat_seconds=0.05)
    assert contender.acquire() is False  # still held — heartbeat prevented reclaim
    holder.release()


def test_release_frees_the_lease_for_reacquire(tmp_path):
    p = str(tmp_path / "x.lease")
    a = FileLease(p, ttl_seconds=5, heartbeat_seconds=1)
    assert a.acquire() is True
    a.release()
    assert not os.path.exists(p)  # released → file removed
    b = FileLease(p, ttl_seconds=5, heartbeat_seconds=1)
    assert b.acquire() is True  # freely re-acquirable
    b.release()


def test_corrupt_lease_file_is_reclaimed(tmp_path):
    p = tmp_path / "x.lease"
    p.write_text("{ not valid json")  # a torn/garbage lease file
    lease = FileLease(str(p), ttl_seconds=5, heartbeat_seconds=1)
    assert lease.acquire() is True  # unparseable → treated as stale → reclaimed
    lease.release()


def test_release_is_idempotent_and_ownership_checked(tmp_path):
    p = str(tmp_path / "x.lease")
    a = FileLease(p, ttl_seconds=5, heartbeat_seconds=1)
    assert a.acquire() is True
    a.release()
    a.release()  # idempotent — second release is a harmless no-op

    # A run that failed to acquire (someone else holds the lease) must never delete
    # the holder's lease when it releases.
    holder = FileLease(p, ttl_seconds=5, heartbeat_seconds=1)
    assert holder.acquire() is True
    loser = FileLease(p, ttl_seconds=5, heartbeat_seconds=1)
    assert loser.acquire() is False
    loser.release()
    assert os.path.exists(p)  # holder's lease survives the loser's release
    holder.release()


# --- run_single_flight (pure combinator, fake lease) ----------------------------


class _FakeLease:
    def __init__(self, acquired: bool):
        self._acquired = acquired
        self.released = False
        self.acquire_calls = 0

    def acquire(self) -> bool:
        self.acquire_calls += 1
        return self._acquired

    def release(self) -> None:
        self.released = True


def test_run_single_flight_runs_then_releases_when_acquired():
    lease = _FakeLease(acquired=True)
    calls: list[int] = []

    def _run():
        calls.append(1)
        return "done"

    assert run_single_flight(lease, _run) == "done"
    assert calls == [1]  # run invoked exactly once
    assert lease.released is True  # lease released afterwards


def test_run_single_flight_coalesces_without_running_when_held():
    lease = _FakeLease(acquired=False)
    calls: list[int] = []
    result = run_single_flight(lease, lambda: calls.append(1))
    assert result is None  # coalesced
    assert calls == []  # run NEVER invoked → no side effects, no EDGAR (AC-1)
    assert lease.released is False  # we never held it, so nothing to release


def test_run_single_flight_releases_even_when_run_raises():
    lease = _FakeLease(acquired=True)

    def _boom():
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError):
        run_single_flight(lease, _boom)
    assert lease.released is True  # the lease must not leak on an error path


# --- purity guard ---------------------------------------------------------------


def _module_imports(path: str) -> set[str]:
    tree = ast.parse(Path(path).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                imported.add(n.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    return imported


def test_core_lease_is_pure():
    """The lease port + combinator import no `edgar`, ClickHouse, or `pyarrow` (and
    no filesystem) — the concrete filesystem lease is the adapter's job."""
    imports = _module_imports("fintin/core/lease.py")
    assert "edgar" not in imports
    assert "clickhouse_connect" not in imports
    assert "pyarrow" not in imports
    assert "os" not in imports  # no filesystem in the pure port
