"""Per-company backfill strategy tests (offline; never hits live EDGAR).

`CompanyFactsStrategy.company_facts` is tested with a fake `EdgarClient` (its
`.run(op)` just calls `op()`) and a monkeypatched `edgar.get_company_facts` — no
network is touched (NFR-7).
"""

from __future__ import annotations

import pytest

import edgar
from fintin.adapters.edgar.backfill import CompanyFactsStrategy
from fintin.adapters.edgar.facts import NoCompanyFactsError
from fintin.core.backfill import BackfillStrategy


class _FakeClient:
    """Stand-in for EdgarClient — runs the operation directly (no throttle)."""

    def __init__(self):
        self.descriptions: list[str] = []

    def run(self, op, description=""):
        self.descriptions.append(description)
        return op()


def test_conforms_to_backfill_strategy_protocol():
    # AC-5: the concrete strategy satisfies the pluggable interface structurally.
    strat = CompanyFactsStrategy(_FakeClient())
    assert isinstance(strat, BackfillStrategy)
    assert strat.name == "per-company"


def test_company_facts_returns_facts_through_the_client(monkeypatch):
    sentinel = ["fact-a", "fact-b"]  # any non-None iterable stands in for EntityFacts
    monkeypatch.setattr(edgar, "get_company_facts", lambda c: sentinel)
    client = _FakeClient()
    strat = CompanyFactsStrategy(client)
    assert list(strat.company_facts(320193)) == sentinel
    # Routed through the rate-limited client (AD-3), with a descriptive label.
    assert client.descriptions and "320193" in client.descriptions[0]


def test_company_facts_raises_no_company_facts_on_none(monkeypatch):
    # EDGAR returns None (not an exception) for an unknown/factless CIK; the
    # strategy surfaces NoCompanyFactsError, which the engine records as a gap.
    monkeypatch.setattr(edgar, "get_company_facts", lambda c: None)
    strat = CompanyFactsStrategy(_FakeClient())
    with pytest.raises(NoCompanyFactsError):
        list(strat.company_facts(999999))
