"""Single-flight lease — the pure port + combinator (AD-12, AD-2, FR-11).

Part of the pure core: depends on nothing outward (no `edgar`, no ClickHouse, no
`pyarrow`, no filesystem). It defines the :class:`Lease` **port** (Protocol) and
the :func:`run_single_flight` combinator that enforces "at most one run at a
time" — the single-flight policy that AD-2 requires to live **inside the engine**,
never in a trigger. The concrete lease (a self-expiring filesystem lock file)
implements this port in ``fintin.adapters.lease`` and is injected in by the CLI.

Coalesce, don't queue (AD-12): a trigger that finds the lease held by a live run
does nothing and its caller reports ``ALREADY_RUNNING`` (exit-0) — it never runs
the guarded work, so it issues **no** EDGAR request. A crashed run's lease
self-expires (its heartbeat goes stale past the TTL); the next trigger reclaims
it and re-derives the remaining work from the DB (AD-16), so the lease governs
*who* runs, never *what is left*.
"""

from __future__ import annotations

from typing import Callable, Protocol, TypeVar, runtime_checkable

T = TypeVar("T")


@runtime_checkable
class Lease(Protocol):
    """A single-flight lease port. The concrete adapter (filesystem) decides how
    "held" is represented and expired; the engine depends only on this shape."""

    def acquire(self) -> bool:
        """Try to take the lease. Return ``True`` if acquired — either it was free
        or an **expired** (stale-heartbeat) lease was reclaimed. Return ``False``
        if a **live** (heartbeating) run holds it — the caller must coalesce
        (``ALREADY_RUNNING``), not queue, and must issue no side effects."""
        ...

    def release(self) -> None:
        """Release the lease if (and only if) we still hold it. Idempotent; safe
        to call after a failed :meth:`acquire` (it is a no-op then)."""
        ...


def run_single_flight(lease: Lease, run: Callable[[], T]) -> T | None:
    """Run ``run`` under the single-flight ``lease``. Acquire first; if a live run
    holds the lease, return ``None`` **without invoking ``run``** — the coalesced
    caller does no work and issues no EDGAR request (AD-12/AC-1, ban-safety).
    Otherwise run and always release afterwards (even if ``run`` raises — the lease
    must never leak on an error path). Generic in ``T`` so every ingestion run
    (catch-up, backfill, …) reuses this one guard."""
    if not lease.acquire():
        return None
    try:
        return run()
    finally:
        lease.release()
