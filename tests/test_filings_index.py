"""EDGAR filing-index adapter tests (offline; never hits live EDGAR).

`_filings_to_work_items` is tested against a synthetic pyarrow table; the network
wrapper `fetch_work_candidates` is tested with a fake client + a monkeypatched
`edgar.get_filings` — no network is touched.
"""

from __future__ import annotations

from datetime import date

import pyarrow as pa

import edgar
from fintin.adapters.edgar.filings_index import (
    _filings_to_work_items,
    fetch_work_candidates,
)


def _index_table(rows):
    """Build a synthetic Filings.data table (rows: form, company, cik, filed, accession)."""
    return pa.table(
        {
            "form": pa.array([r[0] for r in rows], pa.string()),
            "company": pa.array([r[1] for r in rows], pa.string()),
            "cik": pa.array([r[2] for r in rows], pa.int32()),
            "filing_date": pa.array([r[3] for r in rows], pa.date32()),
            "accession_number": pa.array([r[4] for r in rows], pa.string()),
        }
    )


_ROWS = [
    ("10-K", "Apple", 320193, date(2024, 2, 1), "0000320193-24-000001"),
    ("10-Q", "Microsoft", 789019, date(2024, 4, 1), "0000789019-24-000002"),
    ("10-K/A", "Apple", 320193, date(2024, 5, 1), "0000320193-24-000003"),
    ("10-K", "NotInUniverse", 111111, date(2024, 2, 1), "0000111111-24-000004"),
]


class _FakeFilings:
    def __init__(self, table):
        self.data = table

    def __len__(self):
        return self.data.num_rows


class _FakeClient:
    """Stand-in for EdgarClient — runs the operation directly (no throttle)."""

    def run(self, op, description=""):
        return op()


def test_filters_to_universe_ciks():
    items = _filings_to_work_items(_index_table(_ROWS), {320193, 789019})
    assert {i.cik for i in items} == {320193, 789019}
    assert 111111 not in {i.cik for i in items}
    assert len(items) == 3  # two Apple (incl /A) + one Microsoft


def test_work_item_fields_and_amendment_kept():
    items = _filings_to_work_items(_index_table(_ROWS), {320193})
    by_acc = {i.accession: i for i in items}
    assert by_acc["0000320193-24-000001"].form == "10-K"
    assert by_acc["0000320193-24-000003"].form == "10-K/A"  # amendment kept (AC-3)
    assert by_acc["0000320193-24-000001"].filed_date == date(2024, 2, 1)
    # dashed 20-char accession preserved verbatim
    assert all("-" in i.accession and len(i.accession) == 20 for i in items)


def test_empty_universe_intersection_yields_nothing():
    assert _filings_to_work_items(_index_table(_ROWS), {999999}) == []


def test_fetch_empty_ciks_short_circuits(monkeypatch):
    # No CIKs → no request at all.
    called = False

    def _boom(*a, **k):
        nonlocal called
        called = True
        raise AssertionError("get_filings should not be called")

    monkeypatch.setattr(edgar, "get_filings", _boom)
    assert fetch_work_candidates(_FakeClient(), filing_date="2024-01-01:2024-06-01", ciks=[]) == []
    assert called is False


def test_fetch_returns_work_items(monkeypatch):
    monkeypatch.setattr(edgar, "get_filings", lambda **k: _FakeFilings(_index_table(_ROWS)))
    items = fetch_work_candidates(
        _FakeClient(), filing_date="2024-01-01:2024-06-01", ciks=[320193, 789019]
    )
    assert {i.cik for i in items} == {320193, 789019}
    assert len(items) == 3


def test_fetch_handles_none(monkeypatch):
    # get_filings returns None on an invalid/out-of-range date.
    monkeypatch.setattr(edgar, "get_filings", lambda **k: None)
    assert fetch_work_candidates(_FakeClient(), filing_date="bad", ciks=[320193]) == []


def test_fetch_handles_empty_filings(monkeypatch):
    # A valid empty period → Filings with len 0.
    monkeypatch.setattr(edgar, "get_filings", lambda **k: _FakeFilings(_index_table([])))
    assert fetch_work_candidates(
        _FakeClient(), filing_date="2024-01-01:2024-01-02", ciks=[320193]
    ) == []
