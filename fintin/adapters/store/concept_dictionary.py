"""Versioned concept dictionary (AD-8, AD-9) — screening concept → ordered list of
standard XBRL element local names.

The wide screening mart (``schema.py``) resolves each concept to the **latest-filed**
value across its element union, breaking ties deterministically by **element
list-position** (then the AD-7 filing tiebreak). So the ORDER of ``elements``
matters: earlier = higher precedence on a same-filing tie. Ordering convention:
the FASB-primary element first, then observed-frequency fallbacks.

**Curation principle (AD-9, exactness):** a concept's ``elements`` are only *true
synonyms* — the same economic line reported under different tags across taxonomy
eras / cover-page conventions. Elements that denote a DIFFERENT total (e.g. equity
including vs excluding non-controlling interest, cash including vs excluding
restricted cash, revenue including vs excluding assessed taxes, net income vs
consolidated profit) are NOT unioned — mixing them would let recency silently
switch a company's series between magnitudes. When only a different-total element
is reported, the concept is simply absent for that filer (a coverage gap, FR-14),
never a wrong number. This is a curated, verified artifact: edgartools' statistical
standardization may *seed* candidates, but the stored lists are human-checked —
never machine-authoritative.

``unit`` is pinned per concept (v1 is us-gaap; monetary = ``USD``, share counts =
``shares``). ``period_type`` records whether the concept is a flow (``duration``:
income-statement / cash-flow) or a stock (``instant``: balance-sheet) — the
``screening_wide`` companion view uses it to place balance-sheet values alongside
the income period ending on the same date. Owned by ``adapters/store`` (AD-18) and
consumed only by the mart DDL builder. Bump ``DICTIONARY_VERSION`` when the lists
change; the mart is a ``CREATE OR REPLACE VIEW`` so re-running ``schema-init``
re-applies it (no data rebuild — the mart derives on read, AD-1).
"""

from __future__ import annotations

from typing import NamedTuple

DICTIONARY_VERSION = "2"


class ConceptDef(NamedTuple):
    alias: str  # screening_mart column name (SQL identifier)
    unit: str  # unit pin (e.g. 'USD', 'shares')
    period_type: str  # 'duration' (flow) or 'instant' (stock)
    elements: tuple[str, ...]  # ordered; earlier wins a same-filing tie


# v1 headline screening concepts. Bounded on purpose; coverage grows via the FR-14
# gap report + frequency data once the full Universe is backfilled (Epic 2).
CONCEPT_DICTIONARY: tuple[ConceptDef, ...] = (
    # --- income statement (flows) ---
    ConceptDef(
        "revenues",
        "USD",
        "duration",
        # Net top-line revenue across eras (ASC 606 net / legacy net sales). Excludes
        # the assessed-tax-inclusive variant (a different, gross figure).
        ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"),
    ),
    ConceptDef("cost_of_revenue", "USD", "duration", ("CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold")),
    ConceptDef("gross_profit", "USD", "duration", ("GrossProfit",)),
    ConceptDef("operating_income", "USD", "duration", ("OperatingIncomeLoss",)),
    # Net income attributable to the entity. NOT unioned with consolidated `ProfitLoss`
    # (incl. non-controlling interest) — a different total.
    ConceptDef("net_income", "USD", "duration", ("NetIncomeLoss",)),
    # --- balance sheet (stocks) ---
    ConceptDef("assets", "USD", "instant", ("Assets",)),
    ConceptDef("current_assets", "USD", "instant", ("AssetsCurrent",)),
    ConceptDef("liabilities", "USD", "instant", ("Liabilities",)),
    ConceptDef("current_liabilities", "USD", "instant", ("LiabilitiesCurrent",)),
    # Equity attributable to the parent. NOT unioned with the incl-NCI total.
    ConceptDef("stockholders_equity", "USD", "instant", ("StockholdersEquity",)),
    # Cash & equivalents excl. restricted cash (NOT the restricted-inclusive total).
    ConceptDef("cash_and_equivalents", "USD", "instant", ("CashAndCashEquivalentsAtCarryingValue",)),
    # Shares outstanding — us-gaap and dei cover-tag are true synonyms (distinct local names).
    ConceptDef("shares_outstanding", "shares", "instant", ("CommonStockSharesOutstanding", "EntityCommonStockSharesOutstanding")),
)
