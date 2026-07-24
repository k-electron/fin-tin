"""EDGAR company-facts fetch — through the single rate-limited client (AD-3).

The only place `edgar.get_company_facts` is called. The fetch is routed through
:meth:`EdgarClient.run` so the fair-access cool-down (Story 1.3) covers the
companyfacts download (which raises ``TooManyRequestsError`` on a 429).
"""

from __future__ import annotations

import edgar

from fintin.adapters.edgar.client import EdgarClient


def fetch_company_facts(client: EdgarClient, cik: int):
    """Fetch one company's facts as an ``edgar.entity.EntityFacts`` (iterable of
    ``FinancialFact``), through the rate-limited client (AD-3)."""
    cik_int = int(cik)
    return client.run(
        lambda: edgar.get_company_facts(cik_int),
        description=f"companyfacts CIK {cik_int}",
    )


def edgartools_version() -> str:
    """The edgartools package version — stamped as ``taxonomy_version`` (AD-14)."""
    return edgar.__version__
