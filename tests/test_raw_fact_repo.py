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
    insert_raw_facts,
    next_ingest_version,
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


def _row(*, raw_tag="us-gaap:Revenues", value=1000.0, version=1, content_hash="h1") -> RawFactRow:
    return RawFactRow(
        cik=320193,
        accession="0000320193-24-000123",
        raw_tag=raw_tag,
        raw_label="Revenues",
        taxonomy="us-gaap",
        period_start=date(2023, 1, 1),
        period_end=date(2023, 12, 31),
        unit="USD",
        value=value,
        form="10-K",
        filed_date=date(2024, 2, 1),
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
