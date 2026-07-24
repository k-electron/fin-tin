"""Schema DDL tests (require a running ClickHouse container).

Each test runs against a unique throwaway database so the real `default` DB is
never touched. Auto-skipped when ClickHouse isn't listening (see conftest.py).
"""

from __future__ import annotations

import uuid

import pytest

from fintin.adapters.store import schema as store_schema
from fintin.adapters.store.client import get_client

_COLS = (
    "cik, accession, raw_tag, canonical_concept, raw_label, period_start, "
    "period_end, unit, value, form, filed_date, content_hash, taxonomy_version, version"
)


@pytest.fixture
def schema_client(local_clickhouse_config):
    """Create a throwaway database, yield a client pointed at it, drop it after."""
    db = f"fintin_test_{uuid.uuid4().hex[:12]}"
    admin = get_client(local_clickhouse_config)
    admin.command(f"CREATE DATABASE {db}")
    admin.close()
    client = get_client(local_clickhouse_config, database=db)
    try:
        yield client, db
    finally:
        client.close()
        admin2 = get_client(local_clickhouse_config)
        admin2.command(f"DROP DATABASE IF EXISTS {db}")
        admin2.close()


@pytest.mark.integration
def test_tier0_tier1_engines_and_key(schema_client):
    """AC-1: Tier 0/1 are ReplacingMergeTree on the identity key with UInt64 version."""
    client, db = schema_client
    store_schema.create_schema(client)
    for table in ("raw_fact", "canonical_fact"):
        rows = client.query(
            f"SELECT engine, engine_full, sorting_key FROM system.tables "
            f"WHERE database = '{db}' AND name = '{table}'"
        ).result_rows
        assert rows, f"{table} not created"
        engine, engine_full, sorting_key = rows[0]
        assert engine == "ReplacingMergeTree"
        assert "version" in engine_full  # ReplacingMergeTree(version) — ingest-monotonic
        assert sorting_key == "accession, raw_tag, period_start, period_end, unit"
        vcol = client.query(
            f"SELECT type FROM system.columns "
            f"WHERE database = '{db}' AND table = '{table}' AND name = 'version'"
        ).result_rows
        assert vcol and vcol[0][0] == "UInt64"


@pytest.mark.integration
def test_resolution_and_mart_created(schema_client):
    """AC-2: resolution MV (AggregatingMergeTree) + wide mart exist."""
    client, db = schema_client
    store_schema.create_schema(client)
    engine = client.query(
        f"SELECT engine FROM system.tables WHERE database = '{db}' AND name = 'resolved_fact'"
    ).result_rows
    assert engine and engine[0][0] == "AggregatingMergeTree"
    names = {
        r[0]
        for r in client.query(
            f"SELECT name FROM system.tables WHERE database = '{db}'"
        ).result_rows
    }
    assert {"resolved_fact_mv", "screening_mart"} <= names


@pytest.mark.integration
def test_schema_init_is_idempotent(schema_client):
    """AC-3: running create_schema twice is a no-op (no error, no duplicates)."""
    client, db = schema_client
    store_schema.create_schema(client)
    before = client.query(
        f"SELECT count() FROM system.tables WHERE database = '{db}'"
    ).result_rows[0][0]
    store_schema.create_schema(client)  # second run
    after = client.query(
        f"SELECT count() FROM system.tables WHERE database = '{db}'"
    ).result_rows[0][0]
    assert before == after == 5  # 4 tables + 1 view; MV uses TO (no inner table)


@pytest.mark.integration
def test_instant_and_duration_representation(schema_client):
    """AC-4: instant facts store period_start == period_end; duration start < end,
    and both flow through the MV into the wide mart."""
    client, db = schema_client
    store_schema.create_schema(client)
    client.command(
        f"INSERT INTO canonical_fact ({_COLS}) VALUES "
        "(320193,'0000320193-24-000001','us-gaap:Assets','Assets','Assets',"
        "'2024-09-28','2024-09-28','USD',100.0,'10-K','2024-11-01','h1','1',1),"
        "(320193,'0000320193-24-000001','us-gaap:Revenues','Revenues','Revenues',"
        "'2023-10-01','2024-09-28','USD',400.0,'10-K','2024-11-01','h2','1',1)"
    )
    inst = client.query(
        "SELECT period_start, period_end FROM canonical_fact "
        "WHERE canonical_concept = 'Assets'"
    ).result_rows[0]
    assert inst[0] == inst[1]  # instant
    dur = client.query(
        "SELECT period_start, period_end FROM canonical_fact "
        "WHERE canonical_concept = 'Revenues'"
    ).result_rows[0]
    assert dur[0] < dur[1]  # duration

    # instant Assets resolves in its own period row
    assets = client.query(
        "SELECT assets FROM screening_mart "
        "WHERE cik = 320193 AND period_start = '2024-09-28' AND period_end = '2024-09-28'"
    ).result_rows
    assert assets and assets[0][0] == 100.0
    # duration Revenues resolves in its own period row
    revenues = client.query(
        "SELECT revenues FROM screening_mart "
        "WHERE cik = 320193 AND period_start = '2023-10-01' AND period_end = '2024-09-28'"
    ).result_rows
    assert revenues and revenues[0][0] == 400.0


@pytest.mark.integration
def test_latest_filed_wins_smoke(schema_client):
    """Pre-echo of Story 1.6: newer filed_date wins for the same period."""
    client, db = schema_client
    store_schema.create_schema(client)
    client.command(
        f"INSERT INTO canonical_fact ({_COLS}) VALUES "
        "(1,'0000000001-24-000001','us-gaap:Revenues','Revenues','Revenues',"
        "'2023-01-01','2023-03-31','USD',100.0,'10-Q','2023-05-01','a','1',1),"
        "(1,'0000000001-24-000002','us-gaap:Revenues','Revenues','Revenues',"
        "'2023-01-01','2023-03-31','USD',150.0,'10-Q/A','2024-05-01','b','1',1)"
    )
    val = client.query(
        "SELECT revenues FROM screening_mart "
        "WHERE cik = 1 AND period_start = '2023-01-01' AND period_end = '2023-03-31'"
    ).result_rows[0][0]
    assert val == 150.0  # newer filed_date wins
