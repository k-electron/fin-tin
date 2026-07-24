"""Work-list reconciler — the pure core (AD-1, AD-10, AD-16).

Derives outstanding ingestion work from two inputs:
  * ``candidates`` — filings discovered in EDGAR's index over the scan window,
    already restricted to the Universe (produced by the edgar adapter), and
  * ``present_accessions`` — accessions already in the store (from the store adapter).

The work list = candidates − present, with **per-accession membership** as the
correctness authority (AD-16). This module holds no ports and no I/O: the adapters
produce the two inputs; core just windows and diffs. Nothing is persisted — the
work list is derived every run (AD-1); there is no cursor (AD-10).

Pure: imports only the stdlib. No ``edgar``, no ClickHouse, no ``pyarrow``.
"""

from __future__ import annotations

from collections.abc import Container, Iterable
from datetime import date, timedelta
from typing import NamedTuple


class WorkItem(NamedTuple):
    """One outstanding filing to ingest (an EDGAR index row scoped to the Universe)."""

    accession: str  # dashed 20-char canonical accession
    cik: int
    form: str
    filed_date: date


class WorkList(NamedTuple):
    """The derived work list. ``scanned`` = distinct candidate accessions seen;
    ``already_present`` = how many were dropped as already in the store."""

    items: tuple[WorkItem, ...]
    scanned: int
    already_present: int


def resolve_window(
    hwm: date | None, lookback_days: int, today: date
) -> tuple[date, date]:
    """The scan window `[window_start, today]` (AD-16). ``window_start`` reaches
    ``lookback_days`` before the high-water mark — the reordering-safe skew — so a
    filing filed just before the HWM but not yet committed is re-checked. ``hwm``
    is only a **scan-sizing hint**; on an empty store (``hwm is None``) the window
    is anchored at ``today`` (a bounded recent window — full history from empty is
    the per-company backfill's job, Story 2.3), never the done-ness test."""
    anchor = hwm if hwm is not None else today
    return anchor - timedelta(days=lookback_days), today


def compute_work_list(
    candidates: Iterable[WorkItem], present_accessions: Container[str]
) -> WorkList:
    """Diff index ``candidates`` against store ``present_accessions`` (AD-16
    membership). Candidates are deduplicated by accession (first-wins — a co-filed
    accession appears once per filer CIK in the index); an accession already in the
    store is dropped (not re-fetched, AC-2); survivors are returned **sorted** by
    ``(filed_date, accession)`` for deterministic output."""
    seen: dict[str, WorkItem] = {}
    for item in candidates:
        seen.setdefault(item.accession, item)

    already_present = 0
    items: list[WorkItem] = []
    for accession, item in seen.items():
        if accession in present_accessions:
            already_present += 1
            continue
        items.append(item)

    items.sort(key=lambda w: (w.filed_date, w.accession))
    return WorkList(
        items=tuple(items),
        scanned=len(seen),
        already_present=already_present,
    )
