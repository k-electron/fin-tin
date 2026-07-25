"""Coverage & status — the pure coverage report (FR-14, SM-2, AD-1/AD-2).

Part of the pure core: it depends on nothing outward (no `edgar`, no ClickHouse).
It computes what fraction of the in-scope Universe is present in the store and
which in-scope companies are explained gaps — from plain values (the resolved
Universe, the set of present CIKs, the high-water mark) that the CLI fetches via
the store adapter, mirroring `resolve_universe` / `compute_work_list`.

Coverage is DERIVED, never stored (AD-1): there is no failures/status table. A
company that failed backfill is exactly one absent from `raw_fact`, so it surfaces
here as a zero-fact gap with the reason "no facts in store" — the durable
DB-derived state, not the ephemeral per-run failure message (that lives in
`fintin backfill --show-gaps` at run time).
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import date
from typing import NamedTuple

from fintin.core.universe import ResolvedUniverse, UniverseGap


class CoverageReport(NamedTuple):
    in_scope: int
    present: int
    zero_fact_ciks: tuple[int, ...]
    resolution_gaps: tuple[UniverseGap, ...]
    hwm: date | None

    @property
    def missing(self) -> int:
        """In-scope companies with zero facts in the store (explained gaps)."""
        return len(self.zero_fact_ciks)

    @property
    def total_gaps(self) -> int:
        """All explained gaps: unresolvable tickers + zero-fact companies."""
        return len(self.resolution_gaps) + self.missing

    @property
    def is_complete(self) -> bool:
        """Every in-scope company is present AND every configured ticker resolved."""
        return self.missing == 0 and not self.resolution_gaps


def compute_coverage(
    resolved: ResolvedUniverse,
    present: Collection[int],
    hwm: date | None,
) -> CoverageReport:
    """Derive coverage from the resolved Universe and store membership. Pure (no
    I/O). ``present`` is the subset of ``resolved.ciks`` found in the store
    (``present_ciks``); zero-fact gaps are the in-scope CIKs absent from it,
    **sorted** (deterministic). ``resolved.gaps`` (unresolvable tickers) pass
    through as the second class of explained gap — both are surfaced so there are
    no silent omissions (SM-2). Intersecting ``present`` with the in-scope set is
    defensive: a CIK present in the store but out of the current Universe scope
    does not count toward coverage."""
    in_scope = set(resolved.ciks)
    present_in_scope = in_scope & {int(c) for c in present}
    zero_fact = tuple(sorted(in_scope - present_in_scope))
    return CoverageReport(
        in_scope=len(in_scope),
        present=len(present_in_scope),
        zero_fact_ciks=zero_fact,
        resolution_gaps=resolved.gaps,
        hwm=hwm,
    )
