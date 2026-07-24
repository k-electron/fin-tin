"""EDGAR client tests — pure unit tests; NEVER hit live EDGAR (ban risk).

- Cool-down timing is driven by an injected *recording* sleeper, so the suite
  never actually waits (and no real time elapses).
- Identity/header assertions use edgar's own client params + an httpx
  ``MockTransport``, so nothing leaves the process.
- No ``@pytest.mark.integration`` — these run under the default suite with no
  container and no network.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import edgar
import httpx
import pytest
from edgar import httpclient
from edgar.httprequests import TooManyRequestsError

from fintin.adapters.edgar.client import (
    _MAX_COOLDOWN_SECONDS,
    EdgarClient,
    EdgarConfigError,
    EdgarThrottleError,
)
from fintin.config import ClickHouseConfig, Config, EdgarConfig

_CH = ClickHouseConfig(
    host="localhost", port=8123, username="default", password="", database="default"
)


@pytest.fixture(autouse=True)
def _restore_edgar_globals():
    """Constructing an EdgarClient mutates process-global edgar state (identity,
    EDGAR_RATE_LIMIT_PER_SEC, HTTP_MGR). Snapshot and restore it around each test
    so nothing leaks across tests or into the wider process."""
    saved_mgr = httpclient.HTTP_MGR
    saved_rate = os.environ.get("EDGAR_RATE_LIMIT_PER_SEC")
    try:
        saved_identity = edgar.get_identity()
    except Exception:
        saved_identity = None
    try:
        yield
    finally:
        httpclient.HTTP_MGR = saved_mgr
        if saved_rate is None:
            os.environ.pop("EDGAR_RATE_LIMIT_PER_SEC", None)
        else:
            os.environ["EDGAR_RATE_LIMIT_PER_SEC"] = saved_rate
        if saved_identity:
            try:
                edgar.set_identity(saved_identity)
            except Exception:
                pass


def _config(**edgar_overrides) -> Config:
    base = dict(
        user_agent_name="fin-tin",
        contact_email="kboss@fintin.io",  # valid, non-placeholder
        rate_limit_per_sec=10.0,
        cooldown_seconds=600,
        max_throttle_retries=3,
    )
    base.update(edgar_overrides)
    return Config(clickhouse=_CH, edgar=EdgarConfig(**base))


class _Recorder:
    """A stand-in for time.sleep that records durations instead of waiting."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


# --- AC-1: declared identity + rate cap at edgartools' own throttle ------------


def test_sets_declared_identity():
    EdgarClient(_config())
    assert edgar.get_identity() == "fin-tin kboss@fintin.io"


def test_rate_capped_at_edgartools_throttle():
    EdgarClient(_config(rate_limit_per_sec=7))
    assert httpclient.get_edgar_rate_limit_per_sec() == 7
    assert httpclient.HTTP_MGR.request_per_sec_limit == 7


def test_request_headers_carry_ua_and_accept_encoding():
    """The real edgar client params + a MockTransport prove the outgoing
    request carries our declared UA and Accept-Encoding: gzip, deflate — offline."""
    EdgarClient(_config())
    params = httpclient.get_http_params()
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["ua"] = request.headers.get("user-agent")
        captured["ae"] = request.headers.get("accept-encoding")
        return httpx.Response(200, text="ok")

    # An explicit transport configures sending; verify/http2 shape the default
    # transport, so drop them to avoid the httpx conflict.
    safe = {k: v for k, v in params.items() if k not in ("verify", "http2")}
    with httpx.Client(transport=httpx.MockTransport(handler), **safe) as c:
        c.get("https://www.sec.gov/x")

    assert captured["ua"] == "fin-tin kboss@fintin.io"
    assert "gzip" in captured["ae"] and "deflate" in captured["ae"]


# --- AC-2: throttle failure -> Retry-After / >=10-min cool-down -> retry --------


def test_cooldown_uses_floor_when_no_retry_after():
    sleeper = _Recorder()
    client = EdgarClient(_config(), sleep=sleeper)
    calls = {"n": 0}

    def op():
        calls["n"] += 1
        if calls["n"] == 1:
            raise TooManyRequestsError("https://sec.gov/x")  # no Retry-After
        return "ok"

    assert client.run(op) == "ok"
    assert sleeper.waits == [600]  # one self-imposed cool-down at the floor
    assert calls["n"] == 2


def test_retry_after_below_floor_is_raised_to_floor():
    """Ban-safety: a Retry-After shorter than the ≥10-min floor must NOT shorten
    the wait — retrying inside the SEC block extends it."""
    sleeper = _Recorder()
    client = EdgarClient(_config(), sleep=sleeper)
    calls = {"n": 0}

    def op():
        calls["n"] += 1
        if calls["n"] == 1:
            raise TooManyRequestsError("https://sec.gov/x", retry_after=42)
        return "ok"

    assert client.run(op) == "ok"
    assert sleeper.waits == [600]  # floored, NOT 42


def test_retry_after_above_floor_is_honored():
    sleeper = _Recorder()
    client = EdgarClient(_config(), sleep=sleeper)
    calls = {"n": 0}

    def op():
        calls["n"] += 1
        if calls["n"] == 1:
            raise TooManyRequestsError("https://sec.gov/x", retry_after=900)
        return "ok"

    assert client.run(op) == "ok"
    assert sleeper.waits == [900]  # honored a Retry-After longer than the floor


def test_huge_retry_after_is_capped():
    sleeper = _Recorder()
    client = EdgarClient(_config(), sleep=sleeper)
    calls = {"n": 0}

    def op():
        calls["n"] += 1
        if calls["n"] == 1:
            raise TooManyRequestsError("https://sec.gov/x", retry_after=10**9)
        return "ok"

    assert client.run(op) == "ok"
    assert sleeper.waits == [_MAX_COOLDOWN_SECONDS]  # a garbage header can't wedge the run


def test_exhausted_retries_raise_domain_error_not_library_error():
    sleeper = _Recorder()
    client = EdgarClient(_config(max_throttle_retries=2), sleep=sleeper)

    def op():
        raise TooManyRequestsError("https://sec.gov/x")

    with pytest.raises(EdgarThrottleError):
        client.run(op)
    assert sleeper.waits == [600, 600]  # exactly max_throttle_retries cool-downs


def test_non_throttle_error_propagates_without_cooldown():
    sleeper = _Recorder()
    client = EdgarClient(_config(), sleep=sleeper)

    def op():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        client.run(op)
    assert sleeper.waits == []  # cool-down is throttle-only


# --- Ban-safety gate at construction (FR-1: never send a blank/undeclared UA) ---


def test_gate_rejects_missing_edgar_block():
    with pytest.raises(EdgarConfigError):
        EdgarClient(Config(clickhouse=_CH, edgar=None))


@pytest.mark.parametrize(
    "overrides",
    [
        dict(user_agent_name="   "),  # blank name
        dict(user_agent_name="fin-tin\nX-Injected: 1"),  # control char / header injection
        dict(contact_email="   "),  # blank email
        dict(contact_email="not-an-email"),  # malformed
        dict(contact_email="you@example.com"),  # literal placeholder
        dict(contact_email="your.email@example.com"),  # literal placeholder
        dict(contact_email="admin@example.com"),  # reserved domain, not a literal placeholder
        dict(contact_email="x@example.org"),  # reserved domain
        dict(contact_email="x@foo.test"),  # reserved TLD
        dict(contact_email="x@bar.invalid"),  # reserved TLD
    ],
)
def test_gate_rejects_bad_identity(overrides):
    with pytest.raises(EdgarConfigError):
        EdgarClient(_config(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        dict(cooldown_seconds=1),  # below the SEC 10-min floor
        dict(max_throttle_retries=-1),  # negative
        dict(rate_limit_per_sec=0.5),  # floors to 0 req/s
        dict(rate_limit_per_sec=11),  # above SEC max
    ],
)
def test_gate_rejects_unsafe_numeric_config(overrides):
    """A directly-built EdgarConfig bypasses load_config's validation, so the
    client must re-assert the ban-safety floors itself (defense in depth) —
    before mutating any edgar global."""
    with pytest.raises(EdgarConfigError):
        EdgarClient(_config(**overrides))


# --- AC-3: all EDGAR access goes through this one client (verified structurally)


def test_no_edgar_or_raw_http_imports_outside_edgar_adapter():
    root = Path(__file__).resolve().parent.parent / "fintin"
    edgar_adapter = root / "adapters" / "edgar"
    # Match on the top-level module so both `import urllib.request` and
    # `from urllib import request` (and `from http import client`) are caught,
    # not just a hand-listed pair. Covers edgartools + every raw network client.
    # (Dynamic imports via importlib/__import__ are not caught — a documented gap.)
    banned_roots = {
        "edgar", "httpx", "requests",
        "urllib", "http", "aiohttp", "urllib3", "socket", "ftplib",
    }
    offenders: list[tuple[str, str]] = []

    for py in root.rglob("*.py"):
        if py.parent == edgar_adapter or edgar_adapter in py.parents:
            continue  # the client IS allowed to import edgar/httpx
        tree = ast.parse(py.read_text(), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in banned_roots:
                        offenders.append((str(py), alias.name))
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".")[0] in banned_roots:
                    offenders.append((str(py), node.module or ""))

    assert not offenders, f"EDGAR/raw-HTTP imports outside adapters/edgar: {offenders}"
