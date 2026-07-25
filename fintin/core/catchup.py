"""Catch-up — the pure engine + the run-status vocabulary (FR-8, FR-12, AD-2).

Pure `core`: depends on nothing outward (no `edgar`, no ClickHouse, no `pyarrow`).
It composes the Epic 2 work-list reconciler (the CLI computes the `WorkList` and
passes it in) with the Story 2.3 per-company backfill engine — **reusing both,
re-implementing neither** (the Epic 3 guardrail).

`catch_up` derives the distinct companies the outstanding accessions belong to and
re-ingests each affected company's full `companyfacts` via :func:`backfill_universe`
with **nothing skipped** — a company appears here precisely to pick up a *new*
filing (its unchanged history re-lands idempotently, AD-6). The new/restated facts
win on read by a higher ingest-monotonic `version` (AD-6) + latest-filed (AD-7), so
"everything filed since" is ingested through the one per-company path (AD-13).

Status vocabulary (architecture brief §4 — all exit-0; the trigger interprets the
outcome, the engine emits it):
  * ``NOTHING_TO_DO`` — empty work list; **no `companyfacts` request**.
  * ``STARTED`` → ``COMPLETED`` — a non-empty run began and finished.
  * ``ALREADY_RUNNING`` — a live run already holds the single-flight lease
    (AD-12); this trigger coalesces and does nothing (Story 3.2, via
    :func:`catch_up_single_flight`), issuing **no** EDGAR request.
A throttle or systemic abort is NOT in this vocabulary: it propagates out (via
``fatal_errors`` / ``BackfillAborted``) so the CLI exits non-zero — ban-safety
(SM-C1) outranks reporting a success.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Sequence
from typing import NamedTuple

from fintin.core.backfill import (
    BackfillEvent,
    BackfillFailure,
    BackfillReport,
    BackfillStrategy,
    backfill_universe,
)
from fintin.core.ingest import RawFactRow
from fintin.core.lease import Lease, run_single_flight
from fintin.core.reconcile import WorkList


class CatchUpStatus(enum.Enum):
    """The run-outcome vocabulary (all exit-0 at the CLI); rendered via ``.value``.

    A plain :class:`enum.Enum` (not ``str``-mixed) so ``str()``/f-string rendering
    is stable across Python versions."""

    STARTED = "STARTED"
    ALREADY_RUNNING = "ALREADY_RUNNING"
    NOTHING_TO_DO = "NOTHING_TO_DO"
    COMPLETED = "COMPLETED"


class CatchUpReport(NamedTuple):
    """The terminal outcome of a catch-up run. ``status`` is ``NOTHING_TO_DO`` or
    ``COMPLETED`` (``STARTED`` is a transient lifecycle emission, never a terminal
    state). ``backfill`` is the wrapped per-company ingest report, or ``None`` when
    there was nothing to do (the convenience properties stay valid either way)."""

    status: CatchUpStatus
    scanned: int  # candidate accessions the reconciler diffed (work.scanned)
    outstanding: int  # outstanding filings that drove the run (len(work.items))
    companies: int  # distinct affected companies ingested
    backfill: BackfillReport | None

    @property
    def companies_ingested(self) -> int:
        return self.backfill.companies_ingested if self.backfill is not None else 0

    @property
    def companies_failed(self) -> int:
        return self.backfill.companies_failed if self.backfill is not None else 0

    @property
    def rows_landed(self) -> int:
        return self.backfill.rows_landed if self.backfill is not None else 0

    @property
    def failures(self) -> tuple[BackfillFailure, ...]:
        return self.backfill.failures if self.backfill is not None else ()


def _emit_status(
    on_status: Callable[[CatchUpStatus], None] | None, status: CatchUpStatus
) -> None:
    if on_status is None:
        return
    try:
        on_status(status)
    except Exception:
        # A passive lifecycle observer must never sink a run whose companies are
        # already committed — especially the COMPLETED emission. Swallow its errors.
        pass


def catch_up(
    work: WorkList,
    *,
    strategy: BackfillStrategy,
    insert_rows: Callable[[Sequence[RawFactRow]], int],
    taxonomy_version: str,
    version: int,
    fatal_errors: tuple[type[BaseException], ...] = (),
    max_consecutive_failures: int | None = None,
    on_company: Callable[[BackfillEvent], None] | None = None,
    on_status: Callable[[CatchUpStatus], None] | None = None,
) -> CatchUpReport:
    """Catch the store up to ``work``. Pure (no I/O): the CLI has already run
    discovery (the Epic 2 reconciler) and passes the finished :class:`WorkList` in.

    Derives the distinct affected companies from ``work.items`` (sorted,
    deterministic — kboss) and re-ingests each via :func:`backfill_universe` with
    ``already_present=frozenset()`` — nothing skipped, because a company appears
    here precisely to pick up a NEW filing (unchanged history re-lands
    idempotently, AD-6). ``fatal_errors`` / ``max_consecutive_failures`` pass
    straight through: a throttle or systemic abort propagates out (ban-safety,
    SM-C1) rather than returning a success status.

    Emits ``NOTHING_TO_DO`` (empty work list — no fetch, no insert) or
    ``STARTED``→``COMPLETED`` via ``on_status``; returns the terminal
    :class:`CatchUpReport`."""
    affected = tuple(sorted({item.cik for item in work.items}))

    if not affected:
        _emit_status(on_status, CatchUpStatus.NOTHING_TO_DO)
        return CatchUpReport(
            status=CatchUpStatus.NOTHING_TO_DO,
            scanned=work.scanned,
            outstanding=len(work.items),
            companies=0,
            backfill=None,
        )

    _emit_status(on_status, CatchUpStatus.STARTED)
    report = backfill_universe(
        affected,
        strategy=strategy,
        insert_rows=insert_rows,
        taxonomy_version=taxonomy_version,
        version=version,
        already_present=frozenset(),  # re-ingest every affected company (new filings)
        fatal_errors=fatal_errors,
        max_consecutive_failures=max_consecutive_failures,
        on_company=on_company,
    )
    # Only reached if backfill_universe returned normally — a fatal_errors type or
    # BackfillAborted propagates through, so COMPLETED is never emitted on abort.
    _emit_status(on_status, CatchUpStatus.COMPLETED)
    return CatchUpReport(
        status=CatchUpStatus.COMPLETED,
        scanned=work.scanned,
        outstanding=len(work.items),
        companies=len(affected),
        backfill=report,
    )


def catch_up_single_flight(
    lease: Lease, run: Callable[[], CatchUpReport]
) -> CatchUpReport:
    """Run a catch-up under the single-flight ``lease`` (AD-12). ``run`` does the
    discovery + :func:`catch_up` (the CLI wires it). If a live run already holds
    the lease, coalesce: return a terminal ``ALREADY_RUNNING`` report **without
    invoking ``run``** — so the coalesced trigger issues no EDGAR request (AC-1).
    Otherwise the lease is acquired (or an expired one reclaimed), ``run`` runs,
    and the lease is released."""
    result = run_single_flight(lease, run)
    if result is None:
        return CatchUpReport(
            status=CatchUpStatus.ALREADY_RUNNING,
            scanned=0,
            outstanding=0,
            companies=0,
            backfill=None,
        )
    return result
