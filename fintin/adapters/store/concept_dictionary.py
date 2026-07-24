"""Versioned concept dictionary (AD-8, AD-9) — screening concept → ordered list of
standard XBRL element local names.

The wide screening mart (``schema.py``) resolves each concept to the **latest-filed**
value across its element union, breaking ties deterministically by **element
list-position** (then the AD-7 filing tiebreak). So the ORDER of ``elements``
matters: earlier = higher precedence on a same-filing tie. Ordering convention:
the FASB-primary element first, then observed-frequency fallbacks.

This is a **curated, verified** artifact (AD-9): edgartools' statistical
standardization may *seed* candidate lists, but the stored lists are human-checked
against FASB primaries + observed usage — never machine-authoritative. It is owned
by ``adapters/store`` (AD-18) and consumed only by the mart DDL builder.

``unit`` is pinned per concept (v1 is us-gaap; monetary = ``USD``, share counts =
``shares``) so a concept never silently mixes units. Bump ``DICTIONARY_VERSION``
when the lists change; the mart is a ``CREATE OR REPLACE VIEW`` so re-running
``schema-init`` re-applies it (no data rebuild — the mart derives on read, AD-1).
"""

from __future__ import annotations

from typing import NamedTuple

DICTIONARY_VERSION = "1"


class ConceptDef(NamedTuple):
    alias: str  # screening_mart column name
    unit: str  # unit pin (e.g. 'USD', 'shares')
    elements: tuple[str, ...]  # ordered; earlier wins a same-filing tie


# v1 headline screening concepts. Bounded on purpose; coverage grows via the FR-14
# gap report + frequency data once the full Universe is backfilled (Epic 2).
CONCEPT_DICTIONARY: tuple[ConceptDef, ...] = (
    ConceptDef(
        "revenues",
        "USD",
        (
            "RevenueFromContractWithCustomerExcludingAssessedTax",  # ASC 606 primary
            "Revenues",
            "SalesRevenueNet",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
        ),
    ),
    ConceptDef("cost_of_revenue", "USD", ("CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold")),
    ConceptDef("gross_profit", "USD", ("GrossProfit",)),
    ConceptDef("operating_income", "USD", ("OperatingIncomeLoss",)),
    ConceptDef("net_income", "USD", ("NetIncomeLoss", "ProfitLoss")),
    ConceptDef("assets", "USD", ("Assets",)),
    ConceptDef("current_assets", "USD", ("AssetsCurrent",)),
    ConceptDef("liabilities", "USD", ("Liabilities",)),
    ConceptDef("current_liabilities", "USD", ("LiabilitiesCurrent",)),
    ConceptDef(
        "stockholders_equity",
        "USD",
        ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
    ),
    ConceptDef(
        "cash_and_equivalents",
        "USD",
        ("CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
    ),
    ConceptDef("shares_outstanding", "shares", ("CommonStockSharesOutstanding", "EntityCommonStockSharesOutstanding")),
)
