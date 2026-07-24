"""Tier 0 → Tier 1 projection tests — pure/offline. NEVER hit live EDGAR (NFR-7).

The projection is pure string logic (element = raw_tag namespace stripped), so
these tests need no edgartools at all — proving the core stays edgar-free.
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pytest

from fintin.core.canonical import (
    CanonicalFactRow,
    ProjectResult,
    local_name,
    map_company,
    to_canonical_fact_rows,
)
from fintin.core.ingest import RawFactRow

_TAXV = "5.43.0"


def _raw(**over) -> RawFactRow:
    base = dict(
        cik=320193,
        accession="0000320193-24-000123",
        raw_tag="us-gaap:Revenues",
        raw_label="Revenues",
        taxonomy="us-gaap",
        period_start=date(2023, 1, 1),
        period_end=date(2023, 12, 31),
        unit="USD",
        value=1000.0,
        form="10-K",
        filed_date=date(2024, 2, 1),
        content_hash="deadbeef",
        taxonomy_version=_TAXV,
        version=1,
    )
    base.update(over)
    return RawFactRow(**base)


def _project(rows, *, cik=320193, version=7):
    return to_canonical_fact_rows(rows, cik=cik, version=version)


# --- local_name projection ------------------------------------------------------


def test_local_name_strips_namespace():
    assert local_name("us-gaap:Assets") == "Assets"
    assert local_name("dei:EntityCommonStockSharesOutstanding") == "EntityCommonStockSharesOutstanding"
    assert local_name("srt:CumulativeEffectPeriodOfAdoptionAxis") == "CumulativeEffectPeriodOfAdoptionAxis"
    assert local_name("Assets") == "Assets"  # bare name unchanged


# --- 1:1 lossless projection with provenance ------------------------------------


def test_projects_element_with_provenance():
    rows, result = _project([_raw()])
    assert result.projected == 1 and result.raw_seen == 1
    row = rows[0]
    assert row.canonical_concept == "Revenues"  # the standard element, verbatim
    assert row.raw_tag == "us-gaap:Revenues"
    assert row.raw_label == "Revenues"
    assert row.content_hash == "deadbeef"  # carried over from Tier 0 (AD-14)
    assert row.taxonomy_version == _TAXV  # carried over from Tier 0 (AD-9)
    assert row.version == 7
    # identity fields preserved verbatim (AD-5)
    assert (row.accession, row.period_start, row.period_end, row.unit) == (
        "0000320193-24-000123",
        date(2023, 1, 1),
        date(2023, 12, 31),
        "USD",
    )
    # canonical_fact drops the raw `taxonomy` column entirely
    assert not hasattr(row, "taxonomy")


def test_every_fact_projects_no_drops():
    facts = [
        _raw(raw_tag="us-gaap:Assets"),
        _raw(raw_tag="dei:EntityPublicFloat"),
        _raw(raw_tag="srt:SomethingObscure"),  # still projects 1:1 — no standardization drop
    ]
    rows, result = _project(facts)
    assert result.raw_seen == 3 and result.projected == 3
    assert [r.canonical_concept for r in rows] == ["Assets", "EntityPublicFloat", "SomethingObscure"]


def test_named_construction_not_positional():
    """A positional copy of RawFactRow (taxonomy at index 4) into CanonicalFactRow
    (canonical_concept at index 3, no taxonomy) would smuggle the raw taxonomy into
    canonical_concept. Guard: canonical_concept is the element, never 'us-gaap'."""
    rows, _ = _project([_raw(taxonomy="us-gaap", raw_tag="us-gaap:Revenues")])
    assert rows[0].canonical_concept == "Revenues"
    assert rows[0].canonical_concept != "us-gaap"


def test_map_company_wires_ports():
    captured = {}

    def read(cik):
        assert cik == 320193
        return [_raw(), _raw(raw_tag="us-gaap:Assets")]

    def insert(rows):
        captured["rows"] = list(rows)
        return len(rows)

    result = map_company(320193, read_raw_facts=read, insert_rows=insert, version=42)
    assert isinstance(result, ProjectResult)
    assert result.projected == 2 and result.raw_seen == 2
    assert result.version == 42
    assert {r.canonical_concept for r in captured["rows"]} == {"Revenues", "Assets"}
    assert all(r.version == 42 for r in captured["rows"])  # stamped Tier 1 version


def test_empty_input_is_clean():
    rows, result = _project([])
    assert rows == [] and result.raw_seen == 0 and result.projected == 0


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


@pytest.mark.parametrize(
    "module",
    [
        "fintin/core/canonical.py",
        "fintin/adapters/store/canonical_fact_repo.py",
        "fintin/adapters/store/raw_fact_repo.py",
    ],
)
def test_map_path_modules_have_no_edgar_import(module):
    """AC-1 zero-network: no module on the map-canonical path imports `edgar` — the
    projection is pure string logic and the store repos touch only ClickHouse, so
    Tier 1 derivation is network-free by construction. A regression adding an
    `edgar` import to any of these fails CI. (The CLI `app.py` legitimately
    lazy-imports edgar inside the *ingest* command, so it can't be whole-module
    guarded; the map command's own import block is edgar-free by inspection.)"""
    assert "edgar" not in _module_imports(module)
