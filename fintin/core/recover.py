"""Scoped recovery — the pure engine (FR-6, AD-14, AD-2).

Part of the pure core: depends on nothing outward (no ``edgar``, no ClickHouse).
Recovery of a corrupt or lost Tier 0 copy is a **scoped re-ingest** (AD-14), NOT a
new subsystem: :func:`recover_company` composes two primitives that already exist —
:func:`~fintin.core.ingest.ingest_company` (re-land one company's Tier 0 from
``companyfacts``, superseding the prior copy with a higher ingest-monotonic
``version``, AD-6) and :func:`~fintin.core.canonical.map_company` (re-derive its
Tier 1 from the fresh Tier 0). Because the wide mart resolves on read over
``canonical_fact`` and the resolution MV fires on its insert (Story 1.6), the
re-map flows straight through to resolution + the mart — so re-mapping Tier 1
completes "re-derives Tier 1 → resolution → mart" with no explicit rebuild.

Same-taxonomy re-ingest: recovery re-fetches the same company under the same
taxonomy, so every fact's ``canonical_concept`` is unchanged and version-based
supersession resolves cleanly (the cross-taxonomy re-map caveat does not apply).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import NamedTuple

from fintin.core.canonical import CanonicalFactRow, ProjectResult, map_company
from fintin.core.ingest import FactLike, IngestResult, RawFactRow, ingest_company


class RecoverReport(NamedTuple):
    """The outcome of a scoped recovery: the Tier 0 re-ingest and the Tier 1
    re-derivation of one company."""

    cik: int
    ingest: IngestResult
    project: ProjectResult

    @property
    def rows_landed(self) -> int:
        """Tier 0 rows re-ingested (superseding the prior copy)."""
        return self.ingest.rows_landed

    @property
    def projected(self) -> int:
        """Tier 1 rows re-derived (flow to resolution + the mart)."""
        return self.project.projected

    @property
    def raw_seen(self) -> int:
        """Tier 0 rows the re-map read back (post re-ingest)."""
        return self.project.raw_seen


def recover_company(
    cik: int,
    *,
    fetch_facts: Callable[[int], Iterable[FactLike]],
    insert_raw_rows: Callable[[Sequence[RawFactRow]], int],
    read_raw_facts: Callable[[int], Iterable[RawFactRow]],
    insert_canonical_rows: Callable[[Sequence[CanonicalFactRow]], int],
    taxonomy_version: str,
    raw_version: int,
    canonical_version: int,
) -> RecoverReport:
    """Repair one company (FR-6, AD-14): re-ingest its Tier 0 from EDGAR
    (superseding the prior copy at ``raw_version`` — a value strictly greater than
    the corrupt copy's, AD-6), then re-derive its Tier 1 at ``canonical_version``
    (which flows to resolution + the mart, Story 1.6). Pure: the CLI injects the
    concrete EDGAR fetch + store read/insert ports. Reuses
    :func:`ingest_company` + :func:`map_company` — no new subsystem (thin)."""
    ingest = ingest_company(
        cik,
        fetch_facts=fetch_facts,
        insert_rows=insert_raw_rows,
        taxonomy_version=taxonomy_version,
        version=raw_version,
    )
    project = map_company(
        cik,
        read_raw_facts=read_raw_facts,
        insert_rows=insert_canonical_rows,
        version=canonical_version,
    )
    return RecoverReport(cik=int(cik), ingest=ingest, project=project)
