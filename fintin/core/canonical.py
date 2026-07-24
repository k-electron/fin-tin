"""Tier 0 → Tier 1 canonical mapping — pure transform + orchestration (AD-4, AD-5, AD-9).

Part of the pure core: depends on nothing outward (no ``edgar``, no ClickHouse).
Maps each Tier 0 ``RawFactRow`` to a Tier 1 ``canonical_fact`` row by looking up
its canonical concept through an injected standardization **port**; a raw tag the
port cannot resolve produces no row (it stays only in Tier 0). Tier 1 is
rebuildable from Tier 0 with zero network (AD-4) — the standardization itself is
offline, performed by the adapter behind the port.

Mapping a company (Story 1.5):

    raw_rows = read_raw_facts(cik)                       # adapters/store (raw_fact FINAL)
    rows, result = to_canonical_fact_rows(raw_rows, ...) # this module (pure)
    insert_rows(rows)                                    # adapters/store (canonical_fact)

Identity (AD-5): the Tier 1 key is the Tier 0 key
``(accession, raw_tag, period_start, period_end, unit)`` — ``canonical_concept``
is an ATTRIBUTE, never part of the key. Because ``raw_tag`` stays in the key and
rows come from ``raw_fact FINAL`` (already unique by identity), each Tier 0 row
maps 1:1 to at most one Tier 1 row — no dedup needed here. Two different raw tags
that map to the SAME canonical concept keep distinct keys (resolving those is
Story 1.6's latest-filed-wins job). Provenance: ``content_hash`` is carried over
from Tier 0 (AD-14); every row is stamped with ``taxonomy_version`` (AD-9) and an
ingest-monotonic ``version`` (AD-6).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import date
from typing import NamedTuple

from fintin.core.ingest import RawFactRow


class CanonicalFactRow(NamedTuple):
    """One Tier 1 row. Field order MUST match the ``canonical_fact`` column order
    used by ``fintin.adapters.store.canonical_fact_repo`` — identity-key columns
    first (AD-5), ``canonical_concept`` right after ``raw_tag``, and NO ``taxonomy``
    column (Tier 1 drops it; the raw taxonomy lives only in Tier 0)."""

    cik: int
    accession: str
    raw_tag: str
    canonical_concept: str
    raw_label: str
    period_start: date
    period_end: date
    unit: str
    value: float
    form: str
    filed_date: date
    content_hash: str
    taxonomy_version: str
    version: int


class MapResult(NamedTuple):
    cik: int
    raw_seen: int
    mapped: int
    unmapped: int
    version: int


def to_canonical_fact_rows(
    raw_rows: Iterable[RawFactRow],
    *,
    cik: int,
    standardize: Callable[[str], str | None],
    taxonomy_version: str,
    version: int,
) -> tuple[list[CanonicalFactRow], MapResult]:
    """Map Tier 0 rows to Tier 1 rows. Pure (no I/O). A raw tag the injected
    ``standardize`` port cannot resolve (returns ``None``) produces no row (AC-2).

    Each ``CanonicalFactRow`` is built by NAMED field from the ``RawFactRow`` —
    dropping ``taxonomy`` and inserting ``canonical_concept`` — never positionally
    (their field orders differ; a positional copy would corrupt every column)."""
    rows: list[CanonicalFactRow] = []
    seen = mapped = unmapped = 0
    for r in raw_rows:
        seen += 1
        concept = standardize(r.raw_tag)
        if concept is None:  # unmappable/excluded → stays only in Tier 0 (AC-2)
            unmapped += 1
            continue
        rows.append(
            CanonicalFactRow(
                cik=r.cik,
                accession=r.accession,
                raw_tag=r.raw_tag,
                canonical_concept=concept,
                raw_label=r.raw_label,
                period_start=r.period_start,
                period_end=r.period_end,
                unit=r.unit,
                value=r.value,
                form=r.form,
                filed_date=r.filed_date,
                content_hash=r.content_hash,  # carried over (AD-5 attribute / AD-14 provenance)
                taxonomy_version=taxonomy_version,
                version=int(version),
            )
        )
        mapped += 1

    result = MapResult(
        cik=int(cik),
        raw_seen=seen,
        mapped=mapped,
        unmapped=unmapped,
        version=int(version),
    )
    return rows, result


def map_company(
    cik: int,
    *,
    read_raw_facts: Callable[[int], Iterable[RawFactRow]],
    standardize: Callable[[str], str | None],
    insert_rows: Callable[[Sequence[CanonicalFactRow]], int],
    taxonomy_version: str,
    version: int,
) -> MapResult:
    """Orchestrate one company's Tier 0 → Tier 1 mapping via injected ports (no
    adapter imports here — the CLI wires the concrete store read/insert + the
    offline standardizer). Reads Tier 0, writes Tier 1; zero network (AD-4)."""
    raw_rows = read_raw_facts(int(cik))
    rows, result = to_canonical_fact_rows(
        raw_rows,
        cik=cik,
        standardize=standardize,
        taxonomy_version=taxonomy_version,
        version=version,
    )
    insert_rows(rows)
    return result
