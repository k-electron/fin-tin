"""Live ClickHouse connection tests (require a running container).

Auto-skipped when ClickHouse is unreachable (see conftest.py).
"""

from __future__ import annotations

import uuid

import pytest

from fintin.adapters.store.client import check_connection, get_client


@pytest.mark.integration
def test_check_connection_live(local_clickhouse_config):
    version = check_connection(local_clickhouse_config)
    assert version and version != "unknown"


@pytest.mark.integration
def test_read_write_round_trip(local_clickhouse_config):
    """Verify the store adapter can create/insert/read, then clean up.

    Uses a unique table name so parallel/interrupted runs can't collide, and
    asserts the exact value written. (AC-4's full stop/restart persistence
    check is a manual procedure documented in the README — a single pytest run
    cannot restart the container.)
    """
    table = f"fintin_smoke_{uuid.uuid4().hex}"
    client = get_client(local_clickhouse_config)
    try:
        client.command(f"CREATE TABLE {table} (x UInt8) ENGINE = MergeTree ORDER BY x")
        client.command(f"INSERT INTO {table} VALUES (42)")
        rows = client.query(f"SELECT x FROM {table}").result_rows
        assert rows[0][0] == 42
    finally:
        client.command(f"DROP TABLE IF EXISTS {table}")
        client.close()
