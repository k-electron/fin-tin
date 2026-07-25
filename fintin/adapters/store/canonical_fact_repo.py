"""Tier 1 writes — insert into ``canonical_fact`` (AD-4, AD-5). The store adapter
owns all ClickHouse access; no DDL here (Story 1.2 owns the schema, AD-18).
Insert-only (AD-6): a re-map with a higher ingest-monotonic ``version`` supersedes
on read (readers use ``FINAL``/``argMax``)."""

from __future__ import annotations

from collections.abc import Collection, Sequence

from fintin.core.canonical import CanonicalFactRow

# canonical_fact columns in schema order — MUST match CanonicalFactRow field order.
CANONICAL_FACT_COLUMNS = [
    "cik",
    "accession",
    "raw_tag",
    "canonical_concept",
    "raw_label",
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


def insert_canonical_facts(client, rows: Sequence[CanonicalFactRow]) -> int:
    """Insert Tier 1 rows into ``canonical_fact``. Returns the number inserted;
    empty input is a no-op. ``client`` is a clickhouse-connect client from
    :func:`fintin.adapters.store.client.get_client`."""
    if not rows:
        return 0
    data = [list(row) for row in rows]  # CanonicalFactRow is a NamedTuple in column order
    client.insert("canonical_fact", data, column_names=CANONICAL_FACT_COLUMNS)
    return len(rows)


def next_canonical_version(client) -> int:
    """The next ingest-monotonic ``version`` for Tier 1 (AD-6): one greater than
    the greatest version currently in ``canonical_fact`` (1 for an empty table).
    Tier 1 has its OWN monotonic sequence, independent of Tier 0's — so a re-map
    always supersedes the prior mapping on read, regardless of the wall clock."""
    rows = client.query("SELECT max(version) FROM canonical_fact").result_rows
    current = rows[0][0] if rows and rows[0][0] is not None else 0
    return int(current) + 1


def mapped_ciks(client, *, ciks: Collection[int]) -> set[int]:
    """Of the given ``ciks``, the subset already present in ``canonical_fact``
    (≥ 1 row) — the Tier 1 counterpart of
    :func:`~fintin.adapters.store.raw_fact_repo.present_ciks`.

    Backfill's resume test intersects the two: a company counts as done only when
    **both** tiers hold rows. Tier 0 alone is not enough, because a company whose
    inline Tier 1 projection failed has raw rows but is not queryable — resuming
    on Tier 0 presence alone would skip it forever. Parameterized (never
    string-interpolated). Empty input → ``set()`` with no query. No ``FINAL``:
    membership is existence."""
    cik_list = [int(c) for c in ciks]
    if not cik_list:
        return set()
    result = client.query(
        "SELECT DISTINCT cik FROM canonical_fact WHERE cik IN %(ciks)s",
        parameters={"ciks": cik_list},
    )
    return {int(row[0]) for row in result.result_rows}
