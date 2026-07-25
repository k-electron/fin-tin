"""Pure work-list reconciler tests (no container, no network)."""

from __future__ import annotations

from datetime import date

from fintin.core.reconcile import (
    WorkItem,
    WorkList,
    compute_work_list,
    resolve_window,
)
from tests.purity import assert_module_is_pure


def _wi(accession: str, cik: int = 320193, form: str = "10-K", filed="2024-02-01"):
    return WorkItem(accession, cik, form, date.fromisoformat(filed))


# --- resolve_window (AD-16) ----------------------------------------------------


def test_window_anchored_at_hwm_when_present():
    start, end = resolve_window(date(2024, 3, 31), lookback_days=7, today=date(2024, 6, 1))
    assert start == date(2024, 3, 24)  # hwm - 7d (scan-sizing hint)
    assert end == date(2024, 6, 1)  # today


def test_window_anchored_at_today_when_store_empty():
    start, end = resolve_window(None, lookback_days=7, today=date(2024, 6, 1))
    assert start == date(2024, 5, 25)  # today - 7d (bounded recent window)
    assert end == date(2024, 6, 1)


def test_window_lookback_scales():
    start, _ = resolve_window(date(2024, 6, 1), lookback_days=30, today=date(2024, 6, 1))
    assert start == date(2024, 5, 2)


def test_window_clamps_future_dated_hwm():
    # A corrupt future-dated HWM must not reverse the window (start <= end).
    start, end = resolve_window(date(2025, 1, 1), lookback_days=7, today=date(2024, 6, 1))
    assert end == date(2024, 6, 1)
    assert start == date(2024, 5, 25)  # anchored at today, not the future hwm
    assert start <= end


# --- compute_work_list (AC-1, AC-2, AC-3) --------------------------------------


def test_present_accession_excluded_not_refetched():
    # AC-2: an accession already in the store is not in the work list.
    candidates = [_wi("0000000001-24-000001"), _wi("0000000001-24-000002")]
    work = compute_work_list(candidates, {"0000000001-24-000001"})
    assert [i.accession for i in work.items] == ["0000000001-24-000002"]
    assert work.scanned == 2
    assert work.already_present == 1


def test_new_and_amendment_accessions_included():
    # AC-3: a newly-filed accession (incl. a /A amendment) not yet present appears.
    candidates = [
        _wi("0000000001-24-000009", form="10-K", filed="2024-02-01"),
        _wi("0000000001-24-000010", form="10-K/A", filed="2024-05-01"),  # restatement
    ]
    work = compute_work_list(candidates, present_accessions=set())
    accs = {i.accession for i in work.items}
    assert accs == {"0000000001-24-000009", "0000000001-24-000010"}
    assert any(i.form == "10-K/A" for i in work.items)


def test_co_filed_duplicate_accessions_deduped_deterministically():
    # The index has one row per (filing, filer); a co-filed accession repeats.
    # Dedup keeps the SMALLEST cik regardless of index row order (deterministic).
    candidates = [
        _wi("0000000001-24-000001", cik=7),  # larger cik listed FIRST
        _wi("0000000001-24-000001", cik=2),  # same accession, smaller cik
    ]
    work = compute_work_list(candidates, present_accessions=set())
    assert len(work.items) == 1  # deduped by accession
    assert work.items[0].cik == 2  # min cik kept, not the first-listed
    assert work.scanned == 1


def test_items_sorted_by_filed_date_then_accession():
    candidates = [
        _wi("0000000001-24-000003", filed="2024-05-01"),
        _wi("0000000001-24-000001", filed="2024-02-01"),
        _wi("0000000001-24-000002", filed="2024-02-01"),
    ]
    work = compute_work_list(candidates, present_accessions=set())
    assert [i.accession for i in work.items] == [
        "0000000001-24-000001",
        "0000000001-24-000002",
        "0000000001-24-000003",
    ]


def test_empty_candidates_is_clean():
    work = compute_work_list([], present_accessions={"x"})
    assert work == WorkList(items=(), scanned=0, already_present=0)


# --- purity guard --------------------------------------------------------------


def test_core_reconcile_is_pure():
    """The reconciler imports nothing impure — the adapters produce its inputs;
    core only windows and diffs."""
    assert_module_is_pure("fintin/core/reconcile.py")
