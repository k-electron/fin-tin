"""Tier 0 writes — insert into `raw_fact` (AD-4). The store adapter owns all
ClickHouse access; no DDL here (Story 1.2 owns the schema, AD-18). Insert-only
(AD-6): a re-ingest with a higher ingest-monotonic ``version`` supersedes on read
(readers use ``FINAL``/``argMax``)."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import date

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


def read_raw_facts(client, cik: int) -> list[RawFactRow]:
    """Read one company's Tier 0 rows (deduplicated on read via ``FINAL``, AD-6)
    as :class:`RawFactRow` tuples — the input to the Tier 0 → Tier 1 mapping
    (derivation is one-way, AD-4). Parameterized on ``cik`` (never
    string-interpolated). The SELECT enumerates ``RAW_FACT_COLUMNS`` in
    ``RawFactRow`` field order so ``RawFactRow(*row)`` is positionally correct."""
    cols = ", ".join(RAW_FACT_COLUMNS)
    result = client.query(
        f"SELECT {cols} FROM raw_fact FINAL WHERE cik = %(cik)s",
        parameters={"cik": int(cik)},
    )
    return [RawFactRow(*row) for row in result.result_rows]


def high_water_mark(client) -> date | None:
    """The greatest ``filed_date`` in ``raw_fact`` (``None`` on an empty store).
    A **scan-sizing hint only** (AD-16) — it bounds how far back the reconciler
    scans, never what counts as done. Guarded by ``count()`` because ClickHouse
    ``max()`` on an empty table returns the Date zero (1970-01-01), not NULL."""
    rows = client.query("SELECT count(), max(filed_date) FROM raw_fact").result_rows
    if not rows or rows[0][0] == 0:
        return None
    return rows[0][1]


def present_accessions(client, *, accessions: Collection[str]) -> set[str]:
    """Of the given ``accessions``, the subset already present in ``raw_fact`` — the
    AD-16 membership check, by **exact accession** (the correctness authority). Not
    windowed by any date: the scan window sizes the EDGAR *index* fetch, never the
    store membership (a candidate's index ``filing_date`` and its stored
    ``filed_date`` come from different SEC sources and could disagree). Parameterized
    (never string-interpolated). ``accession`` is ``raw_fact``'s leading sort key, so
    the ``IN`` lookup is an efficient primary-key scan. Empty input → ``set()`` with
    no query. No ``FINAL``: membership is existence, and dedup rows share an accession."""
    acc_list = [str(a) for a in accessions]
    if not acc_list:
        return set()
    result = client.query(
        "SELECT DISTINCT accession FROM raw_fact WHERE accession IN %(accessions)s",
        parameters={"accessions": acc_list},
    )
    return {row[0] for row in result.result_rows}


def present_ciks(client, *, ciks: Collection[int]) -> set[int]:
    """Of the given ``ciks``, the subset already present in ``raw_fact`` (≥ 1 row) —
    the per-company AD-16 membership check that makes backfill resumable without a
    checkpoint (AD-1/AD-11). A per-company ingest is a single atomic insert, so a
    present CIK is fully committed; the backfill engine skips these to avoid a
    re-fetch on restart (SM-C1). Parameterized (never string-interpolated). Empty
    input → ``set()`` with no query. No ``FINAL``: membership is existence."""
    cik_list = [int(c) for c in ciks]
    if not cik_list:
        return set()
    result = client.query(
        "SELECT DISTINCT cik FROM raw_fact WHERE cik IN %(ciks)s",
        parameters={"ciks": cik_list},
    )
    return {int(row[0]) for row in result.result_rows}
