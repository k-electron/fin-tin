"""Live ClickHouse connection tests (require a running container).

Auto-skipped when ClickHouse is unreachable (see conftest.py).
"""

from __future__ import annotations

import pytest

from fintin.adapters.store.client import check_connection, get_client


@pytest.mark.integration
def test_check_connection_live(local_clickhouse_config):
    version = check_connection(local_clickhouse_config)
    assert version and version != "unknown"


@pytest.mark.integration
def test_read_write_round_trip(local_clickhouse_config):
    """Verify the store adapter can create/insert/read, then clean up.

    (AC-4's full stop/restart persistence check is a manual step documented in
    the README; this confirms the read/write path works.)
    """
    client = get_client(local_clickhouse_config)
    client.command(
        "CREATE TABLE IF NOT EXISTS fintin_smoke (x UInt8) "
        "ENGINE = MergeTree ORDER BY x"
    )
    try:
        client.command("INSERT INTO fintin_smoke VALUES (1)")
        rows = client.query("SELECT count() FROM fintin_smoke").result_rows
        assert rows[0][0] >= 1
    finally:
        client.command("DROP TABLE IF EXISTS fintin_smoke")
