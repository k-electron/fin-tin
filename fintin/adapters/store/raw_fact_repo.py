"""Tier 0 writes — insert into `raw_fact` (AD-4). The store adapter owns all
ClickHouse access; no DDL here (Story 1.2 owns the schema, AD-18). Insert-only
(AD-6): a re-ingest with a higher ingest-monotonic ``version`` supersedes on read
(readers use ``FINAL``/``argMax``)."""

from __future__ import annotations

from collections.abc import Sequence

from fintin.core.ingest import RawFactRow

# raw_fact columns in schema order — MUST match RawFactRow's field order.
RAW_FACT_COLUMNS = [
    "cik",
    "accession",
    "raw_tag",
    "raw_label",
    "taxonomy",
    "period_start",
    "period_end",
    "unit",
    "value",
    "form",
    "filed_date",
    "content_hash",
    "taxonomy_version",
    "version",
]


def insert_raw_facts(client, rows: Sequence[RawFactRow]) -> int:
    """Insert Tier 0 rows into ``raw_fact``. Returns the number of rows inserted.
    Empty input is a no-op. ``client`` is a clickhouse-connect client from
    :func:`fintin.adapters.store.client.get_client`."""
    if not rows:
        return 0
    data = [list(row) for row in rows]  # RawFactRow is a NamedTuple in column order
    client.insert("raw_fact", data, column_names=RAW_FACT_COLUMNS)
    return len(rows)


def next_ingest_version(client) -> int:
    """The next ingest-monotonic ``version`` (AD-6): one greater than the greatest
    version currently in ``raw_fact`` (1 for an empty table). Sourcing the version
    from the store — not a wall clock — guarantees a re-ingest always supersedes a
    corrupted prior copy on read, regardless of clock changes."""
    rows = client.query("SELECT max(version) FROM raw_fact").result_rows
    current = rows[0][0] if rows and rows[0][0] is not None else 0
    return int(current) + 1
