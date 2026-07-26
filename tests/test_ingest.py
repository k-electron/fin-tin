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
        taxonomy="us-gaap", raw_label="Revenues", period_start=date(2023, 1, 1),
        period_end=date(2023, 12, 31), unit="USD", value=1000.0, form="10-K",
        filed_date=date(2024, 2, 1),
    )
    assert content_hash(**kw) == content_hash(**kw)


def test_content_hash_changes_with_value_and_label():
    base = dict(
        cik=320193, accession="0000320193-24-000123", raw_tag="us-gaap:Revenues",
        taxonomy="us-gaap", raw_label="Revenues", period_start=date(2023, 1, 1),
        period_end=date(2023, 12, 31), unit="USD", value=1000.0, form="10-K",
        filed_date=date(2024, 2, 1),
    )
    assert content_hash(**base) != content_hash(**{**base, "value": 1000.01})
    assert content_hash(**base) != content_hash(**{**base, "raw_label": "Revenue"})


def test_content_hash_injection_proof():
    # A field value can't forge a collision by embedding a separator-like string.
    a = dict(
        cik=1, accession="0000000001-24-000001", raw_tag="us-gaap:A", taxonomy="us-gaap",
        raw_label="x", period_start=date(2023, 1, 1), period_end=date(2023, 12, 31),
        unit="USD", value=1.0, form="10-K", filed_date=date(2024, 2, 1),
    )
    b = {**a, "raw_tag": "us-gaap:A\x1fus-gaap", "taxonomy": ""}  # would collide under a raw join
    assert content_hash(**a) != content_hash(**b)


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


def test_ingest_company_stamps_supplied_version():
    captured = {}

    def fake_insert(rows):
        captured["rows"] = list(rows)
        return len(rows)

    result = ingest_company(
        7, fetch_facts=lambda c: [_fact()], insert_rows=fake_insert,
        taxonomy_version=_TAXV, version=555,
    )
    assert result.version == 555
    assert captured["rows"][0].version == 555


# --- review-round additions ----------------------------------------------------


def test_non_finite_value_dropped():
    rows, result = _rows([_fact(numeric_value=float("nan")), _fact(numeric_value=float("inf"))])
    assert rows == [] and result.dropped_non_numeric == 2


def test_duration_missing_start_dropped():
    rows, result = _rows([_fact(period_type="duration", period_start=None)])
    assert rows == [] and result.dropped_incomplete == 1


def test_reversed_or_zero_length_duration_dropped():
    rows, result = _rows(
        [
            _fact(period_start=date(2023, 12, 31), period_end=date(2023, 1, 1)),  # reversed
            _fact(period_start=date(2023, 6, 30), period_end=date(2023, 6, 30)),  # zero-length duration
        ]
    )
    assert rows == [] and result.dropped_incomplete == 2


def test_noncanonical_accession_dropped():
    rows, result = _rows(
        [
            _fact(accession="   "),  # whitespace — must NOT slip through as ""
            _fact(accession="abc-def"),  # has a dash but wrong shape
            _fact(accession="12345"),  # too short
        ]
    )
    assert rows == [] and result.dropped_incomplete == 3


def test_intra_batch_duplicate_identity_key_deduped_last_wins():
    # Same (accession, raw_tag, period_start, period_end, unit), different value.
    rows, result = _rows(
        [
            _fact(numeric_value=100.0, label="first"),
            _fact(numeric_value=200.0, label="second"),
        ]
    )
    assert result.rows_landed == 1 and result.deduped == 1
    assert rows[0].value == 200.0  # last wins, deterministically


def test_fixture_parse_transform_pins_value_scale():
    """Parse a hand-crafted companyfacts JSON (offline) through edgartools'
    parser and confirm the landed value is the ACTUAL 'val' — a guard against a
    future edgartools change that starts applying `scale` (off-by-1000×)."""
    from edgar.entity.parser import EntityFactsParser

    cf = {
        "cik": 320193,
        "entityName": "TEST CO",
        "facts": {
            "us-gaap": {
                "Revenues": {"label": "Revenues", "units": {"USD": [
                    {"start": "2023-01-01", "end": "2023-12-31", "val": 123456,
                     "accn": "0000320193-24-000123", "fy": 2023, "fp": "FY",
                     "form": "10-K", "filed": "2024-02-01"},
                ]}},
                "Assets": {"label": "Assets", "units": {"USD": [
                    {"end": "2023-12-31", "val": 999, "accn": "0000320193-24-000123",
                     "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2024-02-01"},
                ]}},
            }
        },
    }
    facts = EntityFactsParser.parse_company_facts(cf)
    assert facts is not None
    rows, result = _rows(facts)
    assert result.rows_landed == 2
    by_tag = {r.raw_tag: r for r in rows}
    assert by_tag["us-gaap:Revenues"].value == 123456.0  # actual val, NOT scaled
    assert by_tag["us-gaap:Assets"].period_start == by_tag["us-gaap:Assets"].period_end  # instant


# --- out-of-range dates: one bad fact must not cost a whole company --------------
# Regression for a live failure. Oracle (CIK 1341439) files
# `RestructuringAndRelatedCostExpectedCost` with the sentinel range
# 1900-01-01 -> 2199-12-31. A company commits as ONE atomic insert, so that single
# unstorable date failed the whole company: all 26,035 Oracle facts were lost on
# the first full-market backfill. It was deferred as "unreachable for real
# SEC/XBRL data" — it is not.


def test_oracles_sentinel_dates_are_stored_not_dropped():
    """The exact live case. Because the columns are now Date32, Oracle's sentinel
    range is *storable* — so the fact is kept rather than dropped, and the
    company's insert no longer fails. Preserving the fact is the better outcome;
    the drop guard below is only for dates past even Date32."""
    rows, result = _rows(
        [
            _fact(
                concept="us-gaap:RestructuringAndRelatedCostExpectedCost",
                period_start=date(1900, 1, 1),
                period_end=date(2199, 12, 31),
                filing_date=date(2010, 9, 29),
            ),
            _fact(),  # an ordinary fact from the same company
        ]
    )
    assert result.rows_landed == 2  # BOTH land — nothing lost
    assert result.dropped_out_of_range == 0
    sentinel = next(r for r in rows if "Restructuring" in r.raw_tag)
    assert sentinel.period_start == date(1900, 1, 1)
    assert sentinel.period_end == date(2199, 12, 31)


def test_dates_beyond_date32_are_dropped_not_passed_to_the_driver():
    """Widening to Date32 is not total cover — a filer could stamp 9999-12-31.
    Anything past the column's range is dropped here, so it can never reach the
    driver and fail the company's insert."""
    rows, result = _rows(
        [
            _fact(period_start=date(2023, 1, 1), period_end=date(9999, 12, 31)),
            _fact(period_start=date(1, 1, 1), period_end=date(1850, 1, 1)),
            _fact(filing_date=date(9999, 1, 1)),
            _fact(),  # good
        ]
    )
    assert result.dropped_out_of_range == 3
    assert result.rows_landed == 1
    assert all(
        date(1900, 1, 1) <= r.period_start <= date(2299, 12, 31) for r in rows
    )


def test_out_of_range_drops_are_counted_in_the_dropped_total():
    _, result = _rows([_fact(period_end=date(9999, 12, 31))])
    assert result.dropped_out_of_range == 1
    assert result.dropped >= 1  # surfaced in the reported total, never silent
    assert result.rows_landed == 0
