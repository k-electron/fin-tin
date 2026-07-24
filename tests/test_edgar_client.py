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
from pathlib import Path

import edgar
import httpx
import pytest
from edgar import httpclient
from edgar.httprequests import TooManyRequestsError

from fintin.adapters.edgar.client import (
    EdgarClient,
    EdgarConfigError,
    EdgarThrottleError,
)
from fintin.config import ClickHouseConfig, Config, EdgarConfig

_CH = ClickHouseConfig(
    host="localhost", port=8123, username="default", password="", database="default"
)


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


def test_cooldown_then_retry_succeeds():
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


def test_cooldown_honors_retry_after_when_present():
    sleeper = _Recorder()
    client = EdgarClient(_config(), sleep=sleeper)
    calls = {"n": 0}

    def op():
        calls["n"] += 1
        if calls["n"] == 1:
            raise TooManyRequestsError("https://sec.gov/x", retry_after=42)
        return "ok"

    assert client.run(op) == "ok"
    assert sleeper.waits == [42]  # honored the header value


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
        dict(contact_email="   "),  # blank email
        dict(contact_email="not-an-email"),  # malformed
        dict(contact_email="you@example.com"),  # placeholder
        dict(contact_email="your.email@example.com"),  # placeholder
    ],
)
def test_gate_rejects_bad_identity(overrides):
    with pytest.raises(EdgarConfigError):
        EdgarClient(_config(**overrides))


# --- AC-3: all EDGAR access goes through this one client (verified structurally)


def test_no_edgar_or_raw_http_imports_outside_edgar_adapter():
    root = Path(__file__).resolve().parent.parent / "fintin"
    edgar_adapter = root / "adapters" / "edgar"
    banned_roots = {"edgar", "httpx", "requests"}
    banned_full = {"urllib.request", "http.client"}
    offenders: list[tuple[str, str]] = []

    for py in root.rglob("*.py"):
        if py.parent == edgar_adapter or edgar_adapter in py.parents:
            continue  # the client IS allowed to import edgar/httpx
        tree = ast.parse(py.read_text(), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in banned_roots or alias.name in banned_full:
                        offenders.append((str(py), alias.name))
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod.split(".")[0] in banned_roots or mod in banned_full:
                    offenders.append((str(py), mod))

    assert not offenders, f"EDGAR/raw-HTTP imports outside adapters/edgar: {offenders}"
