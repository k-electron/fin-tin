"""edgartools bundled-table resolver tests (offline; never hits live EDGAR).

Resolution reads edgartools' bundled `company_tickers.parquet` — a file read, no
network. One test blocks the socket layer to PROVE it (NFR-7 hard guard); another
proves it refuses to fall back to the network when the bundled table is missing.
"""

from __future__ import annotations

import pytest

from fintin.adapters.edgar.universe import UniverseReferenceError, resolve_tickers


def test_known_ticker_resolves():
    assert resolve_tickers(["AAPL"]) == {"AAPL": 320193}


def test_ticker_case_and_dot_dash_normalized():
    # brk.b / BRK.B / BRK-B all normalize to the BRK-B lookup key; the result is
    # keyed by the ORIGINAL config string so a report can echo the exact value.
    out = resolve_tickers(["brk.b", "BRK.B", "BRK-B"])
    assert out == {"brk.b": 1067983, "BRK.B": 1067983, "BRK-B": 1067983}


def test_unknown_ticker_is_none_not_raise():
    # A ticker absent from the bundled table maps to None (→ recorded gap in
    # core), with NO network fallback (we build the map from the bundled loader).
    out = resolve_tickers(["AAPL", "ZZZZINVALID"])
    assert out["AAPL"] == 320193
    assert out["ZZZZINVALID"] is None


def test_bare_base_ticker_is_a_gap_not_an_alias():
    # We key on EXACT tickers only — no base-ticker aliases. Bare 'BRK' is not a
    # real ticker (BRK-A / BRK-B are), so it must be a gap, not a silent match.
    out = resolve_tickers(["BRK"])
    assert out == {"BRK": None}


def test_batch_resolves_in_one_pass():
    out = resolve_tickers(["AAPL", "MSFT", "AMZN"])
    assert out == {"AAPL": 320193, "MSFT": 789019, "AMZN": 1018724}


def test_missing_bundled_table_raises_no_fallback(monkeypatch):
    """If the bundled parquet can't load, we refuse to fall back to a live SEC
    fetch (AD-3 / FR-1) — raise a clear error instead."""
    import edgar.reference.tickers as et

    monkeypatch.setattr(et, "load_company_tickers_from_package", lambda: None)
    with pytest.raises(UniverseReferenceError):
        resolve_tickers(["AAPL"])


def test_resolution_touches_no_socket(monkeypatch):
    """NFR-7 hard proof: with the socket connect paths blocked, resolution still
    succeeds — so it makes no network request. Block only the CONNECT methods
    AFTER importing edgar (nulling `socket.socket` itself would break the ssl
    import edgar performs at import time — Story 1.4 lesson)."""
    import socket

    # Force a genuinely uncached parquet read so the proof covers the first
    # bundled load, not a warm lru_cache served from an earlier test.
    from edgar.reference.data.common import read_parquet_from_package

    if hasattr(read_parquet_from_package, "cache_clear"):
        read_parquet_from_package.cache_clear()

    def _no_connect(*args, **kwargs):
        raise AssertionError(
            "network access attempted during offline ticker resolution"
        )

    monkeypatch.setattr(socket.socket, "connect", _no_connect)
    monkeypatch.setattr(socket, "create_connection", _no_connect)

    assert resolve_tickers(["AAPL"]) == {"AAPL": 320193}
