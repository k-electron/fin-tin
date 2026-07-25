"""Pure catch-up engine tests (no container, no network).

The engine is exercised with a fake `BackfillStrategy` and a capturing
`insert_rows` (reused from the backfill test patterns) plus hand-built `WorkList`
inputs — no `edgar`, no ClickHouse. `catch_up` composes the Epic 2 reconciler's
`WorkList` with the Story 2.3 `backfill_universe`; these tests pin the parts
catch-up owns: the affected-CIK derivation and the STARTED/NOTHING_TO_DO/COMPLETED
vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest

from fintin.core.backfill import BackfillAborted
from fintin.core.catchup import CatchUpReport, CatchUpStatus, catch_up
from fintin.core.reconcile import WorkItem, WorkList
from tests.purity import assert_module_is_pure


@dataclass
class _Fact:
    """A minimal `FactLike` stub (structural — `core` needs no `edgar`)."""

    concept: str = "us-gaap:Revenues"
    taxonomy: str = "us-gaap"
    label: str = "Revenues"
    numeric_value: float | None = 1000.0
    unit: str = "USD"
    period_start: date | None = date(2023, 1, 1)
    period_end: date | None = date(2023, 12, 31)
    period_type: str = "duration"
    filing_date: date | None = date(2024, 2, 1)
    form_type: str = "10-K"
    accession: str = "0000000001-24-000001"
    is_dimensioned: bool = False


def _facts(cik: int, concepts=("us-gaap:Revenues",)):
    """Valid facts for one company (per-company accession keeps identity keys distinct)."""
    acc = f"{cik:010d}-24-000001"
    return [_Fact(concept=c, accession=acc) for c in concepts]


class _FakeStrategy:
    """Records which CIKs were fetched; returns facts / raises per configuration."""

    name = "fake"

    def __init__(self, *, facts_by_cik=None, fail=(), raise_exc=None):
        self._facts_by_cik = facts_by_cik or {}
        self._fail = set(fail)  # CIKs that raise a generic error
        self._raise_exc = raise_exc  # (ciks_set, exc_type) that raises a specific type
        self.calls: list[int] = []

    def company_facts(self, cik):
        self.calls.append(cik)
        if self._raise_exc is not None and cik in self._raise_exc[0]:
            raise self._raise_exc[1](f"boom {cik}")
        if cik in self._fail:
            raise RuntimeError(f"fetch failed for {cik}")
        return self._facts_by_cik.get(cik, [])


class _Throttle(Exception):
    """Stand-in for EdgarThrottleError (kept out of core; injected as fatal)."""


def _capturing_insert():
    batches: list[int] = []
    rows: list = []

    def _insert(batch):
        batches.append(len(batch))
        rows.extend(batch)
        return len(batch)

    return _insert, batches, rows


def _work(*items: WorkItem, scanned=None, already_present=0) -> WorkList:
    """Build a WorkList directly (the CLI feeds a real one from the reconciler)."""
    items = tuple(items)
    return WorkList(
        items=items,
        scanned=len(items) if scanned is None else scanned,
        already_present=already_present,
    )


def _item(cik: int, accession=None, form="10-K", filed=date(2024, 2, 1)) -> WorkItem:
    acc = accession or f"{cik:010d}-24-000009"
    return WorkItem(accession=acc, cik=cik, form=form, filed_date=filed)


# --- NOTHING_TO_DO: empty work list, no fetch, no insert (AC-3) -----------------


def test_nothing_to_do_on_empty_work_list():
    strat = _FakeStrategy()
    insert, batches, _ = _capturing_insert()
    statuses: list[CatchUpStatus] = []
    report = catch_up(
        _work(scanned=12),  # 12 candidate accessions scanned, none outstanding
        strategy=strat,
        insert_rows=insert,
        taxonomy_version="v",
        version=1,
        on_status=statuses.append,
    )
    assert isinstance(report, CatchUpReport)
    assert report.status is CatchUpStatus.NOTHING_TO_DO
    assert report.backfill is None
    assert report.companies == 0
    assert report.outstanding == 0
    assert report.scanned == 12
    # No STARTED, no fetch, no insert — an empty work list never starts a run.
    assert statuses == [CatchUpStatus.NOTHING_TO_DO]
    assert strat.calls == []
    assert batches == []


def test_nothing_to_do_report_props_are_none_safe():
    report = catch_up(
        _work(),
        strategy=_FakeStrategy(),
        insert_rows=_capturing_insert()[0],
        taxonomy_version="v",
        version=1,
    )
    assert report.companies_ingested == 0
    assert report.companies_failed == 0
    assert report.rows_landed == 0
    assert report.failures == ()


# --- STARTED -> COMPLETED: non-empty run ingests the affected companies (AC-1) ---


def test_started_then_completed_ingests_affected_companies():
    strat = _FakeStrategy(facts_by_cik={1: _facts(1), 2: _facts(2)})
    insert, batches, rows = _capturing_insert()
    statuses: list[CatchUpStatus] = []
    report = catch_up(
        _work(_item(1), _item(2)),
        strategy=strat,
        insert_rows=insert,
        taxonomy_version="v",
        version=1,
        on_status=statuses.append,
    )
    assert report.status is CatchUpStatus.COMPLETED
    assert report.companies == 2
    assert report.outstanding == 2
    assert report.companies_ingested == 2
    assert report.rows_landed == 2  # one fact each
    assert strat.calls == [1, 2]  # both affected companies fetched
    assert len(batches) == 2  # per-company commit grain (AD-11)
    # The lifecycle transition the AC names: STARTED then COMPLETED (in order).
    assert statuses == [CatchUpStatus.STARTED, CatchUpStatus.COMPLETED]


# --- affected-CIK derivation: dedup + sort (multiple filings per company) --------


def test_affected_ciks_are_distinct_and_sorted():
    # Several outstanding filings across CIKs 9, 5 (twice, different accessions), 2
    # → three distinct affected companies, ingested in sorted order, one each.
    strat = _FakeStrategy(facts_by_cik={2: _facts(2), 5: _facts(5), 9: _facts(9)})
    insert, batches, _ = _capturing_insert()
    report = catch_up(
        _work(
            _item(9, "0000000009-24-000001"),
            _item(5, "0000000005-24-000001"),
            _item(5, "0000000005-24-000002"),  # same company, second filing
            _item(2, "0000000002-24-000001"),
        ),
        strategy=strat,
        insert_rows=insert,
        taxonomy_version="v",
        version=1,
    )
    assert report.companies == 3  # 2, 5, 9 — deduped
    assert report.outstanding == 4  # four filings drove it
    assert strat.calls == [2, 5, 9]  # sorted, one fetch per distinct company
    assert len(batches) == 3


# --- catch-up re-ingests a PRESENT company (no already_present skip) -------------


def test_present_company_is_reingested_for_its_new_filing():
    # This is the restatement / new-filing path and the key difference from
    # backfill: catch-up passes already_present=frozenset(), so a company that
    # already has facts in the store IS re-fetched to pick up its new accession.
    strat = _FakeStrategy(facts_by_cik={7: _facts(7)})
    insert, _, rows = _capturing_insert()
    report = catch_up(
        _work(_item(7)),  # CIK 7 already exists in the store, but has a new filing
        strategy=strat,
        insert_rows=insert,
        taxonomy_version="v",
        version=50,
        on_company=None,
    )
    assert report.status is CatchUpStatus.COMPLETED
    assert strat.calls == [7]  # fetched despite being "present" — new facts land
    assert report.companies_ingested == 1


def test_version_base_offset_by_sorted_position_delegates_to_backfill():
    # catch_up delegates versioning to backfill_universe: base + sorted position,
    # so a globally-shared accession across companies resolves deterministically.
    strat = _FakeStrategy(facts_by_cik={2: _facts(2), 5: _facts(5)})
    insert, _, rows = _capturing_insert()
    catch_up(
        _work(_item(5), _item(2)),
        strategy=strat,
        insert_rows=insert,
        taxonomy_version="v",
        version=100,
    )
    assert {r.cik: r.version for r in rows} == {2: 100, 5: 101}


# --- failure handling: recorded gap, run still COMPLETED (AC-4, SM-2) -----------


def test_per_company_failure_is_recorded_and_run_still_completes():
    strat = _FakeStrategy(facts_by_cik={2: _facts(2)}, fail={1})
    insert, _, _ = _capturing_insert()
    report = catch_up(
        _work(_item(1), _item(2)),
        strategy=strat,
        insert_rows=insert,
        taxonomy_version="v",
        version=1,
    )
    # A per-company failure does NOT abort — the run reaches COMPLETED (exit-0),
    # the failure is a recorded explained gap (SM-2), and CIK 2 still ingests.
    assert report.status is CatchUpStatus.COMPLETED
    assert report.companies_failed == 1
    assert report.failures[0].cik == 1
    assert "RuntimeError" in report.failures[0].reason
    assert report.companies_ingested == 1
    assert strat.calls == [1, 2]  # continued past the failure


# --- ban-safety: throttle / systemic abort propagate, NOT exit-0 (AC-4) ---------


def test_throttle_propagates_and_completed_is_not_emitted():
    strat = _FakeStrategy(facts_by_cik={3: _facts(3)}, raise_exc=({2}, _Throttle))
    insert, _, _ = _capturing_insert()
    statuses: list[CatchUpStatus] = []
    with pytest.raises(_Throttle):
        catch_up(
            _work(_item(1), _item(2), _item(3)),
            strategy=strat,
            insert_rows=insert,
            taxonomy_version="v",
            version=1,
            fatal_errors=(_Throttle,),
            on_status=statuses.append,
        )
    # STARTED was emitted (the run began) but COMPLETED was NOT — the throttle
    # aborted before the terminal transition (ban-safety, SM-C1).
    assert statuses == [CatchUpStatus.STARTED]
    assert strat.calls == [1, 2]  # aborted at CIK 2, CIK 3 never reached


def test_systemic_abort_propagates():
    strat = _FakeStrategy(fail={1, 2, 3})
    insert, _, _ = _capturing_insert()
    with pytest.raises(BackfillAborted):
        catch_up(
            _work(_item(1), _item(2), _item(3)),
            strategy=strat,
            insert_rows=insert,
            taxonomy_version="v",
            version=1,
            max_consecutive_failures=2,
        )
    assert strat.calls == [1, 2]  # aborted at the 2nd consecutive failure


# --- observer robustness --------------------------------------------------------


def test_on_status_exception_does_not_sink_the_run():
    strat = _FakeStrategy(facts_by_cik={1: _facts(1)})
    insert, _, _ = _capturing_insert()

    def _bad_status(status):
        raise RuntimeError("status handler blew up")

    report = catch_up(
        _work(_item(1)),
        strategy=strat,
        insert_rows=insert,
        taxonomy_version="v",
        version=1,
        on_status=_bad_status,
    )
    # A bad on_status (even on the COMPLETED emission) must not lose the committed
    # ingest — the run still returns a COMPLETED report.
    assert report.status is CatchUpStatus.COMPLETED
    assert report.companies_ingested == 1


# --- single-flight: catch_up_single_flight (AC-4, AC-7, Story 3.2) --------------


class _FakeLease:
    def __init__(self, acquired: bool):
        self._acquired = acquired
        self.released = False

    def acquire(self) -> bool:
        return self._acquired

    def release(self) -> None:
        self.released = True


def test_single_flight_coalesces_to_already_running_without_running():
    from fintin.core.catchup import catch_up_single_flight

    lease = _FakeLease(acquired=False)  # a live run already holds it
    calls: list[int] = []

    def _run() -> CatchUpReport:
        calls.append(1)
        return catch_up(  # would run discovery + ingest — must NOT be reached
            _work(_item(1)),
            strategy=_FakeStrategy(facts_by_cik={1: _facts(1)}),
            insert_rows=_capturing_insert()[0],
            taxonomy_version="v",
            version=1,
        )

    report = catch_up_single_flight(lease, _run)
    assert report.status is CatchUpStatus.ALREADY_RUNNING
    assert report.backfill is None
    assert report.companies == 0
    assert calls == []  # _run (discovery + EDGAR) never invoked — AC-1
    assert lease.released is False  # we never held the lease


def test_single_flight_runs_and_releases_when_free():
    from fintin.core.catchup import catch_up_single_flight

    lease = _FakeLease(acquired=True)
    inner = catch_up(
        _work(),  # empty → NOTHING_TO_DO, a cheap real report
        strategy=_FakeStrategy(),
        insert_rows=_capturing_insert()[0],
        taxonomy_version="v",
        version=1,
    )
    report = catch_up_single_flight(lease, lambda: inner)
    assert report is inner  # the run's own report is returned unchanged
    assert report.status is CatchUpStatus.NOTHING_TO_DO
    assert lease.released is True  # released after the run


def test_already_running_is_a_catchup_status_member():
    assert CatchUpStatus.ALREADY_RUNNING.value == "ALREADY_RUNNING"


# --- purity guard --------------------------------------------------------------


def test_core_catchup_is_pure():
    """The catch-up engine imports nothing impure — the CLI runs discovery + builds
    the strategy; core only composes the reconciler's WorkList with the backfill
    engine."""
    assert_module_is_pure("fintin/core/catchup.py")
