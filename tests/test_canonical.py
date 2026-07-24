"""Tier 0 → Tier 1 mapping tests — pure/offline. NEVER hit live EDGAR (NFR-7).

The transform uses an injected fake standardizer (a plain dict lookup), so these
tests are pure and need no edgartools at all — proving the core stays edgar-free.
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

from fintin.core.canonical import (
    CanonicalFactRow,
    MapResult,
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


# A fake standardizer port: name-only dict lookup, None for anything else.
def _fake_std(tag: str) -> str | None:
    return {"us-gaap:Revenues": "Revenue", "us-gaap:Assets": "Assets"}.get(tag)


def _map(rows, *, cik=320193, version=7):
    return to_canonical_fact_rows(
        rows, cik=cik, standardize=_fake_std, taxonomy_version=_TAXV, version=version
    )


def test_maps_known_concept_with_provenance():
    rows, result = _map([_raw()])
    assert result.mapped == 1 and result.raw_seen == 1 and result.unmapped == 0
    row = rows[0]
    assert row.canonical_concept == "Revenue"
    assert row.raw_tag == "us-gaap:Revenues"
    assert row.raw_label == "Revenues"
    assert row.content_hash == "deadbeef"  # carried over from Tier 0 (AD-14)
    assert row.taxonomy_version == _TAXV
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


def test_unmappable_produces_no_row():
    rows, result = _map([_raw(raw_tag="us-gaap:ZzzFake")])
    assert rows == [] and result.unmapped == 1 and result.mapped == 0


def test_named_construction_not_positional():
    """A positional copy of RawFactRow (taxonomy at index 4) into CanonicalFactRow
    (canonical_concept at index 3, no taxonomy) would smuggle the raw taxonomy into
    canonical_concept. Guard: canonical_concept is the MAPPED value, never 'us-gaap'."""
    rows, _ = _map([_raw(taxonomy="us-gaap")])
    assert rows[0].canonical_concept == "Revenue"
    assert rows[0].canonical_concept != "us-gaap"


def test_two_raw_tags_same_concept_keep_two_rows():
    # Both map to one canonical concept; distinct raw_tag → distinct identity key.
    def std(_tag: str) -> str:
        return "Revenue"

    rows, result = to_canonical_fact_rows(
        [_raw(raw_tag="us-gaap:Revenues"), _raw(raw_tag="us-gaap:SalesRevenueNet")],
        cik=320193,
        standardize=std,
        taxonomy_version=_TAXV,
        version=1,
    )
    assert result.mapped == 2
    assert {r.raw_tag for r in rows} == {"us-gaap:Revenues", "us-gaap:SalesRevenueNet"}
    assert {r.canonical_concept for r in rows} == {"Revenue"}


def test_map_company_wires_ports():
    captured = {}

    def read(cik):
        assert cik == 320193
        return [_raw(), _raw(raw_tag="us-gaap:ZzzFake")]

    def insert(rows):
        captured["rows"] = list(rows)
        return len(rows)

    result = map_company(
        320193,
        read_raw_facts=read,
        standardize=_fake_std,
        insert_rows=insert,
        taxonomy_version=_TAXV,
        version=42,
    )
    assert isinstance(result, MapResult)
    assert result.mapped == 1 and result.unmapped == 1 and result.raw_seen == 2
    assert result.version == 42
    assert len(captured["rows"]) == 1
    assert captured["rows"][0].version == 42  # stamped Tier 1 version


def test_empty_input_is_clean():
    rows, result = _map([])
    assert rows == [] and result.raw_seen == 0 and result.mapped == 0


def test_core_canonical_has_no_edgar_import():
    """AD-4/AD-9 layering: the pure core must not import `edgar` — standardization
    is injected as a port so Tier 1 derivation stays network-free by construction."""
    src = Path("fintin/core/canonical.py").read_text()
    tree = ast.parse(src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                imported.add(n.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])
    assert "edgar" not in imported
