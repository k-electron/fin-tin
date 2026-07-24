"""edgartools bundled-table resolver tests (offline; never hits live EDGAR).

Resolution reads edgartools' bundled `company_tickers.parquet` — a file read, no
network. One test additionally blocks the socket layer to PROVE the path touches
no network (NFR-7 hard guard).
"""

from __future__ import annotations

from fintin.adapters.edgar.universe import resolve_tickers


def test_known_ticker_resolves():
    assert resolve_tickers(["AAPL"]) == {"AAPL": 320193}


def test_ticker_case_and_dot_dash_normalized():
    # brk.b / BRK.B / BRK-B all normalize to the BRK-B lookup key; the result is
    # keyed by the ORIGINAL config string so a report can echo the exact value.
    out = resolve_tickers(["brk.b", "BRK.B", "BRK-B"])
    assert out == {"brk.b": 1067983, "BRK.B": 1067983, "BRK-B": 1067983}


def test_unknown_ticker_is_none_not_raise():
    # A ticker absent from the bundled table maps to None (→ recorded gap in
    # core), with NO network fallback (we use the dict getter, not find_cik).
    out = resolve_tickers(["AAPL", "ZZZZINVALID"])
    assert out["AAPL"] == 320193
    assert out["ZZZZINVALID"] is None


def test_batch_resolves_in_one_pass():
    out = resolve_tickers(["AAPL", "MSFT", "AMZN"])
    assert out == {"AAPL": 320193, "MSFT": 789019, "AMZN": 1018724}


def test_resolution_touches_no_socket(monkeypatch):
    """NFR-7 hard proof: with the socket connect paths blocked, resolution still
    succeeds — so it makes no network request. Block only the CONNECT methods
    AFTER importing edgar (nulling `socket.socket` itself would break the ssl
    import edgar performs at import time — Story 1.4 lesson)."""
    import socket

    from edgar.reference import tickers as et

    # Force a genuinely uncached rebuild so the proof covers the first bundled
    # load, not a warm lru_cache served from an earlier test.
    for name in (
        "get_company_cik_lookup",
        "get_cik_tickers",
        "get_company_tickers",
        "_get_company_tickers_raw",
    ):
        fn = getattr(et, name, None)
        if fn is not None and hasattr(fn, "cache_clear"):
            fn.cache_clear()

    def _no_connect(*args, **kwargs):
        raise AssertionError(
            "network access attempted during offline ticker resolution"
        )

    monkeypatch.setattr(socket.socket, "connect", _no_connect)
    monkeypatch.setattr(socket, "create_connection", _no_connect)

    assert resolve_tickers(["AAPL"]) == {"AAPL": 320193}
