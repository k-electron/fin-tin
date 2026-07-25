"""Pure backfill engine tests (no container, no network).

The engine is exercised with a fake `BackfillStrategy` (returns lightweight
`FactLike` stubs / raises) and a fake `insert_rows` capturing per-company batches
— no `edgar`, no ClickHouse. `ingest_company`'s transform runs for real, so the
stubs must pass its filters.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

from fintin.core.backfill import (
    BackfillAborted,
    BackfillEvent,
    BackfillFailure,
    BackfillReport,
    backfill_universe,
)


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

    def __init__(self, *, facts_by_cik=None, fail=(), raise_exc=None, on_cik=None):
        self._facts_by_cik = facts_by_cik or {}
        self._fail = set(fail)  # CIKs that raise a generic error
        self._raise_exc = raise_exc  # (ciks_set, exc_type) that raises a specific type
        self._on_cik = on_cik
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


# --- happy path: per-company ingest + commit -----------------------------------


def test_ingests_each_company_committing_per_company():
    strat = _FakeStrategy(
        facts_by_cik={1: _facts(1, ("us-gaap:Revenues", "us-gaap:Assets")), 2: _facts(2)}
    )
    insert, batches, rows = _capturing_insert()
    report = backfill_universe(
        [1, 2],
        strategy=strat,
        insert_rows=insert,
        taxonomy_version="5.43.0",
        version=7,
    )
    assert report.companies_ingested == 2
    assert report.companies_skipped == 0
    assert report.companies_failed == 0
    assert report.rows_landed == 3  # 2 for CIK 1 + 1 for CIK 2
    # Per-company commit grain (AD-11): one insert call per ingested company.
    assert len(batches) == 2
    # Version is the run base offset by sorted position (AD-6 + deterministic
    # cross-company tie-break): CIK 1 (pos 0) → 7, CIK 2 (pos 1) → 8.
    assert {r.cik: r.version for r in rows} == {1: 7, 2: 8}
    assert report.version == 7  # the base for the run


def test_report_is_backfill_report_with_no_gaps_when_all_succeed():
    strat = _FakeStrategy(facts_by_cik={1: _facts(1)})
    insert, _, _ = _capturing_insert()
    report = backfill_universe(
        [1], strategy=strat, insert_rows=insert, taxonomy_version="v", version=1
    )
    assert isinstance(report, BackfillReport)
    assert report.failures == ()


# --- resumability: skip already-present WITHOUT fetching (AC-2, SM-C1) ----------


def test_skips_already_present_without_fetching():
    strat = _FakeStrategy(facts_by_cik={1: _facts(1), 2: _facts(2)})
    insert, batches, _ = _capturing_insert()
    report = backfill_universe(
        [1, 2],
        strategy=strat,
        insert_rows=insert,
        taxonomy_version="v",
        version=1,
        already_present={1},
    )
    assert report.skipped == (1,)
    assert report.companies_ingested == 1  # only CIK 2
    assert 1 not in strat.calls  # the skipped company was NEVER fetched (no request)
    assert strat.calls == [2]
    assert len(batches) == 1  # no insert for the skipped company


def test_rerun_with_all_present_is_a_noop():
    strat = _FakeStrategy(facts_by_cik={1: _facts(1), 2: _facts(2)})
    insert, batches, _ = _capturing_insert()
    report = backfill_universe(
        [1, 2],
        strategy=strat,
        insert_rows=insert,
        taxonomy_version="v",
        version=1,
        already_present={1, 2},
    )
    assert report.companies_ingested == 0
    assert report.companies_skipped == 2
    assert strat.calls == []  # nothing fetched
    assert batches == []  # nothing inserted


# --- failure handling: recorded, not fatal; run continues (AC-3, SM-2) ----------


def test_records_failure_and_continues():
    strat = _FakeStrategy(facts_by_cik={2: _facts(2)}, fail={1})
    insert, _, _ = _capturing_insert()
    report = backfill_universe(
        [1, 2], strategy=strat, insert_rows=insert, taxonomy_version="v", version=1
    )
    assert report.companies_failed == 1
    assert report.failures[0].cik == 1
    assert "RuntimeError" in report.failures[0].reason
    assert isinstance(report.failures[0], BackfillFailure)
    assert report.companies_ingested == 1  # CIK 2 still ingested after CIK 1 failed
    assert strat.calls == [1, 2]  # the run continued past the failure


def test_zero_row_company_is_a_clean_ingest_not_a_failure():
    # A company that yields zero facts lands zero rows but is NOT a failure — it's
    # a clean ingest of an empty set (folded into companies_ingested, rows_landed
    # 0). Note the resume consequence (see deferred-work.md): with 0 rows it never
    # becomes "present", so a later run re-fetches it. (NoCompanyFactsError, by
    # contrast, raises and is recorded as a failure — covered above.)
    strat = _FakeStrategy(facts_by_cik={1: []})
    insert, batches, rows = _capturing_insert()
    report = backfill_universe(
        [1], strategy=strat, insert_rows=insert, taxonomy_version="v", version=1
    )
    assert report.companies_ingested == 1
    assert report.rows_landed == 0
    assert report.companies_failed == 0
    assert batches == [0]  # an (empty) insert still ran for the company


# --- ban-safety: fatal_errors aborts the whole run (AC-4, SM-C1) ----------------


def test_fatal_error_aborts_the_run():
    strat = _FakeStrategy(facts_by_cik={3: _facts(3)}, raise_exc=({2}, _Throttle))
    insert, _, _ = _capturing_insert()
    with pytest.raises(_Throttle):
        backfill_universe(
            [1, 2, 3],
            strategy=strat,
            insert_rows=insert,
            taxonomy_version="v",
            version=1,
            fatal_errors=(_Throttle,),
        )
    # Aborted at CIK 2 — CIK 3 was never reached (ban-safety: stop, don't continue).
    assert strat.calls == [1, 2]


def test_non_fatal_error_type_is_recorded_when_not_in_fatal_errors():
    # The SAME exception type is a recorded gap when NOT listed as fatal.
    strat = _FakeStrategy(facts_by_cik={2: _facts(2)}, raise_exc=({1}, _Throttle))
    insert, _, _ = _capturing_insert()
    report = backfill_universe(
        [1, 2],
        strategy=strat,
        insert_rows=insert,
        taxonomy_version="v",
        version=1,
        fatal_errors=(),  # nothing fatal → the _Throttle at CIK 1 is recorded
    )
    assert report.companies_failed == 1
    assert report.companies_ingested == 1
    assert strat.calls == [1, 2]


# --- circuit breaker: systemic failure aborts (SM-C1) --------------------------


def test_circuit_breaker_aborts_after_consecutive_failures():
    # A systemic failure (e.g. store down mid-run) must abort, not launder every
    # remaining company into a gap while still fetching from EDGAR.
    strat = _FakeStrategy(fail={1, 2, 3, 4, 5})
    insert, _, _ = _capturing_insert()
    with pytest.raises(BackfillAborted):
        backfill_universe(
            [1, 2, 3, 4, 5],
            strategy=strat,
            insert_rows=insert,
            taxonomy_version="v",
            version=1,
            max_consecutive_failures=2,
        )
    assert strat.calls == [1, 2]  # aborted at the 2nd failure — 3,4,5 never fetched


def test_consecutive_failure_counter_resets_on_success():
    # A success between failures resets the counter, so scattered failures don't
    # trip the breaker.
    strat = _FakeStrategy(facts_by_cik={2: _facts(2)}, fail={1, 3})
    insert, _, _ = _capturing_insert()
    report = backfill_universe(
        [1, 2, 3],  # fail, success (resets), fail
        strategy=strat,
        insert_rows=insert,
        taxonomy_version="v",
        version=1,
        max_consecutive_failures=2,
    )
    assert report.companies_failed == 2
    assert report.companies_ingested == 1
    assert strat.calls == [1, 2, 3]  # no abort


def test_skips_are_neutral_to_the_consecutive_counter():
    # A skip neither increments nor resets the counter, so fail→skip→fail still
    # trips a threshold of 2 (the skip does not rescue a systemic failure).
    strat = _FakeStrategy(fail={1, 3})
    insert, _, _ = _capturing_insert()
    with pytest.raises(BackfillAborted):
        backfill_universe(
            [1, 2, 3],
            strategy=strat,
            insert_rows=insert,
            taxonomy_version="v",
            version=1,
            already_present={2},  # 2 is skipped between the two failures
            max_consecutive_failures=2,
        )
    assert strat.calls == [1, 3]  # 2 skipped (never fetched); aborted at 3


def test_no_circuit_breaker_by_default():
    # Default (None) preserves record-and-continue for every company.
    strat = _FakeStrategy(fail={1, 2, 3})
    insert, _, _ = _capturing_insert()
    report = backfill_universe(
        [1, 2, 3], strategy=strat, insert_rows=insert, taxonomy_version="v", version=1
    )
    assert report.companies_failed == 3  # all recorded, no abort


# --- deterministic per-company version (co-filed accession collision) ----------


def test_version_is_base_offset_by_sorted_position():
    # Each company gets base + its sorted position, so a globally-shared accession
    # across two companies resolves deterministically (higher position wins),
    # never by ReplacingMergeTree's arbitrary equal-version tie-break.
    strat = _FakeStrategy(facts_by_cik={5: _facts(5), 9: _facts(9), 2: _facts(2)})
    insert, _, rows = _capturing_insert()
    backfill_universe(
        [9, 5, 2],  # unsorted input
        strategy=strat,
        insert_rows=insert,
        taxonomy_version="v",
        version=100,
    )
    # sorted order 2,5,9 → positions 0,1,2 → versions 100,101,102
    assert {r.cik: r.version for r in rows} == {2: 100, 5: 101, 9: 102}


# --- observer robustness -------------------------------------------------------


def test_observer_exception_does_not_sink_the_run():
    strat = _FakeStrategy(facts_by_cik={1: _facts(1), 2: _facts(2)})
    insert, _, _ = _capturing_insert()

    def _bad_observer(event):
        raise RuntimeError("logging handler blew up")

    report = backfill_universe(
        [1, 2],
        strategy=strat,
        insert_rows=insert,
        taxonomy_version="v",
        version=1,
        on_company=_bad_observer,
    )
    assert report.companies_ingested == 2  # a bad observer did not abort the run


# --- determinism + dedup + events ----------------------------------------------


def test_iterates_sorted_and_dedups_ciks():
    strat = _FakeStrategy(facts_by_cik={1: _facts(1), 2: _facts(2), 3: _facts(3)})
    insert, _, _ = _capturing_insert()
    report = backfill_universe(
        [3, 1, 2, 1],  # out of order, with a duplicate
        strategy=strat,
        insert_rows=insert,
        taxonomy_version="v",
        version=1,
    )
    assert strat.calls == [1, 2, 3]  # sorted, deduped, deterministic
    assert report.attempted == 3


def test_on_company_events_fire_with_index_total_and_outcome():
    strat = _FakeStrategy(facts_by_cik={2: _facts(2)}, fail={3})
    insert, _, _ = _capturing_insert()
    events: list[BackfillEvent] = []
    backfill_universe(
        [1, 2, 3],
        strategy=strat,
        insert_rows=insert,
        taxonomy_version="v",
        version=1,
        already_present={1},
        on_company=events.append,
    )
    assert [(e.cik, e.outcome, e.index, e.total) for e in events] == [
        (1, "skipped", 1, 3),
        (2, "ingested", 2, 3),
        (3, "failed", 3, 3),
    ]


def test_empty_universe_is_clean():
    strat = _FakeStrategy()
    insert, batches, _ = _capturing_insert()
    report = backfill_universe(
        [], strategy=strat, insert_rows=insert, taxonomy_version="v", version=1
    )
    assert report == BackfillReport(ingested=(), skipped=(), failures=(), version=1)
    assert strat.calls == []
    assert batches == []


# --- purity guard --------------------------------------------------------------


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


def test_core_backfill_is_pure():
    """The engine imports no `edgar`, ClickHouse, or `pyarrow` — the strategy
    fetches and the store inserts via injected ports; core only orchestrates."""
    imports = _module_imports("fintin/core/backfill.py")
    assert "edgar" not in imports
    assert "clickhouse_connect" not in imports
    assert "pyarrow" not in imports
