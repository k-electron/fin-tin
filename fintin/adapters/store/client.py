"""ClickHouse connection for the store adapter.

The store adapter is the sole component that talks to ClickHouse (AD-18). This
module provides the client factory and a lightweight connection check. No
schema/DDL is issued here — that is Story 1.2.
"""

from __future__ import annotations

import clickhouse_connect

from fintin.config import ClickHouseConfig


class StoreConnectionError(Exception):
    """Raised when the store adapter cannot reach ClickHouse."""


def get_client(cfg: ClickHouseConfig):
    """Build a clickhouse-connect client from config.

    Note: clickhouse-connect performs a handshake on construction, so this can
    raise if the server is unreachable — callers that want a clean error should
    use :func:`check_connection`.
    """
    return clickhouse_connect.get_client(
        host=cfg.host,
        port=cfg.port,
        username=cfg.username,
        password=cfg.password,
        database=cfg.database,
    )


def check_connection(cfg: ClickHouseConfig) -> str:
    """Open a client and run a trivial round-trip against ClickHouse.

    Returns the server version string on success; raises
    :class:`StoreConnectionError` on any failure (unreachable server, auth
    failure, unexpected response).
    """
    try:
        client = get_client(cfg)
        result = client.query("SELECT 1").result_rows
        if not result or result[0][0] != 1:
            raise StoreConnectionError(
                f"Unexpected response from ClickHouse at {cfg.host}:{cfg.port}."
            )
        version_rows = client.query("SELECT version()").result_rows
        return str(version_rows[0][0]) if version_rows else "unknown"
    except StoreConnectionError:
        raise
    except Exception as exc:  # clickhouse-connect raises various driver errors
        raise StoreConnectionError(
            f"Cannot reach ClickHouse at {cfg.host}:{cfg.port} "
            f"(database={cfg.database!r}, user={cfg.username!r}): {exc}"
        ) from exc
