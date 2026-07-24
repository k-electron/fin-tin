"""Tier 0 ingestion — the pure transform + orchestration (AD-4, AD-14, AD-15).

This module is part of the pure core: it depends on nothing outward (no `edgar`,
no ClickHouse). It turns fetched facts into `raw_fact` rows — filtering to
consolidated, standard-taxonomy, numeric facts and stamping full provenance — and
orchestrates one company's ingest via injected fetch/insert callables (ports), so
the concrete adapters stay outside `core`.

Landing a company (Story 1.4):

    facts = fetch_facts(cik)                      # adapters/edgar (rate-limited)
    rows  = to_raw_fact_rows(facts, ...)          # this module (pure)
    n     = insert_rows(rows)                      # adapters/store (raw_fact)

Filters (AD-9/AD-15/FR-3): drop dimensional/segment facts, non-standard
taxonomies, and non-numeric facts. Provenance (AD-14): every row carries
`content_hash` = sha256 of the normalized fact tuple + `taxonomy_version`. Period
representation (AD-17): instant → period_start == period_end.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import date
from typing import NamedTuple, Optional, Protocol, runtime_checkable

# The three standard taxonomies fin-tin ingests (AD-9). Anything else stays out
# of Tier 0.
STANDARD_TAXONOMIES = frozenset({"us-gaap", "dei", "srt"})


@runtime_checkable
class FactLike(Protocol):
    """The shape `to_raw_fact_rows` needs from a fetched fact. edgartools'
    ``FinancialFact`` satisfies this structurally, so `core` never imports
    `edgar` (keeps the dependency pointing inward; AD-3 import guard)."""

    concept: str
    taxonomy: str
    label: str
    numeric_value: Optional[float]
    unit: str
    period_start: Optional[date]
    period_end: Optional[date]
    filing_date: Optional[date]
    form_type: str
    accession: str

    @property
    def is_dimensioned(self) -> bool: ...


class RawFactRow(NamedTuple):
    """One Tier 0 row. Field order MUST match the `raw_fact` column order used by
    `fintin.adapters.store.raw_fact_repo` (AD-5/AD-15 identity key first columns)."""

    cik: int
    accession: str
    raw_tag: str
    raw_label: str
    taxonomy: str
    period_start: date
    period_end: date
    unit: str
    value: float
    form: str
    filed_date: date
    content_hash: str
    taxonomy_version: str
    version: int


class IngestResult(NamedTuple):
    cik: int
    facts_seen: int
    rows_landed: int
    dropped_dimensional: int
    dropped_non_standard: int
    dropped_non_numeric: int
    dropped_incomplete: int

    @property
    def dropped(self) -> int:
        return (
            self.dropped_dimensional
            + self.dropped_non_standard
            + self.dropped_non_numeric
            + self.dropped_incomplete
        )


def normalize_accession(accn: str) -> str:
    """Return the dashed 20-char canonical accession (`0000320193-24-000123`).
    Accepts an already-dashed value as-is; formats a bare 18-digit value."""
    a = (accn or "").strip()
    if "-" in a:
        return a
    if len(a) == 18 and a.isdigit():
        return f"{a[:10]}-{a[10:12]}-{a[12:]}"
    return a


def content_hash(
    *,
    cik: int,
    accession: str,
    raw_tag: str,
    taxonomy: str,
    period_start: date,
    period_end: date,
    unit: str,
    value: float,
    form: str,
    filed_date: date,
) -> str:
    """sha256 over a stable serialization of the normalized fact tuple (AD-14).
    Deterministic across runs (detects at-rest corruption later). The unit
    separator ``\\x1f`` cannot appear in any field; ``repr(float(value))`` is
    round-trippable; dates are ISO-8601."""
    parts = [
        str(int(cik)),
        accession,
        raw_tag,
        taxonomy,
        period_start.isoformat(),
        period_end.isoformat(),
        unit,
        repr(float(value)),
        form,
        filed_date.isoformat(),
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def to_raw_fact_rows(
    facts: Iterable[FactLike],
    *,
    cik: int,
    taxonomy_version: str,
    version: int,
) -> tuple[list[RawFactRow], IngestResult]:
    """Filter + map fetched facts to Tier 0 rows. Pure (no I/O). Returns the rows
    and an :class:`IngestResult` with per-reason drop counts."""
    rows: list[RawFactRow] = []
    seen = dropped_dim = dropped_std = dropped_num = dropped_incomplete = 0

    for f in facts:
        seen += 1
        if f.is_dimensioned:  # AD-15 consolidated-only
            dropped_dim += 1
            continue
        if f.taxonomy not in STANDARD_TAXONOMIES:  # AD-9
            dropped_std += 1
            continue
        if f.numeric_value is None:  # numeric facts only (FR-3)
            dropped_num += 1
            continue
        # Need the identity key + non-nullable provenance columns.
        if not f.accession or f.period_end is None or f.filing_date is None:
            dropped_incomplete += 1
            continue

        period_end = f.period_end
        # AD-17: instant facts have no start → store period_start == period_end.
        period_start = f.period_start if f.period_start is not None else period_end
        accession = normalize_accession(f.accession)
        raw_tag = f.concept  # full qualified element (namespace kept — key safety)
        value = float(f.numeric_value)

        rows.append(
            RawFactRow(
                cik=int(cik),
                accession=accession,
                raw_tag=raw_tag,
                raw_label=f.label,
                taxonomy=f.taxonomy,
                period_start=period_start,
                period_end=period_end,
                unit=f.unit,
                value=value,
                form=f.form_type,
                filed_date=f.filing_date,
                content_hash=content_hash(
                    cik=cik,
                    accession=accession,
                    raw_tag=raw_tag,
                    taxonomy=f.taxonomy,
                    period_start=period_start,
                    period_end=period_end,
                    unit=f.unit,
                    value=value,
                    form=f.form_type,
                    filed_date=f.filing_date,
                ),
                taxonomy_version=taxonomy_version,
                version=int(version),
            )
        )

    result = IngestResult(
        cik=int(cik),
        facts_seen=seen,
        rows_landed=len(rows),
        dropped_dimensional=dropped_dim,
        dropped_non_standard=dropped_std,
        dropped_non_numeric=dropped_num,
        dropped_incomplete=dropped_incomplete,
    )
    return rows, result


def ingest_company(
    cik: int,
    *,
    fetch_facts: Callable[[int], Iterable[FactLike]],
    insert_rows: Callable[[Sequence[RawFactRow]], int],
    taxonomy_version: str,
    version: int | None = None,
) -> IngestResult:
    """Orchestrate one company's Tier 0 ingest via injected ports (no adapter
    imports here — the CLI wires the concrete edgar fetch + store insert).

    ``version`` defaults to an ingest-monotonic ``time.time_ns()`` stamped on
    every row of this run (AD-6) so a later re-ingest supersedes a corrupted copy.
    """
    run_version = version if version is not None else time.time_ns()
    facts = fetch_facts(int(cik))
    rows, result = to_raw_fact_rows(
        facts, cik=cik, taxonomy_version=taxonomy_version, version=run_version
    )
    insert_rows(rows)
    return result
