"""Schema DDL tests (require a running ClickHouse container).

Each test runs against a unique throwaway database so the real `default` DB is
never touched. Auto-skipped when ClickHouse isn't listening (see conftest.py).
"""

from __future__ import annotations

import contextlib
import uuid

import pytest

from fintin.adapters.store import schema as store_schema
from fintin.adapters.store.client import get_client
from fintin.adapters.store.concept_dictionary import ConceptDef

_COLS = (
    "cik, accession, raw_tag, canonical_concept, raw_label, period_start, "
    "period_end, unit, value, form, filed_date, content_hash, taxonomy_version, version"
)


@pytest.fixture
def schema_client(local_clickhouse_config):
    """Create a throwaway database, yield a client pointed at it, drop it after.

    The database is dropped even if client acquisition fails after CREATE.
    """
    db = f"fintin_test_{uuid.uuid4().hex[:12]}"
    admin = get_client(local_clickhouse_config)
    try:
        admin.command(f"CREATE DATABASE {db}")
    finally:
        admin.close()

    client = None
    try:
        client = get_client(local_clickhouse_config, database=db)
        yield client, db
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()
        cleanup = get_client(local_clickhouse_config)
        try:
            cleanup.command(f"DROP DATABASE IF EXISTS {db}")
        finally:
            cleanup.close()


@pytest.mark.integration
def test_tier0_tier1_engines_and_key(schema_client):
    """AC-1: Tier 0/1 are ReplacingMergeTree on the identity key with UInt64 version."""
    client, db = schema_client
    store_schema.create_schema(client)
    for table in ("raw_fact", "canonical_fact"):
        rows = client.query(
            f"SELECT engine, engine_full, sorting_key FROM system.tables "
            f"WHERE database = '{db}' AND name = '{table}'"
        ).result_rows
        assert rows, f"{table} not created"
        engine, engine_full, sorting_key = rows[0]
        assert engine == "ReplacingMergeTree"
        # ingest-monotonic version is the ReplacingMergeTree parameter (not filed_date)
        assert "ReplacingMergeTree(version)" in engine_full
        assert sorting_key == "accession, raw_tag, period_start, period_end, unit"
        vcol = client.query(
            f"SELECT type FROM system.columns "
            f"WHERE database = '{db}' AND table = '{table}' AND name = 'version'"
        ).result_rows
        assert vcol and vcol[0][0] == "UInt64"


@pytest.mark.integration
def test_mart_and_wide_view_created(schema_client):
    """The screening surfaces exist — and the superseded resolution layer does not.

    Story 1.2's AC-2 also required an element-grained resolution MV (resolved_fact
    + resolved_fact_mv). Story 1.6's Approach B moved concept resolution to a view
    derived on read over `canonical_fact`, leaving that MV read by nothing while it
    still fired on every Tier 1 insert, so it was dropped (see the deferred-work
    ledger). This asserts it is not silently re-created.
    """
    client, db = schema_client
    store_schema.create_schema(client)
    names = {
        r[0]
        for r in client.query(
            f"SELECT name FROM system.tables WHERE database = '{db}'"
        ).result_rows
    }
    assert {"raw_fact", "canonical_fact", "screening_mart", "screening_wide"} <= names
    assert not ({"resolved_fact", "resolved_fact_mv"} & names)


@pytest.mark.integration
def test_schema_init_is_idempotent(schema_client):
    """AC-3: running create_schema twice is a no-op (no error, no duplicates)."""
    client, db = schema_client
    store_schema.create_schema(client)
    before = client.query(
        f"SELECT count() FROM system.tables WHERE database = '{db}'"
    ).result_rows[0][0]
    store_schema.create_schema(client)  # second run
    after = client.query(
        f"SELECT count() FROM system.tables WHERE database = '{db}'"
    ).result_rows[0][0]
    # Derived from the DDL list rather than hardcoded: each statement creates exactly
    # one `system.tables` entry, so adding or removing an object can't silently drift
    # this count out of date (a hardcoded 6 did exactly that when the superseded
    # resolution MV was dropped).
    expected = len(store_schema.SCHEMA_STATEMENTS)
    assert before == after == expected


@pytest.mark.integration
def test_drop_schema_removes_every_object_and_is_idempotent(schema_client):
    """`drop_schema` wipes exactly what `create_schema` made, and a second drop on
    the now-empty database is a no-op rather than an error."""
    client, db = schema_client
    store_schema.create_schema(client)

    def _names():
        return {
            r[0]
            for r in client.query(
                f"SELECT name FROM system.tables WHERE database = '{db}'"
            ).result_rows
        }

    assert _names(), "nothing created, so the drop below would prove nothing"
    dropped = store_schema.drop_schema(client)
    assert set(dropped) == {obj.name for obj in store_schema.SCHEMA_STATEMENTS}
    assert _names() == set()
    assert store_schema.drop_schema(client) == dropped  # idempotent


@pytest.mark.integration
def test_drop_schema_leaves_foreign_objects_alone(schema_client):
    """The wipe is scoped to fin-tin's declared objects — a neighbouring table in
    the same database survives (this is the difference from nuking the volume)."""
    client, db = schema_client
    store_schema.create_schema(client)
    client.command("CREATE TABLE not_ours (x UInt8) ENGINE = MergeTree ORDER BY x")

    store_schema.drop_schema(client)

    names = {
        r[0]
        for r in client.query(
            f"SELECT name FROM system.tables WHERE database = '{db}'"
        ).result_rows
    }
    assert names == {"not_ours"}
    client.command("DROP TABLE not_ours")


@pytest.mark.integration
def test_drop_then_create_round_trips(schema_client):
    """Reset --recreate: a wipe followed by a rebuild yields the same object set."""
    client, db = schema_client
    before = set(store_schema.create_schema(client))
    store_schema.drop_schema(client)
    after = set(store_schema.create_schema(client))
    assert before == after
    rows = client.query("SELECT count() FROM raw_fact").result_rows[0][0]
    assert rows == 0  # rebuilt empty


@pytest.mark.integration
def test_instant_and_duration_representation(schema_client):
    """AC-4: instant facts store period_start == period_end; duration start < end,
    and both flow through the MV into the wide mart."""
    client, db = schema_client
    store_schema.create_schema(client)
    client.command(
        f"INSERT INTO canonical_fact ({_COLS}) VALUES "
        "(320193,'0000320193-24-000001','us-gaap:Assets','Assets','Assets',"
        "'2024-09-28','2024-09-28','USD',100.0,'10-K','2024-11-01','h1','1',1),"
        "(320193,'0000320193-24-000001','us-gaap:Revenues','Revenues','Revenues',"
        "'2023-10-01','2024-09-28','USD',400.0,'10-K','2024-11-01','h2','1',1)"
    )
    inst = client.query(
        "SELECT period_start, period_end FROM canonical_fact "
        "WHERE canonical_concept = 'Assets'"
    ).result_rows[0]
    assert inst[0] == inst[1]  # instant
    dur = client.query(
        "SELECT period_start, period_end FROM canonical_fact "
        "WHERE canonical_concept = 'Revenues'"
    ).result_rows[0]
    assert dur[0] < dur[1]  # duration

    # instant Assets resolves in its own period row
    assets = client.query(
        "SELECT assets FROM screening_mart "
        "WHERE cik = 320193 AND period_start = '2024-09-28' AND period_end = '2024-09-28'"
    ).result_rows
    assert assets and assets[0][0] == 100.0
    # duration Revenues resolves in its own period row
    revenues = client.query(
        "SELECT revenues FROM screening_mart "
        "WHERE cik = 320193 AND period_start = '2023-10-01' AND period_end = '2024-09-28'"
    ).result_rows
    assert revenues and revenues[0][0] == 400.0


@pytest.mark.integration
def test_latest_filed_wins_smoke(schema_client):
    """Pre-echo of Story 1.6: newer filed_date wins for the same period."""
    client, db = schema_client
    store_schema.create_schema(client)
    client.command(
        f"INSERT INTO canonical_fact ({_COLS}) VALUES "
        "(1,'0000000001-24-000001','us-gaap:Revenues','Revenues','Revenues',"
        "'2023-01-01','2023-03-31','USD',100.0,'10-Q','2023-05-01','a','1',1),"
        "(1,'0000000001-24-000002','us-gaap:Revenues','Revenues','Revenues',"
        "'2023-01-01','2023-03-31','USD',150.0,'10-Q/A','2024-05-01','b','1',1)"
    )
    val = client.query(
        "SELECT revenues FROM screening_mart "
        "WHERE cik = 1 AND period_start = '2023-01-01' AND period_end = '2023-03-31'"
    ).result_rows[0][0]
    assert val == 150.0  # newer filed_date wins


@pytest.mark.integration
def test_absent_concept_is_null_not_zero(schema_client):
    """A concept not reported for a (cik, period) is NULL in the mart, not 0.0."""
    client, db = schema_client
    store_schema.create_schema(client)
    client.command(
        f"INSERT INTO canonical_fact ({_COLS}) VALUES "
        "(7,'0000000007-24-000001','us-gaap:Revenues','Revenues','Revenues',"
        "'2023-01-01','2023-12-31','USD',500.0,'10-K','2024-02-01','h','1',1)"
    )
    row = client.query(
        "SELECT revenues, net_income, assets FROM screening_mart WHERE cik = 7"
    ).result_rows[0]
    assert row[0] == 500.0   # reported
    assert row[1] is None    # not reported -> NULL, not 0.0
    assert row[2] is None


@pytest.mark.integration
def test_equal_filed_date_amendment_tiebreak(schema_client):
    """AD-7: on an equal filed_date, a /A amendment wins (inserted in separate
    ingests to exercise the cross-ingest AggregatingMergeTree state merge)."""
    client, db = schema_client
    store_schema.create_schema(client)
    base = (
        "INSERT INTO canonical_fact (" + _COLS + ") VALUES "
        "(8,'0000000008-24-000001','us-gaap:Revenues','Revenues','Revenues',"
        "'2023-01-01','2023-12-31','USD',100.0,'10-K','2024-02-01','o','1',1)"
    )
    amend = (
        "INSERT INTO canonical_fact (" + _COLS + ") VALUES "
        "(8,'0000000008-24-000002','us-gaap:Revenues','Revenues','Revenues',"
        "'2023-01-01','2023-12-31','USD',175.0,'10-K/A','2024-02-01','a','1',1)"
    )
    client.command(base)
    client.command(amend)  # separate ingest
    val = client.query(
        "SELECT revenues FROM screening_mart WHERE cik = 8"
    ).result_rows[0][0]
    assert val == 175.0  # /A amendment wins on equal filed_date


@pytest.mark.integration
def test_reingest_higher_version_supersedes(schema_client):
    """F1 guard: re-ingesting the SAME identity (same accession/filed_date/form)
    with a higher ingest version supersedes the prior value at the query surface,
    deterministically — including after a background merge (OPTIMIZE FINAL)."""
    client, db = schema_client
    store_schema.create_schema(client)
    ident = (
        "(9,'0000000009-24-000001','us-gaap:Assets','Assets','Assets',"
        "'2023-12-31','2023-12-31','USD',{value},'10-K','2024-02-01','{h}','1',{ver})"
    )
    # original ingest (version 1), then a corrected recovery re-ingest (version 2)
    client.command(f"INSERT INTO canonical_fact ({_COLS}) VALUES " + ident.format(value="200.0", h="v1", ver=1))
    client.command(f"INSERT INTO canonical_fact ({_COLS}) VALUES " + ident.format(value="100.0", h="v2", ver=2))

    def resolved():
        return client.query(
            "SELECT assets FROM screening_mart WHERE cik = 9"
        ).result_rows[0][0]

    assert resolved() == 100.0  # higher version wins pre-merge
    client.command("OPTIMIZE TABLE canonical_fact FINAL")
    assert resolved() == 100.0  # ...and stays correct across a merge


# --- concept-dictionary mart-column builder guards (pure; no ClickHouse) --------


def test_mart_column_empty_list_yields_null_not_syntax_error():
    """A concept with an empty element list must NOT emit broken SQL (which would
    break schema-init for the whole DB) — it resolves to a NULL column."""
    sql = store_schema._mart_column(ConceptDef("foo", "USD", "duration", ()))
    assert sql == "CAST(NULL AS Nullable(Float64)) AS foo"


def test_mart_column_rejects_non_alphanumeric_element():
    """Element names are interpolated into DDL — a non-alphanumeric name (e.g. one
    smuggling a quote) is rejected, so a future sourced dictionary can't inject."""
    with pytest.raises(ValueError):
        store_schema._mart_column(ConceptDef("foo", "USD", "duration", ("Bad'; DROP TABLE x;--",)))


def test_mart_column_rejects_bad_unit():
    with pytest.raises(ValueError):
        store_schema._mart_column(ConceptDef("foo", "US'D", "duration", ("Assets",)))


def test_mart_column_rejects_bad_alias():
    """The column alias is interpolated into DDL too — must be a SQL identifier."""
    with pytest.raises(ValueError):
        store_schema._mart_column(ConceptDef("bad alias; --", "USD", "instant", ("Assets",)))


def test_mart_column_single_element_resolves_via_argmaxif():
    sql = store_schema._mart_column(ConceptDef("assets", "USD", "instant", ("Assets",)))
    assert sql.endswith("AS assets")
    assert "argMaxIf(value," in sql
    assert "canonical_concept IN ('Assets')" in sql and "unit = 'USD'" in sql
    assert "startsWith(form, '10-K')" in sql  # periodic-form restriction
