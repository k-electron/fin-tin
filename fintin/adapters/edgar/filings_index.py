"""EDGAR filing-index discovery — through the single rate-limited client (AD-3).

Fetches SEC's **multi-filer quarterly full-index** via ``edgar.get_filings`` and
turns the rows for our Universe CIKs into :class:`~fintin.core.reconcile.WorkItem`s.
One gzip request per calendar quarter the window spans (FR-2) — not per-company
crawling. The fetch runs through :meth:`EdgarClient.run` so the fair-access
cool-down/retry (AD-3) covers it; ``get_filings`` requires a declared identity,
which the client sets on construction.

**Form scope.** We keep the periodic-report family **`10-K*` / `10-Q*`** — matched
by prefix so it stays in lock-step with the mart's `startsWith(form,'10-K') OR
startsWith(form,'10-Q')` filter (`10-K`, `10-Q`, their `/A`, plus `10-KT`,
`10-KSB`, `10-K405`, `10-QT`, …). We do NOT pass ``get_filings(form=…)`` because it
matches form strings **exactly** (it would drop `10-KT` etc.); the form filter is
client-side anyway (no request saved), so we fetch all forms and prefix-filter here.
Non-periodic forms (8-K, Form 4) are excluded — they carry no XBRL financial
statements. Foreign annual forms (20-F/40-F) DO carry XBRL but are foreign private
issuers, out of v1 scope (see deferred-work.md); the v1 Universe is US-domestic.

edgartools 5.43.0 realities (verified against installed source):
- ``get_filings(filing_date="A:B")`` returns a ``Filings`` backed by a
  ``pyarrow.Table`` (``.data``) with columns ``form``, ``company``, ``cik``(int32),
  ``filing_date``(date32), ``accession_number``(dashed 20-char). Returns ``None``
  on an invalid/out-of-range date, or an empty ``Filings`` (``len == 0``) for a
  valid empty period — both yield an empty work list here.
- No dedup (a co-filed accession repeats per filer CIK) — the core reconciler
  dedups by accession.
"""

from __future__ import annotations

from collections.abc import Collection

import edgar
import pyarrow as pa
import pyarrow.compute as pc

from fintin.core.ingest import _ACCESSION_RE, normalize_accession
from fintin.core.reconcile import WorkItem

# Periodic-report form prefixes — kept in lock-step with the mart's periodic-form
# filter (schema.py `_PERIODIC_FORMS`). Prefix match captures /A and transition
# variants (10-KT, 10-QT, 10-KSB, …) that an exact form list would miss.
PERIODIC_FORM_PREFIXES = ("10-K", "10-Q")


def fetch_work_candidates(
    edgar_client,
    *,
    filing_date: str,
    ciks: Collection[int],
) -> list[WorkItem]:
    """Fetch index rows for ``filing_date`` (a single date or ``"A:B"`` range),
    restricted to ``ciks`` and the periodic-report family, as :class:`WorkItem`s —
    through the rate-limited client (AD-3). Empty ``ciks`` short-circuits to ``[]``
    (no request)."""
    if not ciks:
        return []
    filings = edgar_client.run(
        lambda: edgar.get_filings(filing_date=filing_date),
        description=f"filing index {filing_date}",
    )
    if filings is None or len(filings) == 0:
        return []
    return _filings_to_work_items(filings.data, ciks)


def _filings_to_work_items(table: "pa.Table", ciks: Collection[int]) -> list[WorkItem]:
    """Filter a ``Filings`` pyarrow table to ``ciks`` ∩ the periodic-report family
    and build :class:`WorkItem`s. Pure over the table (no network) — the adapter's
    real logic, offline-testable with a synthetic table.

    Robustness: the ``cik`` value-set is built as int64 so a Universe CIK above the
    index column's int32 range never overflows; rows with a null ``filing_date``/
    ``accession_number`` or a non-canonical accession are dropped (the index is
    normally well-formed, so this is defense-in-depth)."""
    # int64 value-set + int64-cast column so a >int32 (but valid UInt32) CIK can't
    # raise ArrowInvalid.
    cik_col = pc.cast(table["cik"], pa.int64())
    cik_mask = pc.is_in(cik_col, value_set=pa.array(sorted(ciks), type=pa.int64()))
    # Prefix-match the periodic-report family (parity with the mart).
    form_mask = pc.starts_with(table["form"], PERIODIC_FORM_PREFIXES[0])
    for prefix in PERIODIC_FORM_PREFIXES[1:]:
        form_mask = pc.or_(form_mask, pc.starts_with(table["form"], prefix))
    # Drop null accession / filing_date up front.
    valid = pc.and_(
        pc.is_valid(table["accession_number"]), pc.is_valid(table["filing_date"])
    )
    hits = table.filter(pc.and_(pc.and_(cik_mask, form_mask), valid))

    accessions = hits["accession_number"].to_pylist()
    hit_ciks = hits["cik"].to_pylist()
    hit_forms = hits["form"].to_pylist()
    hit_dates = hits["filing_date"].to_pylist()

    items: list[WorkItem] = []
    for accession, cik, form, filed_date in zip(
        accessions, hit_ciks, hit_forms, hit_dates
    ):
        normalized = normalize_accession(accession)
        if not _ACCESSION_RE.match(normalized):
            continue  # skip a malformed accession rather than emit a phantom item
        items.append(WorkItem(normalized, int(cik), form, filed_date))
    return items
