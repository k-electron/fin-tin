"""Ticker → CIK resolution via edgartools' BUNDLED reference table (offline).

The concrete ``resolve_tickers`` port for :func:`fintin.core.universe.resolve_universe`.

**This is offline and issues no EDGAR request** — it reads edgartools' bundled
parquet (`edgar/reference/data/company_tickers.parquet`, shipped in the package),
so AD-3 ("all EDGAR access through the one rate-limited client") is not triggered:
there is no request to route. Consequently this path needs no ``EdgarClient``, no
rate limiter, and no contact email.

Why the plain lookup dict and NOT ``edgar.find_cik`` / ``Company(ticker)``:
``get_company_cik_lookup()`` reads only the bundled table (``@lru_cache``d — parsed
once per process). ``find_cik``/``Company`` add a *per-ticker live SEC fallback*
(`company_tickers.json` on sec.gov) for tickers absent from the bundle. Using the
dict getter keeps resolution guaranteed-offline and deterministic, and makes an
unresolvable ticker a clean ``None`` (→ recorded gap in core) instead of a silent
network round-trip. (For the record: even that fallback routes through edgartools'
rate-limited ``HTTP_MGR`` — nothing bypasses the limiter — we simply avoid it.)

This is the only module here (besides `client`/`facts`) that imports ``edgar``.
"""

from __future__ import annotations

from collections.abc import Sequence


def _normalize(ticker: str) -> str:
    """Match the bundled lookup's key form: upper-cased, ``.`` → ``-`` (so
    ``brk.b``, ``BRK.B`` and ``BRK-B`` all hit the ``BRK-B`` key)."""
    return ticker.strip().upper().replace(".", "-")


def resolve_tickers(tickers: Sequence[str]) -> dict[str, int | None]:
    """Resolve tickers to CIKs from edgartools' bundled reference table (offline).

    Returns ``{original_ticker: cik_or_None}`` keyed by the **original** configured
    ticker string (so a gap can report the exact config value). A ticker absent
    from the bundled table maps to ``None`` — no network fallback."""
    # Imported lazily so importing this module (e.g. for the type/port) does not
    # pull in the heavy `edgar` package until a resolve actually happens.
    from edgar.reference.tickers import get_company_cik_lookup

    lookup = get_company_cik_lookup()  # {TICKER: cik}, bundled parquet, no network
    result: dict[str, int | None] = {}
    for ticker in tickers:
        cik = lookup.get(_normalize(ticker))
        result[ticker] = int(cik) if cik is not None else None
    return result
