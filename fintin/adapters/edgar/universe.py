"""Ticker → CIK resolution via edgartools' BUNDLED reference table (offline).

The concrete ``resolve_tickers`` port for :func:`fintin.core.universe.resolve_universe`.

**This is offline and issues no EDGAR request.** It reads edgartools' bundled
parquet (`edgar/reference/data/company_tickers.parquet`, shipped in the package)
via ``load_company_tickers_from_package()`` and builds the ticker→CIK map itself,
so AD-3 ("all EDGAR access through the one rate-limited client") is not triggered:
there is no request to route. Consequently this needs no ``EdgarClient``, no rate
limiter, and no contact email.

Why we build the map ourselves instead of calling ``get_company_cik_lookup`` /
``find_cik`` / ``Company(ticker)``:

1. **No network fallback.** ``get_company_cik_lookup`` → ``_get_company_tickers_raw``
   *falls through to a live ``download_json(sec.gov)`` fetch* if the bundled
   parquet fails to load — bypassing the rate-limited client AND the ban-safety
   email gate. ``load_company_tickers_from_package()`` instead returns ``None`` on
   failure with no fallback, so we raise :class:`UniverseReferenceError` and refuse
   to touch the network. ``find_cik``/``Company`` add a per-ticker live fallback too.
2. **Exact-match only, no base-ticker aliases.** ``get_company_cik_lookup`` injects
   synthetic base-ticker keys (``ticker.split('-')[0]``), so a bare ``BRK``/``CRD``
   would silently resolve to a share-class issuer's CIK (order-dependent, latent
   nondeterminism). We key only on the exact table ticker, so a bare base surfaces
   as an explained gap — the user lists the exact ticker (``BRK-B``) or its CIK.

This is the only module here (besides `client`/`facts`) that imports ``edgar``.
"""

from __future__ import annotations

from collections.abc import Sequence

from fintin.core.universe import normalize_ticker


class UniverseReferenceError(RuntimeError):
    """Raised when edgartools' bundled ticker reference table cannot be loaded.
    We refuse to fall back to a live SEC fetch (that would bypass the
    rate-limited client + FR-1 ban-safety email gate, AD-3)."""


def resolve_tickers(tickers: Sequence[str]) -> dict[str, int | None]:
    """Resolve tickers to CIKs from edgartools' bundled reference table (offline).

    Returns ``{original_ticker: cik_or_None}`` keyed by the **original** configured
    ticker string (so a gap can report the exact config value). A ticker absent
    from the bundled table maps to ``None`` — no network fallback, and no
    base-ticker-alias match (exact ticker only)."""
    # Lazy import so importing this module (e.g. for the port type) does not pull
    # in the heavy `edgar` package until a resolve actually happens.
    from edgar.reference.tickers import load_company_tickers_from_package

    df = load_company_tickers_from_package()  # bundled parquet, no network
    if df is None:
        raise UniverseReferenceError(
            "edgartools' bundled company_tickers.parquet could not be loaded; "
            "refusing to fall back to a live SEC fetch (ban-safety, AD-3). "
            "Reinstall or upgrade edgartools."
        )

    # Exact-key {TICKER: cik} map (no base-ticker aliases). Table tickers are
    # already upper-case with hyphens (e.g. 'AAPL', 'BRK-B'); cik is int64.
    lookup = {str(t): int(c) for t, c in zip(df["ticker"], df["cik"])}
    return {ticker: lookup.get(normalize_ticker(ticker)) for ticker in tickers}
