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
class EdgarConfig:
    """EDGAR fair-access settings (AD-3, FR-1).

    ``user_agent_name`` + ``contact_email`` form the declared identifying
    User-Agent. ``rate_limit_per_sec`` caps requests at edgartools' own throttle
    (SEC max 10). ``cooldown_seconds`` is the self-imposed cool-down on a throttle
    breach (SEC 10-min floor). Note: load-time validation here is STRUCTURE +
    TYPES + RANGES only — the ban-safety semantic gate (blank / malformed /
    placeholder email) lives in the EDGAR client, so a well-formed block with a
    placeholder email loads cleanly (non-EDGAR commands keep working).
    """

    user_agent_name: str
    contact_email: str
    rate_limit_per_sec: float = 9.0  # SEC max is 10; 9 leaves a safety margin (edgartools' own default)
    cooldown_seconds: int = 600
    max_throttle_retries: int = 3


@dataclass(frozen=True)
class Config:
    clickhouse: ClickHouseConfig
    edgar: EdgarConfig | None = None


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

    ed = data.get("edgar")
    edgar = None
    if ed is not None:
        if not isinstance(ed, dict):
            raise ConfigError(f"[edgar] in {path} must be a table/section.")
        edgar = _parse_edgar(ed, path)

    return Config(clickhouse=_parse_clickhouse(ch, path), edgar=edgar)


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


def _parse_edgar(ed: dict, path: Path) -> EdgarConfig:
    # Required identity keys must be present and string-typed. The blank /
    # malformed / placeholder-email *semantic* check is deliberately NOT here —
    # it lives in the EDGAR client's construction gate (see
    # fintin/adapters/edgar/client.py), so a well-formed block carrying the
    # placeholder email still loads and non-EDGAR commands keep working.
    missing = [k for k in ("user_agent_name", "contact_email") if k not in ed]
    if missing:
        raise ConfigError(
            f"[edgar] in {path} is missing required key(s): {', '.join(missing)}."
        )
    for key in ("user_agent_name", "contact_email"):
        if not isinstance(ed[key], str):
            raise ConfigError(
                f"[edgar].{key} in {path} must be a string, got {ed[key]!r}."
            )

    rate = ed.get("rate_limit_per_sec", 9.0)
    # bool is a subclass of int/float — reject it before the numeric check.
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        raise ConfigError(
            f"[edgar].rate_limit_per_sec in {path} must be a number, got {rate!r}."
        )
    # edgartools' throttle is integer req/s; require >= 1 so the loader and the
    # client agree (the client applies int(rate)). SEC max is 10.
    if not (1 <= rate <= 10):
        raise ConfigError(
            f"[edgar].rate_limit_per_sec in {path} must be in [1, 10] "
            f"(SEC max is 10 req/s), got {rate}."
        )

    cooldown = ed.get("cooldown_seconds", 600)
    if isinstance(cooldown, bool) or not isinstance(cooldown, int):
        raise ConfigError(
            f"[edgar].cooldown_seconds in {path} must be an integer, got {cooldown!r}."
        )
    if cooldown < 600:
        raise ConfigError(
            f"[edgar].cooldown_seconds in {path} must be >= 600 "
            f"(SEC 10-minute cool-down floor), got {cooldown}."
        )

    retries = ed.get("max_throttle_retries", 3)
    if isinstance(retries, bool) or not isinstance(retries, int):
        raise ConfigError(
            f"[edgar].max_throttle_retries in {path} must be an integer, got {retries!r}."
        )
    if retries < 0:
        raise ConfigError(
            f"[edgar].max_throttle_retries in {path} must be >= 0, got {retries}."
        )

    return EdgarConfig(
        user_agent_name=ed["user_agent_name"],
        contact_email=ed["contact_email"],
        rate_limit_per_sec=float(rate),
        cooldown_seconds=cooldown,
        max_throttle_retries=retries,
    )
