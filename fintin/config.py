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
class UniverseConfig:
    """The configured screening Universe (FR-7, AD-13): a static list of tickers
    and/or CIKs. Tickers are resolved to CIKs at run time via edgartools'
    bundled reference table (offline, no network) — see
    ``fintin.adapters.edgar.universe`` / ``fintin.core.universe``.

    Load-time validation here is STRUCTURE + TYPES + RANGES only. Whether a
    (well-formed) ticker actually resolves is decided at resolve time — an
    unknown ticker loads cleanly and later surfaces as an explained gap, never a
    silent drop (SM-2). ``tickers`` are kept verbatim (normalization to the
    lookup key form happens at resolve time). The Universe is **derived from
    config, never persisted** (AD-1)."""

    tickers: tuple[str, ...]
    ciks: tuple[int, ...]


# CIK is a UInt32 in the store (AD "Identity" convention), so 1..2^32-1.
_CIK_MAX = 4_294_967_295

# Default reordering-safe lookback window (AD-16) — days before the store's
# high-water mark to re-scan for stragglers/restatements. Tunable via config.
DEFAULT_LOOKBACK_DAYS = 7
# Upper bound — a lookback is a filing-order-skew window, not a backfill horizon.
# Caps quarterly-index fan-out and guards against a timedelta OverflowError.
MAX_LOOKBACK_DAYS = 3650  # ~10 years


@dataclass(frozen=True)
class ReconcileConfig:
    """Work-list reconciler tuning (AD-16). ``lookback_days`` sizes the
    reordering-safe scan window `[HWM - lookback, today]` — spanning the
    plausible filing-order skew so a filing filed just before the high-water
    mark but not yet committed is still re-checked. The Universe/rate/identity
    live in their own sections; this is derivation tuning only."""

    lookback_days: int = DEFAULT_LOOKBACK_DAYS


# Single-flight lease defaults (AD-12). The lease is a FILESYSTEM lock file (not
# ClickHouse); `path` is relative to the CWD by default. TTL is how long after the
# last heartbeat the lease is considered expired (a crashed run self-expires);
# heartbeat is how often a live run refreshes it — kept well below the TTL.
DEFAULT_LEASE_PATH = "fintin.lease"
DEFAULT_LEASE_TTL_SECONDS = 120
DEFAULT_LEASE_HEARTBEAT_SECONDS = 15
# Upper bound on the TTL — beyond this a crashed run's lease would outlive any sane
# recovery window and silently defeat the self-expiry guarantee (a fat-fingered
# ttl_seconds must not deadlock the tool for days).
MAX_LEASE_TTL_SECONDS = 86400  # 24h


@dataclass(frozen=True)
class LeaseConfig:
    """Single-flight self-expiring lease (AD-12, FR-11). A trigger arriving during
    an active (heartbeating) run coalesces to ``ALREADY_RUNNING``; a crashed run's
    lease expires after ``ttl_seconds`` past its last heartbeat and is reclaimed.
    ``heartbeat_seconds`` must be ≪ ``ttl_seconds`` (enforced: ≥2 beats per TTL) so
    a live run — even one blocked in an EDGAR cool-down — is never reclaimed."""

    path: str = DEFAULT_LEASE_PATH
    ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS
    heartbeat_seconds: int = DEFAULT_LEASE_HEARTBEAT_SECONDS


@dataclass(frozen=True)
class Config:
    clickhouse: ClickHouseConfig
    edgar: EdgarConfig | None = None
    universe: UniverseConfig | None = None
    # Always populated (default when the [reconcile] section is absent) so the
    # reconciler always has a lookback value.
    reconcile: ReconcileConfig = ReconcileConfig()
    # Always populated (default when the [lease] section is absent) so single-flight
    # is on by default with a safe path/TTL/heartbeat.
    lease: LeaseConfig = LeaseConfig()


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

    un = data.get("universe")
    universe = None
    if un is not None:
        if not isinstance(un, dict):
            raise ConfigError(f"[universe] in {path} must be a table/section.")
        universe = _parse_universe(un, path)

    rc = data.get("reconcile")
    reconcile = ReconcileConfig()
    if rc is not None:
        if not isinstance(rc, dict):
            raise ConfigError(f"[reconcile] in {path} must be a table/section.")
        reconcile = _parse_reconcile(rc, path)

    ls = data.get("lease")
    lease = LeaseConfig()
    if ls is not None:
        if not isinstance(ls, dict):
            raise ConfigError(f"[lease] in {path} must be a table/section.")
        lease = _parse_lease(ls, path)

    return Config(
        clickhouse=_parse_clickhouse(ch, path),
        edgar=edgar,
        universe=universe,
        reconcile=reconcile,
        lease=lease,
    )


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


def _parse_universe(un: dict, path: Path) -> UniverseConfig:
    # Structure/type/range checks only (no resolvability check — that is a
    # resolve-time concern; an unknown-but-well-formed ticker loads fine).
    tickers_raw = un.get("tickers", [])
    ciks_raw = un.get("ciks", [])

    if not isinstance(tickers_raw, list):
        raise ConfigError(f"[universe].tickers in {path} must be a list of strings.")
    if not isinstance(ciks_raw, list):
        raise ConfigError(f"[universe].ciks in {path} must be a list of integers.")

    tickers: list[str] = []
    for t in tickers_raw:
        # bool is a str-incompatible scalar; a non-string (or blank) ticker is a
        # config mistake, not something to silently coerce/skip.
        if not isinstance(t, str) or not t.strip():
            raise ConfigError(
                f"[universe].tickers in {path} must be non-empty strings, got {t!r}."
            )
        tickers.append(t)

    ciks: list[int] = []
    for c in ciks_raw:
        # bool subclasses int — reject it before the range check so `ciks = [true]`
        # is not silently accepted as CIK 1.
        if isinstance(c, bool) or not isinstance(c, int):
            raise ConfigError(
                f"[universe].ciks in {path} must be integers, got {c!r}."
            )
        if not (1 <= c <= _CIK_MAX):
            raise ConfigError(
                f"[universe].ciks in {path} must be between 1 and {_CIK_MAX} "
                f"(CIK is a UInt32), got {c}."
            )
        ciks.append(c)

    if not tickers and not ciks:
        raise ConfigError(
            f"[universe] in {path} is empty — list at least one ticker or cik."
        )

    return UniverseConfig(tickers=tuple(tickers), ciks=tuple(ciks))


def _parse_reconcile(rc: dict, path: Path) -> ReconcileConfig:
    lookback = rc.get("lookback_days", DEFAULT_LOOKBACK_DAYS)
    # bool subclasses int — reject it before the int/range check.
    if isinstance(lookback, bool) or not isinstance(lookback, int):
        raise ConfigError(
            f"[reconcile].lookback_days in {path} must be an integer, got {lookback!r}."
        )
    if not (1 <= lookback <= MAX_LOOKBACK_DAYS):
        raise ConfigError(
            f"[reconcile].lookback_days in {path} must be between 1 and "
            f"{MAX_LOOKBACK_DAYS} (a lookback is a filing-skew window, not a "
            f"backfill horizon), got {lookback}."
        )
    return ReconcileConfig(lookback_days=lookback)


def _parse_lease(ls: dict, path: Path) -> LeaseConfig:
    # Structure/type/range only. The lease is a filesystem file; the adapter
    # creates its parent dir at acquire time, so any writable path is valid here.
    lease_path = ls.get("path", DEFAULT_LEASE_PATH)
    if not isinstance(lease_path, str) or not lease_path.strip():
        raise ConfigError(
            f"[lease].path in {path} must be a non-empty string, got {lease_path!r}."
        )

    ttl = ls.get("ttl_seconds", DEFAULT_LEASE_TTL_SECONDS)
    # bool subclasses int — reject it before the int/range check.
    if isinstance(ttl, bool) or not isinstance(ttl, int):
        raise ConfigError(
            f"[lease].ttl_seconds in {path} must be an integer, got {ttl!r}."
        )
    if not (2 <= ttl <= MAX_LEASE_TTL_SECONDS):
        raise ConfigError(
            f"[lease].ttl_seconds in {path} must be between 2 and "
            f"{MAX_LEASE_TTL_SECONDS} (>=2 for room for two heartbeats; a huge TTL "
            f"would defeat self-expiry), got {ttl}."
        )

    heartbeat = ls.get("heartbeat_seconds", DEFAULT_LEASE_HEARTBEAT_SECONDS)
    if isinstance(heartbeat, bool) or not isinstance(heartbeat, int):
        raise ConfigError(
            f"[lease].heartbeat_seconds in {path} must be an integer, got {heartbeat!r}."
        )
    if heartbeat < 1:
        raise ConfigError(
            f"[lease].heartbeat_seconds in {path} must be >= 1, got {heartbeat}."
        )
    # Heartbeat must be well below the TTL (>=2 beats per TTL window) so a live
    # run — even one paused briefly (GC, cool-down) — is never falsely reclaimed.
    if 2 * heartbeat > ttl:
        raise ConfigError(
            f"[lease].heartbeat_seconds ({heartbeat}) must be <= half of "
            f"ttl_seconds ({ttl}) in {path} (heartbeat must be << TTL)."
        )

    return LeaseConfig(
        path=lease_path,
        ttl_seconds=ttl,
        heartbeat_seconds=heartbeat,
    )
