"""Pure coverage-report tests (no container, no network)."""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

from fintin.core.coverage import CoverageReport, compute_coverage
from fintin.core.universe import ResolvedUniverse, UniverseGap


def _resolved(ciks=(), gaps=(), tickers_resolved=0, explicit_ciks=0) -> ResolvedUniverse:
    return ResolvedUniverse(
        ciks=tuple(ciks),
        gaps=tuple(gaps),
        tickers_resolved=tickers_resolved,
        explicit_ciks=explicit_ciks,
    )


# --- compute_coverage (AC-1, AC-2) ---------------------------------------------


def test_all_present_is_complete():
    rep = compute_coverage(_resolved(ciks=(1, 2, 3)), present={1, 2, 3}, hwm=date(2024, 5, 1))
    assert isinstance(rep, CoverageReport)
    assert rep.in_scope == 3 and rep.present == 3
    assert rep.zero_fact_ciks == ()
    assert rep.missing == 0 and rep.total_gaps == 0
    assert rep.is_complete
    assert rep.hwm == date(2024, 5, 1)


def test_partial_coverage_zero_fact_gaps_are_sorted():
    # AC-2: in-scope companies absent from the store are the zero-fact gaps.
    rep = compute_coverage(_resolved(ciks=(1, 2, 3, 4)), present={3, 1}, hwm=date(2024, 1, 1))
    assert rep.present == 2
    assert rep.zero_fact_ciks == (2, 4)  # sorted, deterministic (in-scope − present)
    assert rep.missing == 2 and not rep.is_complete


def test_empty_store_all_in_scope_are_gaps():
    rep = compute_coverage(_resolved(ciks=(10, 20)), present=set(), hwm=None)
    assert rep.present == 0
    assert rep.zero_fact_ciks == (10, 20)
    assert rep.hwm is None  # empty store
    assert rep.total_gaps == 2 and not rep.is_complete


def test_resolution_gaps_pass_through_and_count():
    gap = UniverseGap("ZZZZ", "not found in edgartools reference data")
    rep = compute_coverage(_resolved(ciks=(1,), gaps=(gap,)), present={1}, hwm=date(2024, 2, 1))
    assert rep.missing == 0  # the one in-scope CIK is present
    assert rep.resolution_gaps == (gap,)
    assert rep.total_gaps == 1  # the unresolvable ticker is still a gap
    assert not rep.is_complete  # a resolution gap means not fully covered


def test_both_gap_classes_counted_together():
    gap = UniverseGap("BADTICK", "not found in edgartools reference data")
    rep = compute_coverage(_resolved(ciks=(1, 2), gaps=(gap,)), present={1}, hwm=date(2024, 3, 1))
    assert rep.missing == 1  # CIK 2 zero-fact
    assert len(rep.resolution_gaps) == 1
    assert rep.total_gaps == 2  # one of each class


def test_present_outside_scope_is_ignored():
    # A CIK in the store but not in the current Universe scope doesn't count.
    rep = compute_coverage(_resolved(ciks=(1, 2)), present={1, 999}, hwm=date(2024, 1, 1))
    assert rep.present == 1  # only CIK 1 is in scope
    assert rep.zero_fact_ciks == (2,)


def test_empty_universe_is_trivially_complete():
    rep = compute_coverage(_resolved(ciks=()), present=set(), hwm=None)
    assert rep.in_scope == 0 and rep.present == 0
    assert rep.zero_fact_ciks == () and rep.is_complete


# --- purity guard --------------------------------------------------------------


def _module_imports(path: str) -> set[str]:
    tree = ast.parse(Path(path).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                imported.add(n.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    return imported


def test_core_coverage_is_pure():
    """The coverage engine imports no `edgar`, ClickHouse, or `pyarrow` — the CLI
    fetches present-CIKs + high-water mark and passes plain values in."""
    imports = _module_imports("fintin/core/coverage.py")
    assert "edgar" not in imports
    assert "clickhouse_connect" not in imports
    assert "pyarrow" not in imports
