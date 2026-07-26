"""Tier 0 ingestion — the pure transform + orchestration (AD-4, AD-14, AD-15).

This module is part of the pure core: it depends on nothing outward (no `edgar`,
no ClickHouse). It turns fetched facts into `raw_fact` rows — filtering to
consolidated, standard-taxonomy, finite-numeric facts and stamping full
provenance — and orchestrates one company's ingest via injected fetch/insert
callables (ports), so the concrete adapters stay outside `core`.

Landing a company (Story 1.4):

    facts = fetch_facts(cik)                      # adapters/edgar (rate-limited)
    rows, result = to_raw_fact_rows(facts, ...)   # this module (pure)
    insert_rows(rows)                             # adapters/store (raw_fact)

Filters (AD-9/AD-15/FR-3): drop dimensional/segment facts, non-standard
taxonomies, non-finite/non-numeric facts, and rows that can't form a valid
identity key. Provenance (AD-14): every row carries `content_hash` = sha256 of an
injection-proof encoding of the fact + `taxonomy_version`. Period representation
(AD-17): instant → period_start == period_end; a duration must have
period_start < period_end. Intra-batch identity-key collisions are de-duplicated
deterministically (last-wins) since every row of a run shares one `version`.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import date
from typing import NamedTuple, Optional, Protocol, runtime_checkable

# The three standard taxonomies fin-tin ingests (AD-9). Anything else stays out
# of Tier 0.
STANDARD_TAXONOMIES = frozenset({"us-gaap", "dei", "srt"})

# Canonical dashed accession, e.g. 0000320193-24-000123.
_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")


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
    period_type: str  # 'instant' | 'duration'
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
    deduped: int
    version: int
    dropped_out_of_range: int = 0

    @property
    def dropped(self) -> int:
        return (
            self.dropped_dimensional
            + self.dropped_non_standard
            + self.dropped_non_numeric
            + self.dropped_incomplete
            + self.dropped_out_of_range
            + self.deduped
        )


# ClickHouse `Date32` bounds — the widest date the store can hold.
#
# This guard is not theoretical: Oracle (CIK 1341439) files
# `RestructuringAndRelatedCostExpectedCost` with the sentinel range
# 1900-01-01 → 2199-12-31 for an open-ended expected cost. A company commits as
# ONE atomic insert, so a single unstorable date fails the whole company — that
# one row cost all 26,035 of Oracle's facts on the first full-market backfill.
# Dropping the offending fact here (counted, never silent) keeps the blast radius
# at one fact instead of one company.
_DATE_MIN = date(1900, 1, 1)
_DATE_MAX = date(2299, 12, 31)


def normalize_accession(accn: str) -> str:
    """Return the dashed 20-char canonical accession (`0000320193-24-000123`).
    Accepts an already-dashed value as-is; formats a bare 18-digit value. The
    caller validates the result against the canonical shape."""
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
    raw_label: str,
    period_start: date,
    period_end: date,
    unit: str,
    value: float,
    form: str,
    filed_date: date,
) -> str:
    """sha256 over an injection-proof encoding of the fact (AD-14). Hashing
    ``repr(tuple)`` quotes/escapes every string field, so no field value can
    collide with a different tuple by smuggling a separator; dates and floats
    ``repr`` deterministically. Covers identity + value + provenance (incl.
    ``raw_label``). Deterministic across runs (detects at-rest corruption)."""
    payload = repr(
        (
            int(cik),
            accession,
            raw_tag,
            taxonomy,
            raw_label,
            period_start,
            period_end,
            unit,
            float(value),
            form,
            filed_date,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def to_raw_fact_rows(
    facts: Iterable[FactLike],
    *,
    cik: int,
    taxonomy_version: str,
    version: int,
) -> tuple[list[RawFactRow], IngestResult]:
    """Filter + map fetched facts to Tier 0 rows. Pure (no I/O). Returns the rows
    (de-duplicated by identity key, last-wins) and an :class:`IngestResult`."""
    by_key: dict[tuple, RawFactRow] = {}
    seen = dropped_dim = dropped_std = dropped_num = dropped_incomplete = deduped = 0
    dropped_out_of_range = 0

    for f in facts:
        seen += 1
        if f.is_dimensioned:  # AD-15 consolidated-only
            dropped_dim += 1
            continue
        if f.taxonomy not in STANDARD_TAXONOMIES:  # AD-9
            dropped_std += 1
            continue
        # Numeric facts only (FR-3); reject NaN/inf so they can't poison Float64.
        if f.numeric_value is None or not math.isfinite(f.numeric_value):
            dropped_num += 1
            continue
        # Need non-null provenance + a valid identity key.
        if f.period_end is None or f.filing_date is None:
            dropped_incomplete += 1
            continue
        accession = normalize_accession(f.accession)
        if not _ACCESSION_RE.match(accession):
            dropped_incomplete += 1
            continue

        period_end = f.period_end
        if f.period_type == "instant":
            period_start = period_end  # AD-17: instant stored with equal bounds
        else:  # duration must be a real, forward, non-empty range
            if f.period_start is None or f.period_start >= period_end:
                dropped_incomplete += 1
                continue
            period_start = f.period_start

        # Every date must fit the store's Date32 columns. Checked BEFORE the row is
        # built so an unstorable date can never reach the driver and take the
        # company's whole atomic insert down with it (see _DATE_MIN/_DATE_MAX).
        if not all(
            _DATE_MIN <= d <= _DATE_MAX
            for d in (period_start, period_end, f.filing_date)
        ):
            dropped_out_of_range += 1
            continue

        raw_tag = f.concept  # full qualified element (namespace kept — key safety)
        value = float(f.numeric_value)
        row = RawFactRow(
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
                raw_label=f.label,
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

        # Identity key (AD-5/AD-15). Every row shares one `version`, so equal-key
        # rows would dedup nondeterministically under ReplacingMergeTree — collapse
        # them here, last-wins, deterministically.
        key = (accession, raw_tag, period_start, period_end, f.unit)
        if key in by_key:
            deduped += 1
        by_key[key] = row

    rows = list(by_key.values())
    result = IngestResult(
        cik=int(cik),
        facts_seen=seen,
        rows_landed=len(rows),
        dropped_dimensional=dropped_dim,
        dropped_non_standard=dropped_std,
        dropped_non_numeric=dropped_num,
        dropped_incomplete=dropped_incomplete,
        dropped_out_of_range=dropped_out_of_range,
        deduped=deduped,
        version=int(version),
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

    ``version`` should be supplied by the caller from a monotonic source
    (`raw_fact_repo.next_ingest_version`) so a later re-ingest always supersedes a
    corrupted prior copy (AD-6). It falls back to ``time.time_ns()`` only for
    direct/test callers with no store to consult.
    """
    run_version = version if version is not None else time.time_ns()
    facts = fetch_facts(int(cik))
    rows, result = to_raw_fact_rows(
        facts, cik=cik, taxonomy_version=taxonomy_version, version=run_version
    )
    insert_rows(rows)
    return result
