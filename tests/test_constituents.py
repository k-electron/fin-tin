"""Constituent parsing / config-rendering tests — pure, no network.

The parser takes CSV *text*, so every case here is a literal string; the HTTP
fetch is the adapter's job and is never exercised in the suite.
"""

from __future__ import annotations

import pytest

from fintin.core.constituents import (
    parse_constituents_csv,
    render_universe_block,
    replace_universe_section,
)
from tests.purity import assert_module_is_pure

_HEADER = "Symbol,Security,GICS Sector,CIK\n"


def test_parses_symbols_and_ciks():
    parsed = parse_constituents_csv(
        _HEADER + "AAPL,Apple Inc.,Information Technology,320193\n"
        "MSFT,Microsoft,Information Technology,789019\n"
    )
    assert parsed.tickers == ("AAPL", "MSFT")
    assert [c.cik for c in parsed.constituents] == [320193, 789019]
    assert parsed.skipped == ()


def test_quoted_company_names_containing_commas_do_not_shift_columns():
    """The real list is full of names like "Alphabet Inc. Class A" — a naive
    split(',') would read the CIK column from the wrong field."""
    parsed = parse_constituents_csv(
        _HEADER + '"GOOGL","Alphabet Inc., Class A",Communication Services,1652044\n'
    )
    assert parsed.tickers == ("GOOGL",)
    assert parsed.constituents[0].cik == 1652044


def test_cik_column_is_optional():
    parsed = parse_constituents_csv("Symbol,Security\nAAPL,Apple Inc.\n")
    assert parsed.tickers == ("AAPL",)
    assert parsed.constituents[0].cik is None


def test_ticker_header_spelling_is_accepted():
    parsed = parse_constituents_csv("Ticker,Name\nAAPL,Apple\n")
    assert parsed.tickers == ("AAPL",)


def test_missing_symbol_column_raises_rather_than_yielding_an_empty_universe():
    """An error page or a reshaped source must fail loudly — silently returning
    zero constituents would quietly wipe the Universe on --write."""
    with pytest.raises(ValueError, match="no Symbol/Ticker column"):
        parse_constituents_csv("Name,Sector\nApple,Tech\n")


def test_duplicate_symbols_collapse_preserving_source_order():
    parsed = parse_constituents_csv(
        _HEADER + "GOOG,Alphabet C,Comms,1652044\n"
        "AAPL,Apple,Tech,320193\n"
        "GOOG,Alphabet C again,Comms,1652044\n"
    )
    assert parsed.tickers == ("GOOG", "AAPL")


def test_blank_symbol_is_skipped_and_explained():
    parsed = parse_constituents_csv(_HEADER + ",Mystery Corp,Tech,1\nAAPL,Apple,Tech,320193\n")
    assert parsed.tickers == ("AAPL",)
    assert any("blank symbol" in s for s in parsed.skipped)


def test_bad_cik_degrades_to_ticker_only_rather_than_dropping_the_company():
    parsed = parse_constituents_csv(
        _HEADER + "AAPL,Apple,Tech,not-a-number\nMSFT,Microsoft,Tech,0\n"
    )
    assert parsed.tickers == ("AAPL", "MSFT")  # both kept
    assert [c.cik for c in parsed.constituents] == [None, None]
    assert len(parsed.skipped) == 2
    assert any("unparseable CIK" in s for s in parsed.skipped)
    assert any("out of range" in s for s in parsed.skipped)


def test_with_cik_filters_to_rescuable_entries():
    parsed = parse_constituents_csv(_HEADER + "AAPL,Apple,Tech,320193\nZZZZ,Zed,Tech,\n")
    assert [c.ticker for c in parsed.with_cik] == ["AAPL"]


# --- rendering ------------------------------------------------------------------


def test_render_block_is_valid_toml_and_round_trips():
    import tomllib

    block = render_universe_block(("AAPL", "MSFT", "BRK.B"), (1652044,))
    loaded = tomllib.loads(block)
    assert loaded["universe"]["tickers"] == ["AAPL", "MSFT", "BRK.B"]
    assert loaded["universe"]["ciks"] == [1652044]


def test_render_block_omits_ciks_when_empty():
    assert "ciks" not in render_universe_block(("AAPL",))


def test_render_block_wraps_long_lists():
    block = render_universe_block(tuple(f"T{i}" for i in range(20)), per_line=8)
    ticker_lines = [ln for ln in block.splitlines() if ln.startswith("    ")]
    assert len(ticker_lines) == 3  # 8 + 8 + 4


# --- config surgery -------------------------------------------------------------

_CONFIG = """\
[clickhouse]
host = "localhost"

[universe]
# a comment inside the section
tickers = [
    "OLD",
]

[reconcile]
lookback_days = 7
"""


def test_replace_universe_section_leaves_other_sections_intact():
    out = replace_universe_section(_CONFIG, render_universe_block(("NEW",)))
    import tomllib

    loaded = tomllib.loads(out)
    assert loaded["universe"]["tickers"] == ["NEW"]
    assert loaded["clickhouse"]["host"] == "localhost"  # untouched
    assert loaded["reconcile"]["lookback_days"] == 7  # untouched, still parses
    assert "OLD" not in out


def test_replace_universe_section_at_end_of_file():
    text = '[clickhouse]\nhost = "h"\n\n[universe]\ntickers = ["OLD"]\n'
    out = replace_universe_section(text, render_universe_block(("NEW",)))
    import tomllib

    assert tomllib.loads(out)["universe"]["tickers"] == ["NEW"]


def test_replace_universe_section_raises_when_absent():
    """The caller appends instead — silently producing a config with no
    [universe] would break every later command."""
    with pytest.raises(ValueError, match="no \\[universe\\] section"):
        replace_universe_section('[clickhouse]\nhost = "h"\n', "[universe]\n")


def test_core_constituents_is_pure():
    """`csv`/`io` parse an in-memory string — no I/O; the fetch is the adapter's."""
    assert_module_is_pure("fintin/core/constituents.py", allow=("csv", "io"))


# --- the fetch adapter: every failure becomes one clean error --------------------
# urlopen is faked throughout — the suite never opens a socket.


def _fake_urlopen(monkeypatch, *, body=b"", exc=None):
    import contextlib
    import urllib.request

    @contextlib.contextmanager
    def _open(request, timeout=None):
        if exc is not None:
            raise exc
        yield type("R", (), {"read": staticmethod(lambda n=None: body)})()

    monkeypatch.setattr(urllib.request, "urlopen", _open)


def test_fetch_returns_body_text(monkeypatch):
    from fintin.adapters.constituents import fetch_constituents_csv

    _fake_urlopen(monkeypatch, body=b"Symbol\nAAPL\n")
    assert fetch_constituents_csv("https://example.test/c.csv") == "Symbol\nAAPL\n"


def test_fetch_rejects_non_http_scheme():
    """Without this, a config URL of file:///etc/passwd would be opened."""
    from fintin.adapters.constituents import (
        ConstituentFetchError,
        fetch_constituents_csv,
    )

    with pytest.raises(ConstituentFetchError, match="must be http"):
        fetch_constituents_csv("file:///etc/passwd")


def test_fetch_maps_http_error_to_a_clean_failure(monkeypatch):
    import urllib.error

    from fintin.adapters.constituents import (
        ConstituentFetchError,
        fetch_constituents_csv,
    )

    _fake_urlopen(
        monkeypatch,
        exc=urllib.error.HTTPError("u", 404, "Not Found", None, None),
    )
    with pytest.raises(ConstituentFetchError, match="HTTP 404"):
        fetch_constituents_csv("https://example.test/c.csv")


def test_fetch_maps_network_error_to_a_clean_failure(monkeypatch):
    import urllib.error

    from fintin.adapters.constituents import (
        ConstituentFetchError,
        fetch_constituents_csv,
    )

    _fake_urlopen(monkeypatch, exc=urllib.error.URLError("dns dead"))
    with pytest.raises(ConstituentFetchError, match="could not fetch"):
        fetch_constituents_csv("https://example.test/c.csv")


def test_fetch_rejects_an_oversized_body(monkeypatch):
    from fintin.adapters import constituents as mod

    _fake_urlopen(monkeypatch, body=b"x" * (mod._MAX_BYTES + 1))
    with pytest.raises(mod.ConstituentFetchError, match="exceeded"):
        mod.fetch_constituents_csv("https://example.test/c.csv")


def test_fetch_rejects_undecodable_bytes(monkeypatch):
    from fintin.adapters.constituents import (
        ConstituentFetchError,
        fetch_constituents_csv,
    )

    _fake_urlopen(monkeypatch, body=b"\xff\xfe\x00garbage")
    with pytest.raises(ConstituentFetchError, match="not valid UTF-8"):
        fetch_constituents_csv("https://example.test/c.csv")


def test_default_source_is_not_sec_gov():
    """Belt and braces with the AC-3 structural guard: this fetch must never be an
    EDGAR request escaping the rate limiter."""
    from fintin.adapters.constituents import DEFAULT_CONSTITUENTS_URL

    assert "sec.gov" not in DEFAULT_CONSTITUENTS_URL.lower()
    assert DEFAULT_CONSTITUENTS_URL.startswith("https://")
