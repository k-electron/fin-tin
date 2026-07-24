"""Universe resolution — the pure core (AD-1, AD-2, AD-13).

Turns the configured :class:`~fintin.config.UniverseConfig` (a static list of
tickers and/or CIKs) into a single derived value: a deduplicated, sorted set of
integer CIKs plus the explained gaps for any ticker that could not be resolved.

Pure core: depends on nothing outward (no ``edgar``, no ClickHouse). Ticker
resolution is an **injected port** — a batch ``resolve_tickers`` callable the
edgar adapter supplies (`fintin.adapters.edgar.universe`) — so this module stays
edgar-free and unit-testable with a fake resolver, exactly like
`core.ingest.ingest_company` injects its fetch/insert ports.

The Universe is **derived, never persisted** (AD-1): callers get a value back;
nothing writes it to disk or the store. Backfill (Story 2.3) calls
:func:`resolve_universe` to get its scope on each run.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import NamedTuple

from fintin.config import UniverseConfig


class UniverseGap(NamedTuple):
    """A configured Universe entry that could not be resolved — surfaced, never
    silently dropped (SM-2)."""

    identifier: str  # the configured ticker (verbatim)
    reason: str


class ResolvedUniverse(NamedTuple):
    """The resolved Universe: a deduplicated, sorted set of CIKs plus gaps.

    ``tickers_resolved`` counts tickers that mapped to a CIK; ``explicit_ciks``
    counts CIKs listed directly in config. (A ticker resolving to an
    already-listed CIK is a union, so ``len(ciks)`` can be less than
    ``tickers_resolved + explicit_ciks``.)"""

    ciks: tuple[int, ...]
    gaps: tuple[UniverseGap, ...]
    tickers_resolved: int
    explicit_ciks: int


def resolve_universe(
    universe: UniverseConfig,
    *,
    resolve_tickers: Callable[[Sequence[str]], dict[str, int | None]],
) -> ResolvedUniverse:
    """Resolve a configured Universe into CIKs + gaps. Pure (no I/O).

    ``resolve_tickers`` is a **batch** port: given the configured ticker list it
    returns ``{ticker: cik_or_None}`` keyed by the original ticker string. A
    ``None`` (or missing) mapping becomes a :class:`UniverseGap`; every other
    ticker's CIK is unioned with the explicitly-listed CIKs. CIKs are returned
    sorted and deduplicated for deterministic downstream behavior. The resolver
    is not called when there are no tickers (a pure-CIK Universe needs no
    resolution)."""
    ciks: set[int] = set(universe.ciks)
    explicit_ciks = len(ciks)
    gaps: list[UniverseGap] = []
    tickers_resolved = 0

    if universe.tickers:
        resolved = resolve_tickers(universe.tickers)
        # Iterate the configured order (not the dict's) so gaps report in a
        # stable, config-ordered way.
        for ticker in universe.tickers:
            cik = resolved.get(ticker)
            if cik is None:
                gaps.append(
                    UniverseGap(ticker, "not found in edgartools reference data")
                )
                continue
            ciks.add(int(cik))
            tickers_resolved += 1

    return ResolvedUniverse(
        ciks=tuple(sorted(ciks)),
        gaps=tuple(gaps),
        tickers_resolved=tickers_resolved,
        explicit_ciks=explicit_ciks,
    )
