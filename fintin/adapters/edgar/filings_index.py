"""EDGAR filing-index discovery — through the single rate-limited client (AD-3).

Fetches SEC's **multi-filer quarterly full-index** via ``edgar.get_filings`` and
turns the rows for our Universe CIKs into :class:`~fintin.core.reconcile.WorkItem`s.
One gzip request per calendar quarter the window spans (FR-2) — not per-company
crawling. The fetch runs through :meth:`EdgarClient.run` so the fair-access
cool-down/retry (AD-3) covers it; ``get_filings`` requires a declared identity,
which the client sets on construction.

edgartools 5.43.0 realities (verified against installed source):
- ``get_filings(filing_date="A:B", form=[...])`` returns a ``Filings`` backed by a
  ``pyarrow.Table`` (``.data``) with columns ``form``, ``company``, ``cik``(int32),
  ``filing_date``(date32), ``accession_number``(dashed 20-char). Returns ``None``
  on an invalid/out-of-range date, or an empty ``Filings`` (``len == 0``) for a
  valid empty period — both yield an empty work list here.
- ``amendments=True`` (default) includes ``/A`` rows; no dedup (a co-filed accession
  repeats per filer CIK) — the core reconciler dedups by accession.
"""

from __future__ import annotations

from collections.abc import Collection

import edgar
import pyarrow as pa
import pyarrow.compute as pc

from fintin.core.ingest import normalize_accession
from fintin.core.reconcile import WorkItem

# Only the financial-statement forms carry the XBRL facts fin-tin ingests
# (matches the mart's periodic-form filter). ``/A`` amendments are auto-included
# by ``amendments=True``. Discovering other forms (e.g. Form 4) would propose
# accessions that never produce Tier 0 facts and would look permanently missing.
FINANCIAL_STATEMENT_FORMS = ("10-K", "10-Q")


def fetch_work_candidates(
    edgar_client,
    *,
    filing_date: str,
    ciks: Collection[int],
    forms: Collection[str] = FINANCIAL_STATEMENT_FORMS,
) -> list[WorkItem]:
    """Fetch index rows for ``filing_date`` (a single date or ``"A:B"`` range),
    restricted to ``ciks``, as :class:`WorkItem`s — through the rate-limited client
    (AD-3). Empty ``ciks`` short-circuits to ``[]`` (no request)."""
    if not ciks:
        return []
    filings = edgar_client.run(
        lambda: edgar.get_filings(filing_date=filing_date, form=list(forms)),
        description=f"filing index {filing_date}",
    )
    if filings is None or len(filings) == 0:
        return []
    return _filings_to_work_items(filings.data, ciks)


def _filings_to_work_items(table: "pa.Table", ciks: Collection[int]) -> list[WorkItem]:
    """Filter a ``Filings`` pyarrow table to ``ciks`` and build :class:`WorkItem`s.
    Pure over the table (no network) — the adapter's real logic, offline-testable
    with a synthetic table. Accessions are normalized (the index is already dashed,
    so this validates rather than transforms)."""
    cik_type = table.schema.field("cik").type
    mask = pc.is_in(table["cik"], value_set=pa.array(sorted(ciks), type=cik_type))
    hits = table.filter(mask)
    accessions = hits["accession_number"].to_pylist()
    hit_ciks = hits["cik"].to_pylist()
    hit_forms = hits["form"].to_pylist()
    hit_dates = hits["filing_date"].to_pylist()
    return [
        WorkItem(normalize_accession(a), int(c), f, d)
        for a, c, f, d in zip(accessions, hit_ciks, hit_forms, hit_dates)
    ]
