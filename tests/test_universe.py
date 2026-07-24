"""Pure Universe-resolution tests (no container, no network).

`resolve_universe` is exercised with an INJECTED fake resolver so the core stays
edgar-free and offline; an AST guard locks that purity in.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from fintin.config import UniverseConfig
from fintin.core.universe import ResolvedUniverse, UniverseGap, resolve_universe


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


def _module_imports(path: str) -> set[str]:
    tree = ast.parse(Path(path).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                imported.add(n.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    return imported


def test_core_universe_has_no_edgar_import():
    """Purity guard: the core resolver never imports `edgar` — ticker resolution
    is an injected port, so core stays offline and unit-testable. A regression
    adding an `edgar` import here fails CI."""
    assert "edgar" not in _module_imports("fintin/core/universe.py")
