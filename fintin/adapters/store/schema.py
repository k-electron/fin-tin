"""ClickHouse schema — the SOLE owner of all DDL (AD-18).

`create_schema(client)` creates the four derivation layers, in order, before any
insert (ClickHouse materialized views do not backfill pre-existing rows):

    raw_fact (Tier 0)  ->  canonical_fact (Tier 1)  ->  screening_mart (wide view)
                                                    ->  screening_wide (companion view)

All DDL is idempotent (`IF NOT EXISTS` / `CREATE OR REPLACE VIEW`), so a second
run is a no-op. No other module may issue DDL.

NOTE: schema-init is **create-only** (no migrations in v1). `IF NOT EXISTS`
keeps an existing object as-is, so changing a table/MV definition requires a
manual drop/recreate (or `docker compose down -v`) — see
`_bmad-output/implementation-artifacts/deferred-work.md`.

Key invariants:
- AD-5/AD-15: Tier 0 and Tier 1 share the identity key
  (accession, raw_tag, period_start, period_end, unit).
- AD-6: ReplacingMergeTree(version) with an INGEST-MONOTONIC `version` (not
  filed_date), so a recovery re-ingest supersedes a corrupted prior copy — the
  resolution rank carries `version` as its least-significant term so this
  supersession also holds at the query surface.
- AD-7: latest-filed-wins via argMax over the rank tuple
  (filed_date, is_amendment, accession, version) — deterministic tiebreak
  (/A first, then greatest accession, then latest ingest).
- AD-8 (Approach B): the wide screening mart resolves the concept dictionary
  (concept_dictionary.py) — latest-filed across a concept's element union, element
  position as the tiebreak — as a VIEW derived on read over `canonical_fact FINAL`
  (no materialized concept copy, so it can't drift; AD-1). Story 1.2's
  element-grained resolution MV (resolved_fact + resolved_fact_mv) was superseded
  by this and has been DROPPED: nothing read it, yet it fired on every Tier 1
  insert and stood as a second materialized copy to keep consistent (AD-1).
- AD-17: instant facts period_start = period_end; duration period_start < period_end.
- Dates are `Date32` (1900-01-01 … 2299-12-31), not `Date` (1970 … 2149). Real
  filings use sentinel dates outside the narrower window — Oracle stamps an
  open-ended restructuring cost 1900-01-01 → 2199-12-31 — and because a company
  commits as one atomic insert, a single unstorable date failed that company
  entirely. `core.ingest` also drops (and counts) anything outside even these
  bounds, so the widening and the guard together cap the blast radius at one fact.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from fintin.adapters.store.concept_dictionary import CONCEPT_DICTIONARY, ConceptDef

# Tier 0 — immutable raw landing (AD-6, AD-14, AD-15, AD-17)
RAW_FACT = """
CREATE TABLE IF NOT EXISTS raw_fact (
    cik              UInt32,
    accession        String,
    raw_tag          String,
    raw_label        String,
    taxonomy         LowCardinality(String),
    period_start     Date32,
    period_end       Date32,
    unit             String,
    value            Float64,
    form             LowCardinality(String),
    filed_date       Date32,
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
    period_start      Date32,
    period_end        Date32,
    unit              String,
    value             Float64,
    form              LowCardinality(String),
    filed_date        Date32,
    content_hash      String,
    taxonomy_version  String,
    version           UInt64
) ENGINE = ReplacingMergeTree(version)
ORDER BY (accession, raw_tag, period_start, period_end, unit)
"""

# Wide screening mart (AD-8, Approach B) — one row per (cik, period_start, period_end).
#
# The versioned CONCEPT_DICTIONARY (concept_dictionary.py, AD-9) maps each screening
# column to an ORDERED list of standard elements. The mart resolves each column to
# the LATEST-FILED value across that concept's element union, breaking ties by the
# AD-7 filing rank then ELEMENT LIST-POSITION — computed as a single `argMaxIf`
# directly over `canonical_fact FINAL`. It is derived on read (no materialized
# concept-level copy), so it always reflects the current dictionary and never drifts
# (AD-1); a dictionary edit is a `CREATE OR REPLACE VIEW`, not a data rebuild.
# `canonical_concept` is the element verbatim (AD-9), so the element lists compare
# directly against Tier 1. A column is NULL when none of its elements is present for
# the (cik, period) — distinct from a real 0.0. unit is pinned per concept.

# Names interpolated into DDL are validated so the dictionary can grow without a
# DDL-injection surface. Element local names are XBRL NCNames (alphanumeric); units
# may include '/' (e.g. 'USD/shares'); aliases are SQL identifiers.
_ELEMENT_NAME_RE = re.compile(r"^[A-Za-z0-9]+$")
_UNIT_RE = re.compile(r"^[A-Za-z0-9/]+$")
_ALIAS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Authoritative periodic reports only — a later-filed 8-K (or other non-periodic
# form) must not override the audited 10-K/10-Q on recency (10-K, 10-Q and their /A).
_PERIODIC_FORMS = "(startsWith(form, '10-K') OR startsWith(form, '10-Q'))"


def _mart_column(concept: ConceptDef) -> str:
    """Resolve one screening concept as a wide-mart column (Approach B): the
    latest-filed value across the concept's element union (periodic forms only),
    with the AD-7 filing rank then element list-position then `raw_tag` as
    deterministic tiebreaks. NULL (not 0.0) when no element is present."""
    if not _ALIAS_RE.match(concept.alias):
        raise ValueError(f"Invalid concept alias {concept.alias!r}: must be a SQL identifier.")
    if not _UNIT_RE.match(concept.unit):
        raise ValueError(f"Invalid concept unit {concept.unit!r}.")
    if not concept.elements:
        # An empty list resolves to NULL (guards a future typo'd/empty dictionary entry).
        return f"CAST(NULL AS Nullable(Float64)) AS {concept.alias}"
    for el in concept.elements:
        if not _ELEMENT_NAME_RE.match(el):
            raise ValueError(f"Invalid concept-dictionary element {el!r}: must be alphanumeric.")
    in_list = ", ".join(f"'{el}'" for el in concept.elements)
    cond = f"canonical_concept IN ({in_list}) AND unit = '{concept.unit}' AND {_PERIODIC_FORMS}"
    # Element position priority: earlier in the list wins a same-filing rank tie.
    n = len(concept.elements)
    pos = ", ".join(f"canonical_concept = '{el}', {n - i}" for i, el in enumerate(concept.elements))
    # Rank: latest-filed (AD-7: filed_date, /A, greatest accession, ingest version),
    # then element position, then raw_tag — fully deterministic (no cross-namespace tie).
    rank = f"(filed_date, toUInt8(endsWith(form, '/A')), accession, version, multiIf({pos}, 0), raw_tag)"
    # if()-guard keeps "absent" distinct from a real 0.0 (argMaxIf returns 0 on no match).
    return f"if(countIf({cond}) > 0, argMaxIf(value, {rank}, {cond}), NULL) AS {concept.alias}"


def _build_screening_mart() -> str:
    aliases = [c.alias for c in CONCEPT_DICTIONARY]
    dupes = sorted({a for a in aliases if aliases.count(a) > 1})
    if dupes:
        raise ValueError(f"Duplicate concept aliases in the dictionary: {dupes}")
    cols = ",\n    ".join(_mart_column(c) for c in CONCEPT_DICTIONARY)
    return (
        "CREATE OR REPLACE VIEW screening_mart AS\n"
        "SELECT\n    cik,\n    period_start,\n    period_end,\n    "
        f"{cols}\n"
        "FROM canonical_fact FINAL\n"
        "GROUP BY cik, period_start, period_end"
    )


SCREENING_MART = _build_screening_mart()


# Cross-statement screening surface (AD-8) — one row per income (duration) period
# with the balance-sheet instant AS OF period_end joined on. The base screening_mart
# keys on (period_start, period_end), so flows (income, durations) and stocks
# (balance sheet, instants) land in SEPARATE rows; this companion collapses them so a
# single screen can mix them (ROA, leverage, per-share). Duration concepts come from
# the income row; instant concepts from the balance-sheet row whose instant date ==
# the income period_end.
def _build_screening_wide() -> str:
    dur = [c.alias for c in CONCEPT_DICTIONARY if c.period_type == "duration"]
    inst = [c.alias for c in CONCEPT_DICTIONARY if c.period_type == "instant"]
    dur_cols = ",\n    ".join(f"d.{a} AS {a}" for a in dur)
    inst_cols = ",\n    ".join(f"b.{a} AS {a}" for a in inst)
    return (
        "CREATE OR REPLACE VIEW screening_wide AS\n"
        "SELECT\n    d.cik AS cik,\n    d.period_start AS period_start,\n    d.period_end AS period_end,\n    "
        f"{dur_cols},\n    {inst_cols}\n"
        "FROM screening_mart AS d\n"
        "LEFT JOIN screening_mart AS b\n"
        "  ON b.cik = d.cik AND b.period_start = d.period_end AND b.period_end = d.period_end\n"
        "WHERE d.period_start < d.period_end"
    )


SCREENING_WIDE = _build_screening_wide()

class SchemaObject(NamedTuple):
    """One store object: its name, what kind of thing it is (so it can be dropped
    with the right statement), and the DDL that creates it."""

    name: str
    kind: str  # "TABLE" | "VIEW"
    ddl: str


# Strict creation order — every derived surface created before any insert (AD-18).
# `drop_schema` walks this in reverse, so dependents go before their sources.
SCHEMA_STATEMENTS: tuple[SchemaObject, ...] = (
    SchemaObject("raw_fact", "TABLE", RAW_FACT),
    SchemaObject("canonical_fact", "TABLE", CANONICAL_FACT),
    SchemaObject("screening_mart", "VIEW", SCREENING_MART),
    SchemaObject("screening_wide", "VIEW", SCREENING_WIDE),
)


def create_schema(client) -> list[str]:
    """Create all store objects in order, idempotently. Returns the object
    names created/ensured (in order). ``client`` is a clickhouse-connect client
    (from `fintin.adapters.store.client.get_client`); DDL targets the client's
    current database. This is the ONLY place that issues DDL (AD-18)."""
    created: list[str] = []
    for obj in SCHEMA_STATEMENTS:
        client.command(obj.ddl)
        created.append(obj.name)
    return created


def drop_schema(client) -> list[str]:
    """Drop every fin-tin object, in reverse creation order. Returns the names
    dropped (in drop order). Idempotent (``IF EXISTS``) — dropping an already-empty
    database is a no-op, not an error.

    **Destroys the whole corpus.** Scoped deliberately: it drops only the objects
    fin-tin declares above, in the client's current database, so anything else
    living in that database is untouched (unlike `docker compose down -v`, which
    discards the entire volume). Re-derivable by definition — every row comes from
    EDGAR — which is why a wipe-and-repopulate is a supported workflow rather than
    a data-loss event. Like `create_schema`, this is DDL and therefore lives here
    (AD-18)."""
    dropped: list[str] = []
    for obj in reversed(SCHEMA_STATEMENTS):
        client.command(f"DROP {obj.kind} IF EXISTS {obj.name}")
        dropped.append(obj.name)
    return dropped
