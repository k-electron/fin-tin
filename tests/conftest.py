"""Shared test fixtures and integration gating.

Integration tests (marked ``@pytest.mark.integration``) require a running
ClickHouse container. They are auto-skipped only when nothing is listening at
the configured host:port, so the default ``uv run pytest`` stays green without
Docker. If the server *is* listening but rejects the connection (wrong
password/database), the integration tests are NOT skipped — they run and fail
loudly, surfacing the misconfiguration instead of hiding it behind a skip.

Connection details are read from the project's ``fintin.toml`` so tests never
drift from the real config.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from fintin.config import ClickHouseConfig, ConfigError, load_config

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "fintin.toml"


def _local_config() -> ClickHouseConfig | None:
    try:
        return load_config(_CONFIG_PATH).clickhouse
    except ConfigError:
        return None


def _server_listening(cfg: ClickHouseConfig | None, timeout: float = 2.0) -> bool:
    """Cheap, timed reachability probe — a plain TCP connect, no ClickHouse
    client (so no connection pool to leak and no hang on a half-up server)."""
    if cfg is None:
        return False
    try:
        with socket.create_connection((cfg.host, cfg.port), timeout=timeout):
            return True
    except OSError:
        return False


def pytest_collection_modifyitems(config, items):
    integration_items = [it for it in items if "integration" in it.keywords]
    if not integration_items:
        return  # unit-only run — never touch the network
    if _server_listening(_local_config()):
        return  # reachable — run them (they fail loudly on auth/db misconfig)
    skip = pytest.mark.skip(
        reason="ClickHouse not listening at configured host:port (see fintin.toml)"
    )
    for item in integration_items:
        item.add_marker(skip)


@pytest.fixture
def local_clickhouse_config() -> ClickHouseConfig:
    cfg = _local_config()
    if cfg is None:
        pytest.skip("fintin.toml not loadable")
    return cfg
