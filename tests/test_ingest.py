"""Tier 0 ingestion tests — pure/offline; NEVER hit live EDGAR (NFR-7).

The filter/map transform is exercised by constructing edgartools ``FinancialFact``
objects directly — the only way to cover the dimensioned-drop branch (AC-2), since
the companyfacts API never emits dimensioned facts. Fetch routing and the
orchestrator use fakes; nothing leaves the process.
"""

from __future__ import annotations

from datetime import date

from edgar.entity.models import FinancialFact

from fintin.core.ingest import (
    STANDARD_TAXONOMIES,
    IngestResult,
    content_hash,
    ingest_company,
    normalize_accession,
    to_raw_fact_rows,
)

_TAXV = "5.43.0"


def _fact(**over) -> FinancialFact:
    base = dict(
        concept="us-gaap:Revenues",
        taxonomy="us-gaap",
        label="Revenues",
        value=1000.0,
        numeric_value=1000.0,
        unit="USD",
        period_start=date(2023, 1, 1),
        period_end=date(2023, 12, 31),
        period_type="duration",
        filing_date=date(2024, 2, 1),
        form_type="10-K",
        accession="0000320193-24-000123",
    )
    base.update(over)
    return FinancialFact(**base)


def _rows(facts, *, cik=320193, version=1):
    return to_raw_fact_rows(facts, cik=cik, taxonomy_version=_TAXV, version=version)


# --- AC-1: standard numeric facts land with full provenance --------------------


def test_us_gaap_numeric_fact_lands_with_provenance():
    rows, result = _rows([_fact()])
    assert result.rows_landed == 1 and result.facts_seen == 1 and result.dropped == 0
    row = rows[0]
    assert row.cik == 320193
    assert row.accession == "0000320193-24-000123"
    assert row.raw_tag == "us-gaap:Revenues"  # full qualified (namespace kept)
    assert row.raw_label == "Revenues"
    assert row.taxonomy == "us-gaap"
    assert row.unit == "USD"
    assert row.value == 1000.0
    assert row.form == "10-K"
    assert row.filed_date == date(2024, 2, 1)
    assert row.taxonomy_version == _TAXV
    assert row.version == 1
    assert len(row.content_hash) == 64  # sha256 hex


def test_dei_and_srt_land():
    rows, result = _rows(
        [
            _fact(taxonomy="dei", concept="dei:EntityCommonStockSharesOutstanding", unit="shares"),
            _fact(taxonomy="srt", concept="srt:...", label="srt fact"),
        ]
    )
    assert result.rows_landed == 2
    assert {r.taxonomy for r in rows} == {"dei", "srt"}


def test_standard_taxonomies_constant():
    assert STANDARD_TAXONOMIES == {"us-gaap", "dei", "srt"}


# --- AC-2 / AC-4 / numeric: drops ----------------------------------------------


def test_dimensioned_fact_dropped():
    rows, result = _rows([_fact(dimensions={"us-gaap:StatementBusinessSegmentsAxis": "SegMember"})])
    assert rows == [] and result.dropped_dimensional == 1


def test_non_standard_taxonomy_dropped():
    rows, result = _rows([_fact(taxonomy="ifrs-full", concept="ifrs-full:Revenue")])
    assert rows == [] and result.dropped_non_standard == 1


def test_non_numeric_fact_dropped():
    rows, result = _rows([_fact(numeric_value=None, value="n/a")])
    assert rows == [] and result.dropped_non_numeric == 1


def test_incomplete_facts_dropped():
    rows, result = _rows(
        [
            _fact(accession=""),  # no identity anchor
            _fact(period_end=None),  # no period
            _fact(filing_date=None),  # no provenance date
        ]
    )
    assert rows == [] and result.dropped_incomplete == 3


# --- AD-17 period representation -----------------------------------------------


def test_instant_fact_period_collapsed():
    rows, _ = _rows(
        [_fact(concept="us-gaap:Assets", period_type="instant", period_start=None, period_end=date(2023, 12, 31))]
    )
    assert rows[0].period_start == rows[0].period_end == date(2023, 12, 31)


def test_duration_fact_period_preserved():
    rows, _ = _rows([_fact()])
    assert rows[0].period_start < rows[0].period_end


# --- AD-14 content hash --------------------------------------------------------


def test_content_hash_deterministic():
    kw = dict(
        cik=320193, accession="0000320193-24-000123", raw_tag="us-gaap:Revenues",
        taxonomy="us-gaap", period_start=date(2023, 1, 1), period_end=date(2023, 12, 31),
        unit="USD", value=1000.0, form="10-K", filed_date=date(2024, 2, 1),
    )
    assert content_hash(**kw) == content_hash(**kw)


def test_content_hash_changes_with_value():
    base = dict(
        cik=320193, accession="0000320193-24-000123", raw_tag="us-gaap:Revenues",
        taxonomy="us-gaap", period_start=date(2023, 1, 1), period_end=date(2023, 12, 31),
        unit="USD", value=1000.0, form="10-K", filed_date=date(2024, 2, 1),
    )
    other = {**base, "value": 1000.01}
    assert content_hash(**base) != content_hash(**other)


def test_normalize_accession():
    assert normalize_accession("0000320193-24-000123") == "0000320193-24-000123"
    assert normalize_accession("000032019324000123") == "0000320193-24-000123"


# --- AC-5: fetch routes through the rate-limited client ------------------------


def test_fetch_routes_through_client_run():
    from fintin.adapters.edgar.facts import fetch_company_facts

    seen = {}

    class _FakeClient:
        def run(self, operation, *, description="EDGAR request"):
            seen["description"] = description
            # Do NOT execute `operation` — it would call edgar.get_company_facts (network).
            return "SENTINEL_FACTS"

    assert fetch_company_facts(_FakeClient(), 320193) == "SENTINEL_FACTS"
    assert "320193" in seen["description"]


# --- orchestrator (pure, injected ports) ---------------------------------------


def test_ingest_company_wires_fetch_transform_insert():
    facts = [_fact(), _fact(taxonomy="ifrs-full"), _fact(dimensions={"Axis": "Member"})]
    captured = {}

    def fake_fetch(cik):
        assert cik == 320193
        return facts

    def fake_insert(rows):
        captured["rows"] = list(rows)
        return len(rows)

    result = ingest_company(
        320193,
        fetch_facts=fake_fetch,
        insert_rows=fake_insert,
        taxonomy_version=_TAXV,
        version=42,
    )
    assert isinstance(result, IngestResult)
    assert result.rows_landed == 1  # only the consolidated us-gaap numeric fact
    assert result.dropped == 2
    assert len(captured["rows"]) == 1
    assert captured["rows"][0].version == 42  # stamped ingest version


def test_ingest_company_default_version_is_monotonic_ns():
    def fake_fetch(cik):
        return [_fact()]

    def fake_insert(rows):
        return len(rows)

    r1 = ingest_company(1, fetch_facts=fake_fetch, insert_rows=fake_insert, taxonomy_version=_TAXV)
    r2 = ingest_company(1, fetch_facts=fake_fetch, insert_rows=fake_insert, taxonomy_version=_TAXV)
    assert r1.rows_landed == r2.rows_landed == 1  # both runs land; version defaulted (time_ns)
