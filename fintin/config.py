"""Configuration loading for fin-tin.

Loads the single ``fintin.toml`` config file into typed models. Story 1.1
requires only the ``[clickhouse]`` connection block; later stories add
``[universe]``, ``[edgar]``, ``[lease]`` and ``LOOKBACK``, which are tolerated
but not required here.

Any problem loading config raises :class:`ConfigError`, which the CLI boundary
renders as a clear message (never a raw traceback).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("fintin.toml")


class ConfigError(Exception):
    """Raised when the config file is missing, unreadable, or invalid."""


@dataclass(frozen=True)
class ClickHouseConfig:
    host: str
    port: int
    username: str
    password: str
    database: str


@dataclass(frozen=True)
class Config:
    clickhouse: ClickHouseConfig


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    """Load and validate ``fintin.toml`` at ``path``.

    Raises :class:`ConfigError` on a missing/unreadable file, malformed TOML, a
    missing ``[clickhouse]`` section, or missing required connection keys.
    """
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"Config file not found: {path}. "
            f"Create it (see fintin.toml) or pass --config."
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"Cannot read config file {path}: {exc}") from exc
    try:
        # utf-8-sig tolerates an optional BOM (common from Windows editors).
        data = tomllib.loads(raw.decode("utf-8-sig"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"Malformed TOML in {path}: {exc}") from exc

    ch = data.get("clickhouse")
    if not isinstance(ch, dict):
        raise ConfigError(f"Missing required [clickhouse] section in {path}.")
    return Config(clickhouse=_parse_clickhouse(ch, path))


def _parse_clickhouse(ch: dict, path: Path) -> ClickHouseConfig:
    # password may legitimately be an empty string for a local default user,
    # but the key must be present; the others must be non-empty.
    missing = [k for k in ("host", "port", "username", "database")
               if ch.get(k) in (None, "")]
    if "password" not in ch:
        missing.append("password")
    if missing:
        raise ConfigError(
            f"[clickhouse] in {path} is missing required key(s): "
            f"{', '.join(missing)}."
        )

    port = ch["port"]
    # bool is a subclass of int — reject it explicitly so `port = true` is not
    # silently accepted as port 1.
    if isinstance(port, bool) or not isinstance(port, int):
        raise ConfigError(
            f"[clickhouse].port in {path} must be an integer, got {port!r}."
        )
    if not (1 <= port <= 65535):
        raise ConfigError(
            f"[clickhouse].port in {path} must be between 1 and 65535, got {port}."
        )

    for key in ("host", "username", "password", "database"):
        if not isinstance(ch[key], str):
            raise ConfigError(
                f"[clickhouse].{key} in {path} must be a string, got {ch[key]!r}."
            )

    return ClickHouseConfig(
        host=ch["host"],
        port=port,
        username=ch["username"],
        password=ch["password"],
        database=ch["database"],
    )
