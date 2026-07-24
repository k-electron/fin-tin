"""Tier 0 → Tier 1 canonical projection — pure transform + orchestration (AD-4, AD-5, AD-9).

Part of the pure core: depends on nothing outward (no ``edgar``, no ClickHouse).
Projects each Tier 0 ``RawFactRow`` to a Tier 1 ``canonical_fact`` row whose
``canonical_concept`` is the fact's **standard XBRL element** — the local name of
``raw_tag`` with its namespace stripped (``us-gaap:Assets`` → ``Assets``). This is
a **1:1, lossless** projection: exact and unambiguous by construction (each element
is FASB-defined and identical across filers, AD-9), so no statistical
standardization and no network are involved — Tier 1 is rebuildable from Tier 0
with zero network (AD-4).

Cross-company *screening* concepts (revenue, net income, …) are NOT built here;
they are the versioned concept dictionary over these elements, applied in the
wide mart (Story 1.6, AD-8/AD-9).

Projecting a company (Story 1.5):

    raw_rows = read_raw_facts(cik)                       # adapters/store (raw_fact FINAL)
    rows, result = to_canonical_fact_rows(raw_rows, ...) # this module (pure)
    insert_rows(rows)                                    # adapters/store (canonical_fact)

Identity (AD-5): the Tier 1 key is the Tier 0 key
``(accession, raw_tag, period_start, period_end, unit)`` — ``canonical_concept``
is an ATTRIBUTE, never part of the key. Because ``raw_tag`` stays in the key and
rows come from ``raw_fact FINAL`` (already unique by identity), each Tier 0 row
maps 1:1 to exactly one Tier 1 row — no dedup, no drops. Provenance ``content_hash``
and ``taxonomy_version`` are carried over from Tier 0 (AD-14); each row is stamped
with an ingest-monotonic ``version`` (AD-6).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import date
from typing import NamedTuple

from fintin.core.ingest import RawFactRow


def local_name(raw_tag: str) -> str:
    """The standard element local name = ``raw_tag`` with any namespace prefix
    stripped (``us-gaap:Assets`` → ``Assets``, ``dei:...`` → ``...``). Tier 0
    tags are always namespace-qualified; a bare name is returned unchanged."""
    return raw_tag.split(":", 1)[1] if ":" in raw_tag else raw_tag


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


class ProjectResult(NamedTuple):
    cik: int
    raw_seen: int
    projected: int
    version: int


def to_canonical_fact_rows(
    raw_rows: Iterable[RawFactRow],
    *,
    cik: int,
    version: int,
) -> tuple[list[CanonicalFactRow], ProjectResult]:
    """Project Tier 0 rows to Tier 1 rows. Pure (no I/O). Every row projects 1:1
    (``canonical_concept`` = the element local name); nothing is dropped.

    Each ``CanonicalFactRow`` is built by NAMED field from the ``RawFactRow`` —
    dropping ``taxonomy`` and inserting ``canonical_concept`` — never positionally
    (their field orders differ; a positional copy would corrupt every column)."""
    rows: list[CanonicalFactRow] = []
    seen = 0
    for r in raw_rows:
        seen += 1
        rows.append(
            CanonicalFactRow(
                cik=r.cik,
                accession=r.accession,
                raw_tag=r.raw_tag,
                canonical_concept=local_name(r.raw_tag),
                raw_label=r.raw_label,
                period_start=r.period_start,
                period_end=r.period_end,
                unit=r.unit,
                value=r.value,
                form=r.form,
                filed_date=r.filed_date,
                content_hash=r.content_hash,  # carried over (AD-5 attribute / AD-14 provenance)
                taxonomy_version=r.taxonomy_version,  # carried over from Tier 0 (AD-9)
                version=int(version),
            )
        )

    result = ProjectResult(
        cik=int(cik),
        raw_seen=seen,
        projected=len(rows),
        version=int(version),
    )
    return rows, result


def map_company(
    cik: int,
    *,
    read_raw_facts: Callable[[int], Iterable[RawFactRow]],
    insert_rows: Callable[[Sequence[CanonicalFactRow]], int],
    version: int,
) -> ProjectResult:
    """Orchestrate one company's Tier 0 → Tier 1 projection via injected ports (no
    adapter imports here — the CLI wires the concrete store read/insert). Reads
    Tier 0, writes Tier 1; zero network by construction (no ``edgar`` anywhere in
    this path, AD-4)."""
    raw_rows = read_raw_facts(int(cik))
    rows, result = to_canonical_fact_rows(raw_rows, cik=cik, version=version)
    insert_rows(rows)
    return result
