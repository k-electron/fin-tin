"""Pure scoped-recovery tests (no container, no network).

`recover_company` composes the real `ingest_company` (Tier 0) + `map_company`
(Tier 1) over injected ports. The fakes wire `read_raw_facts` to return exactly
what `insert_raw_rows` captured — simulating the store round-trip — so the re-map
reads the just-re-ingested Tier 0. No `edgar`, no ClickHouse.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from fintin.core.recover import RecoverReport, recover_company


@dataclass
class _Fact:
    """A minimal `FactLike` stub (structural — `core` needs no `edgar`)."""

    concept: str = "us-gaap:Revenues"
    taxonomy: str = "us-gaap"
    label: str = "Revenues"
    numeric_value: float | None = 1000.0
    unit: str = "USD"
    period_start: date | None = date(2023, 1, 1)
    period_end: date | None = date(2023, 12, 31)
    period_type: str = "duration"
    filing_date: date | None = date(2024, 2, 1)
    form_type: str = "10-K"
    accession: str = "0000320193-24-000001"
    is_dimensioned: bool = False


def _round_trip_store():
    """A fake store: `insert_raw_rows` captures Tier 0 rows; `read_raw_facts`
    returns them (post re-ingest); `insert_canonical_rows` captures Tier 1 rows."""
    raw: list = []
    canon: list = []

    def insert_raw(rows):
        raw.extend(rows)
        return len(rows)

    def read_raw(cik):
        return list(raw)

    def insert_canon(rows):
        canon.extend(rows)
        return len(rows)

    return insert_raw, read_raw, insert_canon, raw, canon


# --- recover_company: re-ingest Tier 0 then re-derive Tier 1 (AC-1, AC-2) -------


def test_recover_reingests_tier0_then_remaps_tier1():
    insert_raw, read_raw, insert_canon, raw, canon = _round_trip_store()
    report = recover_company(
        320193,
        fetch_facts=lambda c: [_Fact(concept="us-gaap:Revenues"), _Fact(concept="us-gaap:Assets")],
        insert_raw_rows=insert_raw,
        read_raw_facts=read_raw,
        insert_canonical_rows=insert_canon,
        taxonomy_version="5.43.0",
        raw_version=5,
        canonical_version=9,
    )
    assert isinstance(report, RecoverReport)
    assert report.cik == 320193
    assert report.rows_landed == 2  # Tier 0 re-ingested
    assert report.projected == 2  # Tier 1 re-derived
    assert report.raw_seen == 2  # the re-map read the re-ingested Tier 0 back
    # Tier 0 stamped raw_version; Tier 1 stamped canonical_version (AD-6 supersede).
    assert report.ingest.version == 5
    assert report.project.version == 9
    assert {r.version for r in raw} == {5}
    assert {r.version for r in canon} == {9}
    # Tier 1 canonical_concept = the element local name (namespace stripped).
    assert {r.canonical_concept for r in canon} == {"Revenues", "Assets"}


def test_recover_supersedes_by_higher_version():
    # The re-ingest stamps a strictly-higher version than the (corrupt) prior copy,
    # so FINAL/argMax reads return the fresh copy — recovery IS a scoped re-ingest.
    insert_raw, read_raw, insert_canon, raw, canon = _round_trip_store()
    report = recover_company(
        1,
        fetch_facts=lambda c: [_Fact(accession="0000000001-24-000001")],
        insert_raw_rows=insert_raw,
        read_raw_facts=read_raw,
        insert_canonical_rows=insert_canon,
        taxonomy_version="v",
        raw_version=42,  # > any prior corrupt copy
        canonical_version=43,
    )
    assert report.rows_landed == 1
    assert raw[0].version == 42
    assert canon[0].version == 43


def test_recover_zero_fact_company_runs_both_stages_cleanly():
    # A company that yields zero facts still runs both stages (0 landed, 0
    # projected) — not an error at the engine level.
    insert_raw, read_raw, insert_canon, raw, canon = _round_trip_store()
    report = recover_company(
        7,
        fetch_facts=lambda c: [],
        insert_raw_rows=insert_raw,
        read_raw_facts=read_raw,
        insert_canonical_rows=insert_canon,
        taxonomy_version="v",
        raw_version=1,
        canonical_version=1,
    )
    assert report.rows_landed == 0
    assert report.projected == 0
    assert report.raw_seen == 0
    assert raw == [] and canon == []


# --- purity guard ---------------------------------------------------------------


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


def test_core_recover_is_pure():
    """The recovery engine imports no `edgar`, ClickHouse, or `pyarrow` — it only
    composes `ingest_company` + `map_company` over injected ports."""
    imports = _module_imports("fintin/core/recover.py")
    assert "edgar" not in imports
    assert "clickhouse_connect" not in imports
    assert "pyarrow" not in imports
