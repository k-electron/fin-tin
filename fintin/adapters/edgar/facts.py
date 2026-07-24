"""EDGAR company-facts fetch — through the single rate-limited client (AD-3).

The only place `edgar.get_company_facts` is called. The fetch is routed through
:meth:`EdgarClient.run` so the fair-access cool-down (Story 1.3) covers the
companyfacts download (which raises ``TooManyRequestsError`` on a 429).
"""

from __future__ import annotations

import edgar

from fintin.adapters.edgar.client import EdgarClient


class NoCompanyFactsError(RuntimeError):
    """Raised when EDGAR has no companyfacts for a CIK (unknown CIK, no XBRL
    facts, or a response that failed to parse). ``edgar.get_company_facts``
    returns ``None`` in these cases rather than raising."""


def fetch_company_facts(client: EdgarClient, cik: int):
    """Fetch one company's facts as an ``edgar.entity.EntityFacts`` (iterable of
    ``FinancialFact``), through the rate-limited client (AD-3).

    ``edgar.get_company_facts`` returns ``None`` (not an exception) for an
    empty/unknown/unparseable response — surface that as a clear
    :class:`NoCompanyFactsError` so callers don't hit a downstream
    ``TypeError: 'NoneType' object is not iterable``."""
    cik_int = int(cik)
    facts = client.run(
        lambda: edgar.get_company_facts(cik_int),
        description=f"companyfacts CIK {cik_int}",
    )
    if facts is None:
        raise NoCompanyFactsError(
            f"No companyfacts returned for CIK {cik_int} "
            f"(unknown CIK, no XBRL facts, or the response failed to parse)."
        )
    return facts


def edgartools_version() -> str:
    """The edgartools package version — stamped as ``taxonomy_version`` (AD-14)."""
    return edgar.__version__
