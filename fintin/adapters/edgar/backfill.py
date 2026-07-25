"""Per-company backfill strategy — the v1 `companyfacts` API implementation of
the pluggable :class:`~fintin.core.backfill.BackfillStrategy` (AD-13).

One `companyfacts` request returns a company's entire standard-taxonomy history,
so backfill is **one request per company** — the request-minimizing strategy for
the S&P 500-scale Universe (SM-C1). The bulk `companyfacts.zip` strategy (the
large/full-market path) is a future implementation of the same interface,
deferred until the Universe outgrows per-company scale.

All EDGAR access stays here in `adapters/edgar/` and routes through the one
rate-limited client (AD-3) via :func:`fetch_company_facts`.
"""

from __future__ import annotations

from collections.abc import Iterable

from fintin.adapters.edgar.client import EdgarClient
from fintin.adapters.edgar.facts import fetch_company_facts
from fintin.core.ingest import FactLike


class CompanyFactsStrategy:
    """Fetch a company's full history via the per-company `companyfacts` API.

    Satisfies :class:`~fintin.core.backfill.BackfillStrategy` structurally. Holds
    a single :class:`EdgarClient` (constructed once and reused across the whole
    backfill loop — a second construction would reset process-global edgar state).
    """

    name = "per-company"

    def __init__(self, client: EdgarClient) -> None:
        self._client = client

    def company_facts(self, cik: int) -> Iterable[FactLike]:
        """One company's facts through the rate-limited client. Raises
        ``NoCompanyFactsError`` when EDGAR has no companyfacts for the CIK — the
        backfill engine records that as an explained gap and continues."""
        return fetch_company_facts(self._client, int(cik))
