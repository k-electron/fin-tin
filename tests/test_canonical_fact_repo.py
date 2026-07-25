"""Tier 1 read/write + re-projection idempotency tests (require a running ClickHouse).

Each test runs against a unique throwaway database (Story 1.2 pattern), so the
real `default` DB is never touched. Auto-skipped when ClickHouse isn't listening
(see conftest.py). No EDGAR at all — the Tier 0 → Tier 1 projection is pure string
logic (element = raw_tag namespace stripped).
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import date

import pytest

from fintin.adapters.store import schema as store_schema
from fintin.adapters.store.canonical_fact_repo import (
    insert_canonical_facts,
    mapped_ciks,
    next_canonical_version,
)
from fintin.adapters.store.client import get_client
from fintin.adapters.store.raw_fact_repo import insert_raw_facts, read_raw_facts
from fintin.core.canonical import CanonicalFactRow, map_company
from fintin.core.ingest import RawFactRow


@pytest.fixture
def schema_client(local_clickhouse_config):
    db = f"fintin_test_{uuid.uuid4().hex[:12]}"
    admin = get_client(local_clickhouse_config)
    try:
        admin.command(f"CREATE DATABASE {db}")
    finally:
        admin.close()

    client = None
    try:
        client = get_client(local_clickhouse_config, database=db)
        store_schema.create_schema(client)
        yield client
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()
        cleanup = get_client(local_clickhouse_config)
        try:
            cleanup.command(f"DROP DATABASE IF EXISTS {db}")
        finally:
            cleanup.close()


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
        content_hash="h1",
        taxonomy_version="5.43.0",
        version=1,
    )
    base.update(over)
    return RawFactRow(**base)


def _canon(**over) -> CanonicalFactRow:
    base = dict(
        cik=320193,
        accession="0000320193-24-000123",
        raw_tag="us-gaap:Revenues",
        canonical_concept="Revenues",  # the standard element, verbatim
        raw_label="Revenues",
        period_start=date(2023, 1, 1),
        period_end=date(2023, 12, 31),
        unit="USD",
        value=1000.0,
        form="10-K",
        filed_date=date(2024, 2, 1),
        content_hash="h1",
        taxonomy_version="5.43.0",
        version=1,
    )
    base.update(over)
    return CanonicalFactRow(**base)


@pytest.mark.integration
def test_read_raw_facts_roundtrip(schema_client):
    """read_raw_facts returns RawFactRow tuples with the exact SELECT column order,
    round-tripping dates/floats/ids through ClickHouse."""
    original = _raw(raw_tag="us-gaap:Assets", value=42.0, content_hash="rr")
    insert_raw_facts(schema_client, [original])
    got = read_raw_facts(schema_client, 320193)
    assert len(got) == 1
    assert got[0] == original  # positional NamedTuple equality — column order correct


@pytest.mark.integration
def test_insert_and_read_back_canonical(schema_client):
    n = insert_canonical_facts(
        schema_client,
        [
            _canon(raw_tag="us-gaap:Revenues", canonical_concept="Revenues", value=1000.0),
            _canon(raw_tag="us-gaap:Assets", canonical_concept="Assets", value=500.0,
                   period_start=date(2023, 12, 31)),
        ],
    )
    assert n == 2
    rows = schema_client.query(
        "SELECT raw_tag, canonical_concept, value, taxonomy_version "
        "FROM canonical_fact FINAL WHERE cik = 320193 ORDER BY raw_tag"
    ).result_rows
    assert len(rows) == 2
    assets, revenues = rows
    assert assets[0] == "us-gaap:Assets" and assets[1] == "Assets" and assets[2] == 500.0
    assert revenues[0] == "us-gaap:Revenues" and revenues[1] == "Revenues"
    assert revenues[3] == "5.43.0"


@pytest.mark.integration
def test_empty_insert_is_noop(schema_client):
    assert insert_canonical_facts(schema_client, []) == 0
    assert schema_client.query("SELECT count() FROM canonical_fact").result_rows[0][0] == 0


@pytest.mark.integration
def test_next_canonical_version_is_monotonic(schema_client):
    assert next_canonical_version(schema_client) == 1  # empty table
    insert_canonical_facts(schema_client, [_canon(version=5, content_hash="a")])
    assert next_canonical_version(schema_client) == 6
    insert_canonical_facts(
        schema_client, [_canon(raw_tag="us-gaap:Assets", canonical_concept="Assets", version=6, content_hash="b")]
    )
    assert next_canonical_version(schema_client) == 7


@pytest.mark.integration
def test_reproject_idempotent_on_read(schema_client):
    """AC-3: re-projecting the SAME identity with a higher version is an in-place
    upsert on read — one row per identity key, the higher version wins, and it
    stays correct across a background merge (OPTIMIZE FINAL)."""
    insert_canonical_facts(schema_client, [_canon(value=200.0, version=1, content_hash="v1")])
    # Re-project: same identity key, higher ingest version, corrected value.
    insert_canonical_facts(schema_client, [_canon(value=100.0, version=2, content_hash="v2")])

    def final():
        return schema_client.query(
            "SELECT count(), any(value) FROM canonical_fact FINAL WHERE cik = 320193"
        ).result_rows[0]

    n, val = final()
    assert n == 1 and val == 100.0  # one row per key, higher version wins pre-merge
    schema_client.command("OPTIMIZE TABLE canonical_fact FINAL")
    n, val = final()
    assert n == 1 and val == 100.0  # ...and after a merge


@pytest.mark.integration
def test_end_to_end_project_lights_up_mart(schema_client):
    """End-to-end seam (offline, no edgar): land Tier 0 facts → read_raw_facts →
    project to element-keyed Tier 1 → insert_canonical_facts → the resolution MV
    auto-populates and the wide mart's first-present concept dictionary returns the
    values. `us-gaap:Revenues` projects to the element `Revenues`, which the
    `revenues` column's ordered element list resolves; `us-gaap:Assets` → `Assets`."""
    insert_raw_facts(
        schema_client,
        [
            _raw(raw_tag="us-gaap:Revenues", value=400.0,
                 period_start=date(2023, 1, 1), period_end=date(2023, 12, 31)),  # duration
            _raw(raw_tag="us-gaap:Assets", value=100.0,
                 period_start=date(2023, 12, 31), period_end=date(2023, 12, 31),
                 content_hash="h2"),  # instant
        ],
    )
    version = next_canonical_version(schema_client)
    result = map_company(
        320193,
        read_raw_facts=lambda c: read_raw_facts(schema_client, c),
        insert_rows=lambda rows: insert_canonical_facts(schema_client, rows),
        version=version,
    )
    assert result.raw_seen == 2 and result.projected == 2

    revenues = schema_client.query(
        "SELECT revenues FROM screening_mart "
        "WHERE cik = 320193 AND period_start = '2023-01-01' AND period_end = '2023-12-31'"
    ).result_rows
    assert revenues and revenues[0][0] == 400.0
    assets = schema_client.query(
        "SELECT assets FROM screening_mart "
        "WHERE cik = 320193 AND period_start = '2023-12-31' AND period_end = '2023-12-31'"
    ).result_rows
    assert assets and assets[0][0] == 100.0


@pytest.mark.integration
def test_first_present_precedence_deterministic(schema_client):
    """The concept dictionary resolves synonymous elements by first-present
    precedence: with BOTH RevenueFromContractWithCustomerExcludingAssessedTax (list
    position 1) and Revenues (position 2) present for one (cik, period), the mart
    deterministically returns the position-1 element's value — no nondeterministic
    collision (the finding the AD-9 pivot retires)."""
    insert_canonical_facts(
        schema_client,
        [
            _canon(raw_tag="us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                   canonical_concept="RevenueFromContractWithCustomerExcludingAssessedTax",
                   value=900.0, content_hash="excl"),
            _canon(raw_tag="us-gaap:Revenues", canonical_concept="Revenues",
                   value=950.0, content_hash="rev"),  # includes excise tax, different value
        ],
    )
    val = schema_client.query(
        "SELECT revenues FROM screening_mart "
        "WHERE cik = 320193 AND period_start = '2023-01-01' AND period_end = '2023-12-31'"
    ).result_rows[0][0]
    assert val == 900.0  # position-1 element wins deterministically


# --- mapped_ciks: the Tier 1 half of backfill's both-tier resume test ------------


@pytest.mark.integration
def test_mapped_ciks_reports_only_companies_present_in_tier1(schema_client):
    insert_canonical_facts(schema_client, [_canon(cik=320193)])
    # Asked about three, only the one with Tier 1 rows comes back.
    assert mapped_ciks(schema_client, ciks=[320193, 789019, 1652044]) == {320193}


@pytest.mark.integration
def test_mapped_ciks_empty_input_is_a_noop(schema_client):
    assert mapped_ciks(schema_client, ciks=[]) == set()


@pytest.mark.integration
def test_tier_split_company_is_not_counted_as_done(schema_client):
    """The hazard the both-tier resume test exists for: a company whose Tier 0
    landed but whose projection failed has raw rows and NO canonical rows, so
    `present_ciks & mapped_ciks` must exclude it and a resumed backfill retries it
    (rather than skipping it forever on Tier 0 presence alone)."""
    from fintin.adapters.store.raw_fact_repo import present_ciks

    insert_raw_facts(
        schema_client,
        [
            RawFactRow(
                cik=999, accession="0000000999-24-000001", raw_tag="us-gaap:Revenues",
                raw_label="Revenues", taxonomy="us-gaap",
                period_start=date(2023, 1, 1), period_end=date(2023, 12, 31),
                unit="USD", value=5.0, form="10-K", filed_date=date(2024, 2, 1),
                content_hash="split", taxonomy_version="5.43.0", version=1,
            )
        ],
    )
    scope = [999]
    assert present_ciks(schema_client, ciks=scope) == {999}  # Tier 0 present
    assert mapped_ciks(schema_client, ciks=scope) == set()  # Tier 1 absent
    done = present_ciks(schema_client, ciks=scope) & mapped_ciks(
        schema_client, ciks=scope
    )
    assert done == set(), "a tier-split company must not count as already done"
