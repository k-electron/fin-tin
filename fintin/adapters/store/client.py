"""ClickHouse connection for the store adapter.

The store adapter is the sole component that talks to ClickHouse (AD-18). This
module provides the client factory and a lightweight connection check. No
schema/DDL is issued here — that is Story 1.2.
"""

from __future__ import annotations

import contextlib

import clickhouse_connect

from fintin.config import ClickHouseConfig


class StoreConnectionError(Exception):
    """Raised when the store adapter cannot connect to (or authenticate with) ClickHouse."""


def get_client(
    cfg: ClickHouseConfig,
    *,
    connect_timeout: int | None = None,
    database: str | None = None,
):
    """Build a clickhouse-connect client from config.

    ``database`` overrides ``cfg.database`` (used for test isolation against a
    throwaway database). Callers own the client's lifecycle — close it when done
    (or use :func:`check_connection`, which closes its own client).
    clickhouse-connect performs a handshake on construction, so this can raise if
    the server is unreachable or rejects auth.
    """
    kwargs: dict = dict(
        host=cfg.host,
        port=cfg.port,
        username=cfg.username,
        password=cfg.password,
        database=database if database is not None else cfg.database,
    )
    if connect_timeout is not None:
        kwargs["connect_timeout"] = connect_timeout
    return clickhouse_connect.get_client(**kwargs)


def check_connection(cfg: ClickHouseConfig, *, connect_timeout: int | None = 5) -> str:
    """Open a client, run a trivial round-trip, and close it.

    Returns the server version string on success; raises
    :class:`StoreConnectionError` on any failure (server unreachable, auth
    failure, wrong database, unexpected response). The client is always closed.
    """
    client = None
    try:
        client = get_client(cfg, connect_timeout=connect_timeout)
        rows = client.query("SELECT 1").result_rows
        if not rows or rows[0][0] != 1:
            raise StoreConnectionError(
                f"Unexpected response from ClickHouse at {cfg.host}:{cfg.port}."
            )
        version_rows = client.query("SELECT version()").result_rows
        return str(version_rows[0][0]) if version_rows else "unknown"
    except StoreConnectionError:
        raise
    except Exception as exc:  # clickhouse-connect raises various driver errors
        raise StoreConnectionError(
            f"Cannot connect to ClickHouse at {cfg.host}:{cfg.port} "
            f"(database={cfg.database!r}, user={cfg.username!r}) — server unreachable, "
            f"or wrong credentials/database: {exc}"
        ) from exc
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()
