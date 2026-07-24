"""Shared test fixtures and integration gating.

Integration tests (marked ``@pytest.mark.integration``) require a running
ClickHouse container. They are auto-skipped when ClickHouse is unreachable, so
the default ``uv run pytest`` stays green without Docker.

Connection details are read from the project's ``fintin.toml`` so tests never
drift from the real config.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fintin.adapters.store.client import StoreConnectionError, check_connection
from fintin.config import ClickHouseConfig, ConfigError, load_config

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "fintin.toml"


def _local_config() -> ClickHouseConfig | None:
    try:
        return load_config(_CONFIG_PATH).clickhouse
    except ConfigError:
        return None


def _clickhouse_reachable(cfg: ClickHouseConfig | None) -> bool:
    if cfg is None:
        return False
    try:
        check_connection(cfg)
        return True
    except StoreConnectionError:
        return False


def pytest_collection_modifyitems(config, items):
    if _clickhouse_reachable(_local_config()):
        return
    skip = pytest.mark.skip(reason="ClickHouse not reachable (see fintin.toml)")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def local_clickhouse_config() -> ClickHouseConfig:
    cfg = _local_config()
    if cfg is None:
        pytest.skip("fintin.toml not loadable")
    return cfg
