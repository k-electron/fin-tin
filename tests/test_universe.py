"""Pure Universe-resolution tests (no container, no network).

`resolve_universe` is exercised with an INJECTED fake resolver so the core stays
edgar-free and offline; an AST guard locks that purity in.
"""

from __future__ import annotations

import pytest

from fintin.config import UniverseConfig
from fintin.core.universe import (
    ResolvedUniverse,
    UniverseGap,
    normalize_ticker,
    resolve_universe,
)
from tests.purity import assert_module_is_pure


def _fake_resolver(mapping: dict[str, int | None]):
    """A batch resolver that returns a fixed {ticker: cik_or_None} mapping and
    records that it was called with exactly the configured tickers."""
    calls: list[tuple[str, ...]] = []

    def resolve(tickers):
        calls.append(tuple(tickers))
        return {t: mapping.get(t) for t in tickers}

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


def test_resolves_tickers_and_unions_with_explicit_ciks():
    u = UniverseConfig(tickers=("AAPL", "MSFT"), ciks=(1652044,))
    resolved = resolve_universe(
        u, resolve_tickers=_fake_resolver({"AAPL": 320193, "MSFT": 789019})
    )
    assert isinstance(resolved, ResolvedUniverse)
    assert resolved.ciks == (320193, 789019, 1652044)  # sorted, deduped, unioned
    assert resolved.gaps == ()
    assert resolved.tickers_resolved == 2
    assert resolved.explicit_ciks == 1


def test_unresolvable_ticker_becomes_gap_and_rest_still_resolve():
    u = UniverseConfig(tickers=("AAPL", "ZZZZ", "MSFT"), ciks=())
    resolved = resolve_universe(
        u,
        resolve_tickers=_fake_resolver({"AAPL": 320193, "ZZZZ": None, "MSFT": 789019}),
    )
    assert resolved.ciks == (320193, 789019)  # the resolvable two, not dropped
    assert resolved.gaps == (
        UniverseGap("ZZZZ", "not found in edgartools reference data"),
    )
    assert resolved.tickers_resolved == 2


def test_ticker_resolving_to_an_explicit_cik_is_deduped():
    # AAPL resolves to 320193, which is also listed explicitly -> one CIK, not two.
    u = UniverseConfig(tickers=("AAPL",), ciks=(320193,))
    resolved = resolve_universe(u, resolve_tickers=_fake_resolver({"AAPL": 320193}))
    assert resolved.ciks == (320193,)
    assert resolved.explicit_ciks == 1
    assert resolved.tickers_resolved == 1  # it did resolve; the CIK just coincides


def test_pure_cik_universe_never_calls_resolver():
    u = UniverseConfig(tickers=(), ciks=(320193, 789019))
    resolver = _fake_resolver({"AAPL": 320193})
    resolved = resolve_universe(u, resolve_tickers=resolver)
    assert resolved.ciks == (320193, 789019)
    assert resolved.tickers_resolved == 0
    assert resolver.calls == []  # AC: no resolution work for a pure-CIK Universe


def test_adding_a_cik_grows_the_universe_no_code_change():
    """NFR-2: the Universe is config-derived — adding a CIK grows the resolved set."""
    before = resolve_universe(
        UniverseConfig(tickers=(), ciks=(320193,)), resolve_tickers=_fake_resolver({})
    )
    after = resolve_universe(
        UniverseConfig(tickers=(), ciks=(320193, 789019)),
        resolve_tickers=_fake_resolver({}),
    )
    assert before.ciks == (320193,)
    assert after.ciks == (320193, 789019)


def test_ciks_returned_sorted_and_deduplicated():
    u = UniverseConfig(tickers=("A", "B"), ciks=(500, 100, 500))
    resolved = resolve_universe(u, resolve_tickers=_fake_resolver({"A": 300, "B": 100}))
    assert resolved.ciks == (100, 300, 500)  # sorted; the 500/100 dupes collapsed


def test_gaps_ordered_by_config_order():
    u = UniverseConfig(tickers=("ZZ1", "AAPL", "ZZ2"), ciks=())
    resolved = resolve_universe(
        u, resolve_tickers=_fake_resolver({"ZZ1": None, "AAPL": 320193, "ZZ2": None})
    )
    assert [g.identifier for g in resolved.gaps] == ["ZZ1", "ZZ2"]  # config order


def test_normalized_duplicate_tickers_counted_once():
    # ".", "-" and case variants of one ticker are the same company — count and
    # resolve once, not thrice, and pass only the first original to the resolver.
    u = UniverseConfig(tickers=("BRK.B", "BRK-B", "brk.b"), ciks=())
    resolver = _fake_resolver({"BRK.B": 1067983})
    resolved = resolve_universe(u, resolve_tickers=resolver)
    assert resolved.ciks == (1067983,)
    assert resolved.tickers_resolved == 1
    assert resolver.calls == [("BRK.B",)]  # deduped to the first original


def test_duplicate_unresolvable_ticker_gapped_once():
    u = UniverseConfig(tickers=("ZZZZ", "ZZZZ"), ciks=())
    resolved = resolve_universe(u, resolve_tickers=_fake_resolver({"ZZZZ": None}))
    assert resolved.gaps == (
        UniverseGap("ZZZZ", "not found in edgartools reference data"),
    )


def test_resolved_cik_out_of_range_becomes_gap():
    # A resolved CIK must fit the UInt32 store column (matching the config guard);
    # an out-of-range value is a gap, not a downstream insert failure.
    u = UniverseConfig(tickers=("BADCIK",), ciks=())
    resolved = resolve_universe(
        u, resolve_tickers=_fake_resolver({"BADCIK": 4_294_967_296})
    )
    assert resolved.ciks == ()
    assert len(resolved.gaps) == 1
    assert "out of range" in resolved.gaps[0].reason


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("brk.b", "BRK-B"),
        ("  aapl  ", "AAPL"),
        ("BRK-B", "BRK-B"),
        ("BF.B", "BF-B"),
    ],
)
def test_normalize_ticker(raw, expected):
    assert normalize_ticker(raw) == expected


def test_core_universe_is_pure():
    """Purity guard: the core resolver imports nothing impure — ticker resolution
    is an injected port, so core stays offline and unit-testable. A regression
    adding `edgar` (or any other impure dependency) here fails CI."""
    assert_module_is_pure("fintin/core/universe.py")
