"""raw_fact insert + idempotency tests (require a running ClickHouse container).

Each test runs against a unique throwaway database (Story 1.2 pattern), so the
real `default` DB is never touched. Auto-skipped when ClickHouse isn't listening
(see conftest.py). No EDGAR/network involved — rows are built in-memory.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import date

import pytest
from edgar.entity.models import FinancialFact

from fintin.adapters.store import schema as store_schema
from fintin.adapters.store.client import get_client
from fintin.adapters.store.raw_fact_repo import (
    high_water_mark,
    insert_raw_facts,
    next_ingest_version,
    present_accessions,
    present_ciks,
)
from fintin.core.ingest import RawFactRow, to_raw_fact_rows


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


def _row(
    *,
    raw_tag="us-gaap:Revenues",
    value=1000.0,
    version=1,
    content_hash="h1",
    cik=320193,
    accession="0000320193-24-000123",
    filed_date=date(2024, 2, 1),
) -> RawFactRow:
    return RawFactRow(
        cik=cik,
        accession=accession,
        raw_tag=raw_tag,
        raw_label="Revenues",
        taxonomy="us-gaap",
        period_start=date(2023, 1, 1),
        period_end=date(2023, 12, 31),
        unit="USD",
        value=value,
        form="10-K",
        filed_date=filed_date,
        content_hash=content_hash,
        taxonomy_version="5.43.0",
        version=version,
    )


@pytest.mark.integration
def test_insert_and_read_back(schema_client):
    n = insert_raw_facts(
        schema_client,
        [_row(raw_tag="us-gaap:Revenues", value=1000.0), _row(raw_tag="us-gaap:Assets", value=500.0)],
    )
    assert n == 2
    rows = schema_client.query(
        "SELECT raw_tag, value, taxonomy, taxonomy_version, content_hash "
        "FROM raw_fact FINAL WHERE cik = 320193 ORDER BY raw_tag"
    ).result_rows
    assert len(rows) == 2
    assets, revenues = rows
    assert assets[0] == "us-gaap:Assets" and assets[1] == 500.0
    assert revenues[0] == "us-gaap:Revenues" and revenues[1] == 1000.0
    assert revenues[2] == "us-gaap" and revenues[3] == "5.43.0"


@pytest.mark.integration
def test_empty_insert_is_noop(schema_client):
    assert insert_raw_facts(schema_client, []) == 0
    count = schema_client.query("SELECT count() FROM raw_fact").result_rows[0][0]
    assert count == 0


@pytest.mark.integration
def test_reingest_same_cik_unchanged_on_read(schema_client):
    """AC-3: ingesting the same CIK twice leaves Tier 0 unchanged on read —
    identical facts re-inserted with a higher ingest version collapse to the same
    rows under FINAL (idempotent by identity key)."""
    rows = [_row(raw_tag="us-gaap:Revenues", value=1000.0, version=1, content_hash="h")]
    insert_raw_facts(schema_client, rows)
    # Second ingest of the SAME facts, higher ingest-monotonic version.
    rows_v2 = [_row(raw_tag="us-gaap:Revenues", value=1000.0, version=2, content_hash="h")]
    insert_raw_facts(schema_client, rows_v2)

    final = schema_client.query(
        "SELECT count(), any(value) FROM raw_fact FINAL WHERE cik = 320193"
    ).result_rows[0]
    assert final[0] == 1  # one row per identity key, not two
    assert final[1] == 1000.0  # value unchanged


@pytest.mark.integration
def test_corrected_reingest_higher_version_supersedes(schema_client):
    """A recovery re-ingest (same identity, higher version, corrected value)
    supersedes the prior value at the query surface (AD-6), including after a
    background merge."""
    insert_raw_facts(schema_client, [_row(value=200.0, version=1, content_hash="v1")])
    insert_raw_facts(schema_client, [_row(value=100.0, version=2, content_hash="v2")])

    def resolved():
        return schema_client.query(
            "SELECT value FROM raw_fact FINAL WHERE cik = 320193 AND raw_tag = 'us-gaap:Revenues'"
        ).result_rows[0][0]

    assert resolved() == 100.0  # higher version wins pre-merge
    schema_client.command("OPTIMIZE TABLE raw_fact FINAL")
    assert resolved() == 100.0  # ...and stays correct across a merge


@pytest.mark.integration
def test_next_ingest_version_is_monotonic(schema_client):
    """AD-6: version comes from max(version)+1 in the store, so a re-ingest always
    supersedes — independent of the wall clock."""
    assert next_ingest_version(schema_client) == 1  # empty table
    insert_raw_facts(schema_client, [_row(version=5, content_hash="a")])
    assert next_ingest_version(schema_client) == 6
    insert_raw_facts(schema_client, [_row(raw_tag="us-gaap:Assets", version=6, content_hash="b")])
    assert next_ingest_version(schema_client) == 7


@pytest.mark.integration
def test_transform_to_insert_roundtrip(schema_client):
    """End-to-end seam (offline): FinancialFact -> to_raw_fact_rows -> insert ->
    read back via FINAL. Exercises real date/float round-tripping of transform
    output through ClickHouse (not hand-built rows)."""
    facts = [
        FinancialFact(
            concept="us-gaap:Revenues", taxonomy="us-gaap", label="Revenues",
            value=123456.0, numeric_value=123456.0, unit="USD",
            period_start=date(2023, 1, 1), period_end=date(2023, 12, 31),
            period_type="duration", filing_date=date(2024, 2, 1),
            form_type="10-K", accession="0000320193-24-000123",
        ),
        FinancialFact(
            concept="us-gaap:Assets", taxonomy="us-gaap", label="Assets",
            value=999.0, numeric_value=999.0, unit="USD",
            period_start=None, period_end=date(2023, 12, 31),
            period_type="instant", filing_date=date(2024, 2, 1),
            form_type="10-K", accession="0000320193-24-000123",
        ),
    ]
    version = next_ingest_version(schema_client)
    rows, result = to_raw_fact_rows(facts, cik=320193, taxonomy_version="5.43.0", version=version)
    assert insert_raw_facts(schema_client, rows) == result.rows_landed == 2

    got = schema_client.query(
        "SELECT raw_tag, value, period_start, period_end, filed_date, version "
        "FROM raw_fact FINAL WHERE cik = 320193 ORDER BY raw_tag"
    ).result_rows
    assert len(got) == 2
    assets, revenues = got
    assert assets[0] == "us-gaap:Assets" and assets[2] == assets[3] == date(2023, 12, 31)  # instant
    assert revenues[0] == "us-gaap:Revenues" and revenues[1] == 123456.0  # actual, unscaled
    assert revenues[2] == date(2023, 1, 1) and revenues[3] == date(2023, 12, 31)  # duration
    assert revenues[5] == version


# --- work-list membership + HWM (Story 2.2) ------------------------------------


@pytest.mark.integration
def test_high_water_mark_none_on_empty(schema_client):
    assert high_water_mark(schema_client) is None  # empty store -> None (not 1970)


@pytest.mark.integration
def test_high_water_mark_returns_latest_filed_date(schema_client):
    insert_raw_facts(
        schema_client,
        [
            _row(accession="0000000001-24-000001", filed_date=date(2024, 2, 1), content_hash="a"),
            _row(accession="0000000001-24-000002", filed_date=date(2024, 5, 15), content_hash="b"),
        ],
    )
    assert high_water_mark(schema_client) == date(2024, 5, 15)


@pytest.mark.integration
def test_present_accessions_returns_present_subset(schema_client):
    # Membership by EXACT accession (AD-16), independent of cik/filed_date.
    insert_raw_facts(
        schema_client,
        [
            _row(accession="0000320193-24-000001", filed_date=date(2024, 5, 1), content_hash="a"),
            _row(accession="0000320193-23-000009", filed_date=date(2023, 1, 1), content_hash="b"),
        ],
    )
    # Query a candidate set: two present, one absent.
    present = present_accessions(
        schema_client,
        accessions={"0000320193-24-000001", "0000320193-23-000009", "0000999999-24-000000"},
    )
    assert present == {"0000320193-24-000001", "0000320193-23-000009"}  # absent one excluded


@pytest.mark.integration
def test_present_accessions_empty_is_empty_no_query(schema_client):
    insert_raw_facts(schema_client, [_row(content_hash="a")])
    assert present_accessions(schema_client, accessions=[]) == set()


# --- present_ciks: per-company membership for resumable backfill (Story 2.3) ----


@pytest.mark.integration
def test_present_ciks_returns_present_subset(schema_client):
    # Per-company membership (AD-16): a CIK with ≥1 row is "present" (done).
    insert_raw_facts(
        schema_client,
        [
            _row(cik=320193, accession="0000320193-24-000001", content_hash="a"),
            _row(cik=789019, accession="0000789019-24-000002", content_hash="b"),
        ],
    )
    # Query a scope of three: two present, one absent.
    present = present_ciks(schema_client, ciks={320193, 789019, 111111})
    assert present == {320193, 789019}  # the absent CIK is excluded


@pytest.mark.integration
def test_present_ciks_empty_is_empty_no_query(schema_client):
    insert_raw_facts(schema_client, [_row(content_hash="a")])
    assert present_ciks(schema_client, ciks=[]) == set()


def test_present_ciks_empty_short_circuits_without_a_query():
    # No container needed: the empty-input guard must return set() WITHOUT issuing
    # a query (a fake client whose .query raises proves the short-circuit).
    class _NoQuery:
        def query(self, *a, **k):
            raise AssertionError("present_ciks must not query for empty ciks")

    assert present_ciks(_NoQuery(), ciks=[]) == set()
