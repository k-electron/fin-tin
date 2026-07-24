"""ClickHouse schema — the SOLE owner of all DDL (AD-18).

`create_schema(client)` creates the four derivation layers, in order, before any
insert (ClickHouse materialized views do not backfill pre-existing rows):

    raw_fact (Tier 0)  ->  canonical_fact (Tier 1)
                       ->  resolved_fact (+ resolved_fact_mv)  ->  screening_mart

All DDL is idempotent (`IF NOT EXISTS` / `CREATE OR REPLACE VIEW`), so a second
run is a no-op. No other module may issue DDL.

Key invariants:
- AD-5/AD-15: Tier 0 and Tier 1 share the identity key
  (accession, raw_tag, period_start, period_end, unit).
- AD-6: ReplacingMergeTree(version) with an INGEST-MONOTONIC `version` (not
  filed_date), so a recovery re-ingest supersedes a corrupted prior copy.
- AD-7: latest-filed-wins via argMax over the rank tuple
  (filed_date, is_amendment, accession) — deterministic tiebreak (/A first,
  then greatest accession).
- AD-8: resolution MV (AggregatingMergeTree, argMaxState) auto-populated on
  Tier 1 insert; wide screening mart over it.
- AD-17: instant facts period_start = period_end; duration period_start < period_end.
"""

from __future__ import annotations

# Tier 0 — immutable raw landing (AD-6, AD-14, AD-15, AD-17)
RAW_FACT = """
CREATE TABLE IF NOT EXISTS raw_fact (
    cik              UInt32,
    accession        String,
    raw_tag          String,
    raw_label        String,
    taxonomy         LowCardinality(String),
    period_start     Date,
    period_end       Date,
    unit             String,
    value            Float64,
    form             LowCardinality(String),
    filed_date       Date,
    content_hash     String,
    taxonomy_version String,
    version          UInt64
) ENGINE = ReplacingMergeTree(version)
ORDER BY (accession, raw_tag, period_start, period_end, unit)
"""

# Tier 1 — canonical store (AD-5, AD-6, AD-9); canonical_concept is an attribute
CANONICAL_FACT = """
CREATE TABLE IF NOT EXISTS canonical_fact (
    cik               UInt32,
    accession         String,
    raw_tag           String,
    canonical_concept String,
    raw_label         String,
    period_start      Date,
    period_end        Date,
    unit              String,
    value             Float64,
    form              LowCardinality(String),
    filed_date        Date,
    content_hash      String,
    taxonomy_version  String,
    version           UInt64
) ENGINE = ReplacingMergeTree(version)
ORDER BY (accession, raw_tag, period_start, period_end, unit)
"""

# Resolution target table (AD-7, AD-8) — argMax over (filed_date, is_amendment, accession)
RESOLVED_FACT = """
CREATE TABLE IF NOT EXISTS resolved_fact (
    cik               UInt32,
    canonical_concept String,
    unit              String,
    period_start      Date,
    period_end        Date,
    value_state       AggregateFunction(argMax, Float64, Tuple(Date, UInt8, String))
) ENGINE = AggregatingMergeTree
ORDER BY (cik, canonical_concept, unit, period_start, period_end)
"""

# Resolution MV — populated on canonical_fact insert (AD-8)
RESOLVED_FACT_MV = """
CREATE MATERIALIZED VIEW IF NOT EXISTS resolved_fact_mv TO resolved_fact AS
SELECT
    cik,
    canonical_concept,
    unit,
    period_start,
    period_end,
    argMaxState(value, (filed_date, toUInt8(endsWith(form, '/A')), accession)) AS value_state
FROM canonical_fact
GROUP BY cik, canonical_concept, unit, period_start, period_end
"""

# Wide screening mart (AD-8) — one row per (cik, period_start, period_end), concepts as columns.
# Curated starter set of edgartools standardized labels; extend as the mapping (Story 1.5) lands.
SCREENING_MART = """
CREATE OR REPLACE VIEW screening_mart AS
SELECT
    cik,
    period_start,
    period_end,
    argMaxMergeIf(value_state, canonical_concept = 'Revenues')      AS revenues,
    argMaxMergeIf(value_state, canonical_concept = 'NetIncomeLoss') AS net_income,
    argMaxMergeIf(value_state, canonical_concept = 'Assets')        AS assets,
    argMaxMergeIf(value_state, canonical_concept = 'Liabilities')   AS liabilities
FROM resolved_fact
GROUP BY cik, period_start, period_end
"""

# Strict creation order — MV + mart created before any insert (AD-18).
SCHEMA_STATEMENTS: tuple[tuple[str, str], ...] = (
    ("raw_fact", RAW_FACT),
    ("canonical_fact", CANONICAL_FACT),
    ("resolved_fact", RESOLVED_FACT),
    ("resolved_fact_mv", RESOLVED_FACT_MV),
    ("screening_mart", SCREENING_MART),
)


def create_schema(client) -> list[str]:
    """Create all store objects in order, idempotently. Returns the object
    names created/ensured (in order). ``client`` is a clickhouse-connect client
    (from `fintin.adapters.store.client.get_client`); DDL targets the client's
    current database. This is the ONLY place that issues DDL (AD-18)."""
    created: list[str] = []
    for name, ddl in SCHEMA_STATEMENTS:
        client.command(ddl)
        created.append(name)
    return created
