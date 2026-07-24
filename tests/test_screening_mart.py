"""Screening-mart resolution tests (require a running ClickHouse container).

Story 1.6 / Approach B: the wide mart resolves each screening concept to the
LATEST-FILED value across its element union, tie-broken by element list-position,
computed on read over `canonical_fact FINAL`. Throwaway-DB per test; synthetic
`canonical_fact` inserts — NEVER live EDGAR (NFR-7).
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import date

import pytest

from fintin.adapters.store import schema as store_schema
from fintin.adapters.store.canonical_fact_repo import insert_canonical_facts
from fintin.adapters.store.client import get_client
from fintin.core.canonical import CanonicalFactRow


@pytest.fixture
def schema_client(local_clickhouse_config):
    db = f"fintin_test_{uuid.uuid4().hex[:12]}"
    admin = get_client(local_clickhouse_config)
    try:
        admin.command(f"CREATE DATABASE {db}")
    finally:
        admin.close()
    client = None
    try:
        client = get_client(local_clickhouse_config, database=db)
        store_schema.create_schema(client)
        yield client
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()
        cleanup = get_client(local_clickhouse_config)
        try:
            cleanup.command(f"DROP DATABASE IF EXISTS {db}")
        finally:
            cleanup.close()


def _fact(**over) -> CanonicalFactRow:
    """A duration us-gaap:Revenues fact for CY2023, USD. Override per test."""
    base = dict(
        cik=100,
        accession="0000000100-24-000001",
        raw_tag="us-gaap:Revenues",
        canonical_concept="Revenues",
        raw_label="Revenues",
        period_start=date(2023, 1, 1),
        period_end=date(2023, 12, 31),
        unit="USD",
        value=500.0,
        form="10-K",
        filed_date=date(2024, 2, 1),
        content_hash="h",
        taxonomy_version="5.43.0",
        version=1,
    )
    base.update(over)
    return CanonicalFactRow(**base)


def _revenues(client, cik=100):
    rows = client.query(
        f"SELECT revenues FROM screening_mart WHERE cik = {cik} "
        "AND period_start = '2023-01-01' AND period_end = '2023-12-31'"
    ).result_rows
    return rows[0][0] if rows else None


# --- AC-1/AC-2: the product-defining restatement test (SM-1) ---------------------


@pytest.mark.integration
def test_restatement_newer_filing_wins(schema_client):
    """SM-1 (REQUIRED): two filings of one period with different filed_date and
    differing values → the mart returns the most-recently-filed value."""
    insert_canonical_facts(
        schema_client,
        [
            _fact(accession="0000000100-24-000001", value=500.0, filed_date=date(2024, 2, 1), content_hash="a"),
            # restatement, filed later, corrected value
            _fact(accession="0000000100-25-000001", value=550.0, filed_date=date(2025, 2, 1),
                  form="10-K", content_hash="b"),
        ],
    )
    assert _revenues(schema_client) == 550.0  # newer filed_date wins


@pytest.mark.integration
def test_amendment_wins_on_equal_filed_date(schema_client):
    """AD-7 tiebreak: on an equal filed_date, the /A amendment wins."""
    insert_canonical_facts(
        schema_client,
        [
            _fact(accession="0000000100-24-000001", value=100.0, filed_date=date(2024, 2, 1),
                  form="10-K", content_hash="a"),
            _fact(accession="0000000100-24-000002", value=175.0, filed_date=date(2024, 2, 1),
                  form="10-K/A", content_hash="b"),
        ],
    )
    assert _revenues(schema_client) == 175.0


# --- AC-3: recency-aware resolution across a concept's element union -------------


@pytest.mark.integration
def test_recency_beats_position_across_elements(schema_client):
    """A period reported under a LOWER-precedence element (`Revenues`, list pos 2)
    in a NEWER filing beats a HIGHER-precedence element
    (`RevenueFromContractWithCustomerExcludingAssessedTax`, pos 1) in an OLDER
    filing — recency wins across the element union (the finding the pivot's
    position-first stopgap got wrong)."""
    insert_canonical_facts(
        schema_client,
        [
            _fact(raw_tag="us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                  canonical_concept="RevenueFromContractWithCustomerExcludingAssessedTax",
                  accession="0000000100-24-000001", value=800.0, filed_date=date(2024, 2, 1), content_hash="a"),
            _fact(raw_tag="us-gaap:Revenues", canonical_concept="Revenues",
                  accession="0000000100-25-000001", value=900.0, filed_date=date(2025, 2, 1), content_hash="b"),
        ],
    )
    assert _revenues(schema_client) == 900.0  # newer filing wins despite lower list position


@pytest.mark.integration
def test_same_filing_position_tiebreak_deterministic(schema_client):
    """When two of a concept's elements appear in the SAME filing (identical
    filed_date/accession/form/version), the higher list-position element wins
    deterministically — the collision the AD-9 pivot + this rank retire."""
    insert_canonical_facts(
        schema_client,
        [
            _fact(raw_tag="us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                  canonical_concept="RevenueFromContractWithCustomerExcludingAssessedTax",
                  accession="0000000100-24-000001", value=900.0, filed_date=date(2024, 2, 1), content_hash="a"),
            _fact(raw_tag="us-gaap:Revenues", canonical_concept="Revenues",
                  accession="0000000100-24-000001", value=950.0, filed_date=date(2024, 2, 1), content_hash="b"),
        ],
    )
    # RevenueFromContract... is list position 1 → wins the tie, deterministically.
    assert _revenues(schema_client) == 900.0
    schema_client.command("OPTIMIZE TABLE canonical_fact FINAL")
    assert _revenues(schema_client) == 900.0  # ...and stays deterministic across a merge


# --- AC-4: wide shape, auto-populate, NULL-not-zero, unit pin ---------------------


@pytest.mark.integration
def test_absent_concept_is_null_not_zero(schema_client):
    insert_canonical_facts(schema_client, [_fact(value=500.0)])
    row = schema_client.query(
        "SELECT revenues, net_income, assets FROM screening_mart WHERE cik = 100"
    ).result_rows[0]
    assert row[0] == 500.0
    assert row[1] is None and row[2] is None  # NULL, not 0.0


@pytest.mark.integration
def test_shares_outstanding_resolves_on_shares_unit(schema_client):
    """A non-USD concept: shares_outstanding is pinned to unit='shares'."""
    insert_canonical_facts(
        schema_client,
        [
            _fact(raw_tag="us-gaap:CommonStockSharesOutstanding",
                  canonical_concept="CommonStockSharesOutstanding", unit="shares",
                  period_start=date(2023, 12, 31), period_end=date(2023, 12, 31),
                  value=1_500_000.0, content_hash="s"),
        ],
    )
    val = schema_client.query(
        "SELECT shares_outstanding FROM screening_mart "
        "WHERE cik = 100 AND period_start = period_end"
    ).result_rows[0][0]
    assert val == 1_500_000.0


@pytest.mark.integration
def test_wide_one_row_per_period_with_multiple_concepts(schema_client):
    insert_canonical_facts(
        schema_client,
        [
            _fact(canonical_concept="Revenues", raw_tag="us-gaap:Revenues", value=500.0, content_hash="r"),
            _fact(canonical_concept="NetIncomeLoss", raw_tag="us-gaap:NetIncomeLoss", value=90.0, content_hash="n"),
        ],
    )
    rows = schema_client.query(
        "SELECT revenues, net_income FROM screening_mart WHERE cik = 100 "
        "AND period_start = '2023-01-01' AND period_end = '2023-12-31'"
    ).result_rows
    assert len(rows) == 1 and rows[0][0] == 500.0 and rows[0][1] == 90.0


# --- AC-5 / AC-6: SQL screen + performance tripwire -------------------------------


@pytest.mark.integration
def test_sql_screen_returns_matching_companies(schema_client):
    """AC-5: a screen (concept > threshold for a period) returns the matching
    company-period rows."""
    facts = []
    for cik, rev in [(1, 100.0), (2, 500.0), (3, 900.0)]:
        facts.append(
            _fact(cik=cik, accession=f"{cik:010d}-24-000001", value=rev, content_hash=f"c{cik}")
        )
    insert_canonical_facts(schema_client, facts)
    hits = schema_client.query(
        "SELECT cik FROM screening_mart "
        "WHERE period_start = '2023-01-01' AND period_end = '2023-12-31' "
        "AND revenues > 400 ORDER BY cik"
    ).result_rows
    assert [r[0] for r in hits] == [2, 3]


@pytest.mark.integration
def test_cross_sectional_screen_returns(schema_client):
    """AC-6 (soft NFR-3 tripwire): a cross-sectional screen over a seeded multi-company
    set completes and returns rows. Not a hard-time assertion (flaky); just a guard
    that the derive-on-read mart runs a market-wide screen without error."""
    facts = [
        _fact(cik=cik, accession=f"{cik:010d}-24-000001", value=float(cik * 10), content_hash=f"c{cik}")
        for cik in range(1, 51)
    ]
    insert_canonical_facts(schema_client, facts)
    hits = schema_client.query(
        "SELECT count() FROM screening_mart "
        "WHERE period_start = '2023-01-01' AND period_end = '2023-12-31' AND revenues > 100"
    ).result_rows[0][0]
    assert hits == 40  # ciks 11..50 have revenues > 100


# --- form primacy + accession tiebreak (review patches) --------------------------


@pytest.mark.integration
def test_non_periodic_form_excluded(schema_client):
    """A later-filed 8-K must NOT override the audited 10-K/10-Q — the mart restricts
    to periodic forms, so recency can't pull in a non-authoritative value."""
    insert_canonical_facts(
        schema_client,
        [
            _fact(accession="0000000100-24-000001", value=500.0, filed_date=date(2024, 2, 1),
                  form="10-K", content_hash="k"),
            _fact(accession="0000000100-24-000009", value=999.0, filed_date=date(2024, 6, 1),
                  form="8-K", content_hash="e"),  # newer, but non-periodic
        ],
    )
    assert _revenues(schema_client) == 500.0  # 8-K excluded despite being newer


@pytest.mark.integration
def test_greatest_accession_tiebreak(schema_client):
    """AC-1: on an equal filed_date with neither row a /A, the greatest accession
    wins (isolates the accession tiebreak — both non-/A, same date)."""
    insert_canonical_facts(
        schema_client,
        [
            _fact(accession="0000000100-24-000001", value=100.0, filed_date=date(2024, 2, 1),
                  form="10-K", content_hash="a"),
            _fact(accession="0000000100-24-000002", value=200.0, filed_date=date(2024, 2, 1),
                  form="10-K", content_hash="b"),
        ],
    )
    assert _revenues(schema_client) == 200.0  # greater accession wins


# --- screening_wide: cross-statement (flow + stock) in one row -------------------


@pytest.mark.integration
def test_screening_wide_joins_flow_and_stock(schema_client):
    """The base mart keeps income (duration) and balance-sheet (instant) facts in
    separate rows; screening_wide joins the balance sheet AS OF period_end onto the
    income row, so a single screen can mix flows and stocks (ROA, leverage, ...)."""
    insert_canonical_facts(
        schema_client,
        [
            _fact(canonical_concept="Revenues", raw_tag="us-gaap:Revenues", value=1000.0,
                  period_start=date(2023, 1, 1), period_end=date(2023, 12, 31), content_hash="rev"),
            _fact(canonical_concept="NetIncomeLoss", raw_tag="us-gaap:NetIncomeLoss", value=120.0,
                  period_start=date(2023, 1, 1), period_end=date(2023, 12, 31), content_hash="ni"),
            _fact(canonical_concept="Assets", raw_tag="us-gaap:Assets", value=5000.0,
                  period_start=date(2023, 12, 31), period_end=date(2023, 12, 31), content_hash="as"),  # instant
        ],
    )
    row = schema_client.query(
        "SELECT revenues, net_income, assets FROM screening_wide "
        "WHERE cik = 100 AND period_start = '2023-01-01' AND period_end = '2023-12-31'"
    ).result_rows
    assert len(row) == 1
    assert row[0] == (1000.0, 120.0, 5000.0)  # flow + stock in ONE row
    # ...and a cross-statement screen (mixing a flow and a stock) returns it.
    hits = schema_client.query(
        "SELECT cik FROM screening_wide WHERE revenues > 500 AND assets > 1000"
    ).result_rows
    assert [r[0] for r in hits] == [100]
