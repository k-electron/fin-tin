---
baseline_commit: 00dac138f08ab5bed8627629fda278be2b965dfa
---

# Story 1.2: Store schema and DDL (single owner, correct creation order)

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the builder,
I want the store adapter to create Tier 0, Tier 1, the resolution MV, and the wide mart in the correct order,
so that ingestion and querying have a correct, mutation-safe schema before any data lands.

## Acceptance Criteria

1. **Given** an empty ClickHouse **When** schema-init runs **Then** `raw_fact` (Tier 0) and `canonical_fact` (Tier 1) are `ReplacingMergeTree` ordered by `(accession, raw_tag, period_start, period_end, unit)` with an **ingest-monotonic** `version` column (AD-5, AD-6, AD-15).
2. **Given** the base tables exist **When** schema-init runs **Then** the resolution MV (`AggregatingMergeTree`, `argMaxState`) and the wide mart are created **before any insert** (AD-18) **And** only the store adapter issues DDL.
3. **Given** schema-init is run twice **Then** it is idempotent (no error, no duplicate objects).
4. **Given** instant vs. duration facts **Then** the schema represents them per AD-17 (instant: `period_start = period_end`; duration: `period_start < period_end`).

## Tasks / Subtasks

- [x] **Task 1 — Store-adapter DDL module (sole owner, AD-18)** (AC: 1, 2)
  - [x] New `fintin/adapters/store/schema.py` — the **only** place that issues ClickHouse DDL. Expose `create_schema(client) -> None` that runs the statements in strict order: (1) `raw_fact`, (2) `canonical_fact`, (3) `resolved_fact` target table, (4) `resolved_fact_mv` materialized view, (5) `screening_mart` view. No other module may issue DDL.
  - [x] Keep each DDL statement as a named constant/string so tests and future migrations can reference them.
- [x] **Task 2 — Tier 0 `raw_fact` + Tier 1 `canonical_fact`** (AC: 1, 4)
  - [x] Both `ENGINE = ReplacingMergeTree(version)`, `ORDER BY (accession, raw_tag, period_start, period_end, unit)` (AD-5/AD-15 identity key).
  - [x] `version UInt64` is **ingest-monotonic** (an ingest sequence/timestamp), **not** `filed_date` (AD-6) — so a recovery re-ingest supersedes a corrupted prior copy regardless of filing dates.
  - [x] Provenance columns per AD-14: `raw_tag`, `raw_label`, `filed_date`, `content_hash`, `taxonomy_version` (+ `form`, needed for the AD-7 tiebreak). `period_start`/`period_end` are `Date` (AD-17). Types per the conventions table below.
  - [x] Tier 1 adds `canonical_concept` as an **attribute** (not in the key, AD-5).
- [x] **Task 3 — Resolution MV `resolved_fact` (latest-filed-wins, AD-7/AD-8)** (AC: 2)
  - [x] Target table `resolved_fact` = `AggregatingMergeTree`, `ORDER BY (cik, canonical_concept, unit, period_start, period_end)`, storing `argMaxState(value, rank)`.
  - [x] The **rank** encodes the AD-7 deterministic tiebreak: a tuple `(filed_date, is_amendment, accession)` where `is_amendment = toUInt8(endsWith(form, '/A'))` — so on an equal `filed_date` a `/A` amendment wins, then the greatest `accession`.
  - [x] MV `resolved_fact_mv TO resolved_fact` reads `canonical_fact` inserts, grouping on `(cik, canonical_concept, unit, period_start, period_end)`. Read the resolved value via `argMaxMerge(value_state)`.
- [x] **Task 4 — Wide Screening Mart `screening_mart` (AD-8)** (AC: 2)
  - [x] One row per `(cik, period_start, period_end)` with **canonical concepts as columns** (wide, not long). Recommended v1 shape: a `VIEW` over `resolved_fact` using `argMaxMergeIf(value_state, canonical_concept = '<label>')` per curated standardized concept. Derived-metric columns are deferred (AD-8).
  - [x] ⚠️ **Design decision to confirm:** the exact concept **column list** must use edgartools' standardized labels *verbatim* (convention). The label strings aren't known until the edgartools mapping is wired (Story 1.5). Start with a small curated set of common statement lines (see Dev Notes) and mark it extensible; the screening test in Story 1.6 will exercise it. Flag this to the reviewer.
- [x] **Task 5 — `schema-init` CLI command (thin trigger, AD-2)** (AC: 1, 2, 3)
  - [x] Add `fintin schema-init` to `fintin/cli/app.py` — loads config, opens a client via the existing `get_client`, calls `store.schema.create_schema(client)`, prints a clear success line, closes the client. Do **not** name it `status` (reserved, FR-14). CLI stays a dumb trigger (no DDL logic in the CLI).
  - [x] Idempotency: use `CREATE TABLE IF NOT EXISTS` / `CREATE MATERIALIZED VIEW IF NOT EXISTS` / `CREATE VIEW IF NOT EXISTS` (or `CREATE OR REPLACE VIEW` for the mart) so a second run is a no-op with no error and no duplicate objects (AC-3).
- [x] **Task 6 — Tests** (AC: 1, 2, 3, 4)
  - [x] `tests/test_schema.py`, `@pytest.mark.integration` (auto-skips when the container isn't listening — the Story 1.1 `conftest.py` gate already handles this; reuse the `local_clickhouse_config` fixture).
  - [x] **Isolation:** create the schema in a **throwaway database** (unique name, e.g. `fintin_test_<uuid>`) so tests never touch the real `default` DB; drop it in `finally`. (Extend `get_client`/`create_schema` to accept a target DB, or create the DB and point a client at it.)
  - [x] Assert engines + sorting keys + the ingest-monotonic version column by querying `system.tables` / `system.columns` (e.g. `engine`, `sorting_key`) for `raw_fact` and `canonical_fact` (AC-1); assert `resolved_fact` is `AggregatingMergeTree` and `resolved_fact_mv` / `screening_mart` exist (AC-2).
  - [x] Idempotency: run `create_schema` twice → no error, object counts unchanged (AC-3).
  - [x] AD-17 representation (AC-4): insert two synthetic `canonical_fact` rows directly — an **instant** (`period_start = period_end`) and a **duration** (`period_start < period_end`) — and confirm both store and read back distinctly, and that `resolved_fact` resolves each. (This is a schema-level check using hand-written rows; real ingestion is Story 1.4.)
  - [x] A latest-filed-wins smoke check (optional but valuable, pre-echoes Story 1.6): insert two `canonical_fact` rows for one `(cik, concept, unit, period)` with different `filed_date` → `argMaxMerge` returns the newer value.

### Review Findings

_Adversarial code review (2026-07-23) — 3 parallel layers, all verified live against CH 26.3. Triage: 0 decision-needed, 7 patch, 1 deferred, 2 dismissed. AC-1..AC-4 all satisfied; no scope-creep or architecture violations. One HIGH correctness defect (F1) in the resolution layer._

- [x] [Review][Patch] (high) Resolution rank tuple omits `version` — a recovery re-ingest (same accession/filed_date/form, higher `version`, corrected `value`) ties on the rank and `argMax` picks arbitrarily, so `resolved_fact`/`screening_mart` can serve the STALE value and flip across background merges. Breaks AD-6 supersession at the query surface (threatens SM-1). Fix: append `version` as the **least-significant** rank element — `argMaxState(value, (filed_date, toUInt8(endsWith(form,'/A')), accession, version))` — and widen the state type to `AggregateFunction(argMax, Float64, Tuple(Date, UInt8, String, UInt64))`. [fintin/adapters/store/schema.py: RESOLVED_FACT / RESOLVED_FACT_MV]
- [x] [Review][Patch] (medium) Wide mart returns `0.0` (Float64 default), not `NULL`, for a concept absent in a `(cik, period)` group — "not reported" is indistinguishable from a real zero (breaks `> 0` screens / ratios). Fix: emit `Nullable` per column, e.g. `if(countIf(canonical_concept='X') > 0, argMaxMergeIf(value_state, canonical_concept='X'), NULL)`. [schema.py: SCREENING_MART]
- [x] [Review][Patch] (medium) Wide mart omits `unit` from GROUP BY while `resolved_fact` keys on it — a concept reported in >1 unit collapses to an arbitrary one (nondeterministic across merges). Low real risk in v1 (us-gaap USD-only) but cheap to harden: pin monetary concept columns to `unit = 'USD'`. [schema.py: SCREENING_MART]
- [x] [Review][Patch] (medium) `schema-init` has a dead `except StoreConnectionError` branch — `get_client` doesn't wrap driver errors, so a down server / missing DB is caught by the generic handler and mislabeled "Schema init failed" (should read "Connection failed"). Fix: `check_connection(cfg.clickhouse)` first, then create_schema; keep the generic branch only for real DDL errors. [fintin/cli/app.py: schema_init_command]
- [x] [Review][Patch] (low) Test fixture `schema_client` acquires the client (and runs `CREATE DATABASE`) outside the `try/finally`, so a failure there leaks an orphan `fintin_test_*` DB; a raising `client.close()` also skips the DROP. Fix: move creation/acquisition inside `try`, suppress close errors so DROP always runs. [tests/test_schema.py]
- [x] [Review][Patch] (low) Test gaps: no equal-`filed_date` tiebreak test (the spec-critical AD-7 rule), no re-ingest/`version`-supersession test (would guard F1), the amendment is inserted in one batch (never exercises the cross-ingest `AggregatingMergeTree` state merge), and no CLI-level `schema-init` test. Add these; tighten the `engine_full` assertion. [tests/test_schema.py, tests/test_cli.py]
- [x] [Review][Patch] (low) `get_client(database="")` passes the empty string through (overrides to the server default) instead of falling back to `cfg.database`. Fix: guard with `if database` (truthy) or reject blank. [fintin/adapters/store/client.py: get_client]
- [x] [Review][Defer] (medium) No schema-migration story — `CREATE … IF NOT EXISTS` silently keeps a stale table/MV if its DDL later changes (the F1 fix won't apply to an already-created deployment without a manual drop). Deferred: migrations are future work for a still-stabilizing v1 schema; a "create-only, DDL changes need manual drop/recreate" note is added now. [schema.py]

_Dismissed (2): direct non-FINAL reads of `raw_fact`/`canonical_fact` return duplicates — by-design per AD-6 (readers use FINAL/argMax; the resolution layer is the intended read path); `create_schema` returns `list[str]` vs the spec's `-> None` — benign and used by the CLI._

## Dev Notes

### What this story IS
The **ClickHouse schema** — the data-model spine. It creates the four derivation layers (Tier 0 → Tier 1 → Resolution MV → wide mart) via the single DDL-owning store adapter, in the correct order, idempotently, exposed through a `fintin schema-init` command. This is the substrate Stories 1.4 (ingest), 1.5 (map), and 1.6 (resolve/screen) build on.

### What this story is NOT (scope fences — do not implement)
- ❌ **No ingestion / no EDGAR / no `edgartools`.** Story 1.4 lands Tier 0 facts; Story 1.3 is the EDGAR client. Do not add `edgartools` to deps here. Any rows in tests are hand-written synthetic rows.
- ❌ **No mapping logic.** The Tier 0→Tier 1 standardization is Story 1.5; here `canonical_fact` just has the `canonical_concept` column.
- ❌ **No backfill / catch-up / reconciler / lease.** Epics 2–3.
- ❌ **No real screening queries.** Story 1.6 exercises the mart; here we only create it.

### Builds directly on Story 1.1 (previous-story intelligence)
- **Store adapter exists:** `fintin/adapters/store/client.py` has `get_client(cfg, *, connect_timeout=None)` and `check_connection`. **Reuse `get_client`** — do not create a second client path (AD-18: store adapter is the sole ClickHouse owner). Add `schema.py` alongside `client.py`.
- **ClickHouse 26.3 requires a password** for the `default` user — `fintin.toml` has `password = "fintin_local"` and `get_client` already handles it. (Verified live at `26.3.17.56` in Story 1.1.)
- **clickhouse-connect usage:** `client.command("DDL…")` for DDL/DML; `client.query("SELECT …").result_rows` for reads. Always `client.close()` in a `finally` (Story 1.1 review finding).
- **Integration tests auto-skip** when ClickHouse isn't listening — `tests/conftest.py` already does a timed TCP probe and only gates `@pytest.mark.integration` items; the `local_clickhouse_config` fixture reads `fintin.toml`. Reuse both. Use **unique names** (DB/table) to stay parallel-safe (Story 1.1 review finding).
- **Runtime:** `uv` selected CPython 3.14.6 (fine, ≥3.12); run everything via `uv run …`.

### Architecture decisions this story must obey
| AD | Rule as it applies here |
| --- | --- |
| AD-18 | One component (`adapters/store`) owns **all** DDL; MV + mart created **before any insert**. Since this story does no ingest, "before insert" is satisfied by construction — but keep the strict creation order in `create_schema`. |
| AD-6 | Tier 0/Tier 1 = `ReplacingMergeTree(version)`; `version` is **ingest-monotonic**, NOT `filed_date`; all writes are inserts; readers use `FINAL`/`argMax`, never assume a merge ran. |
| AD-5 / AD-15 | Identity key `(accession, raw_tag, period_start, period_end, unit)` for both tiers; `canonical_concept`/`taxonomy_version`/`filed_date`/`content_hash` are attributes. Consolidated facts only (no dimensional axis members — enforced at ingest, Story 1.4). |
| AD-7 | Resolution = `argMax(value, (filed_date, is_amendment, accession))`; group on **actual period dates**, never `fy`/`fp`. Deterministic tiebreak: `/A` first, then greatest `accession`. |
| AD-8 | Resolution MV (`AggregatingMergeTree`, `argMaxState`) auto-populated on Tier 1 insert; wide mart over it (one row per `(cik, period_start, period_end)`, concepts as columns). **Caveat:** a re-map or a Tier 0 recovery re-ingest requires a **mart rebuild** (the MV can't retract a superseded contribution) — out of scope here, but don't design anything that assumes in-place MV correction. |
| AD-9 | `canonical_concept` values will be edgartools standardized labels verbatim; `taxonomy_version` = edgartools version string. |
| AD-17 | Instant facts `period_start = period_end`; duration `period_start < period_end`. Uniform across all tiers + mart. |

### Consistency conventions (types) — [Source: ARCHITECTURE-SPINE.md#Consistency-Conventions]
- `cik` = `UInt32`; `accession` = `String` (dashed 20-char canonical `0000320193-24-000123`).
- `value` = `Float64` (screening-adequate; Decimal deferred). `period_start`/`period_end`/`filed_date` = `Date`. `unit` = `String` (`USD`, `shares`, `USD/shares`). `taxonomy_version` = `String`. Table/column names `snake_case`.
- Canonical table names: `raw_fact` (Tier 0), `canonical_fact` (Tier 1), `resolved_fact` (MV target), `screening_mart` (wide view).

### Reference DDL (implement + validate against the live CH 26.3 container; not gospel)
```sql
-- (1) Tier 0
CREATE TABLE IF NOT EXISTS raw_fact (
    cik              UInt32,
    accession        String,
    raw_tag          String,
    raw_label        String,
    taxonomy         LowCardinality(String),      -- us-gaap | dei | srt
    period_start     Date,                         -- AD-17
    period_end       Date,
    unit             String,
    value            Float64,
    form             LowCardinality(String),       -- 10-K, 10-Q, 10-K/A … (AD-7 tiebreak)
    filed_date       Date,
    content_hash     String,                       -- sha256, AD-14
    taxonomy_version String,
    version          UInt64                        -- INGEST-MONOTONIC (AD-6), not filed_date
) ENGINE = ReplacingMergeTree(version)
ORDER BY (accession, raw_tag, period_start, period_end, unit);

-- (2) Tier 1 (same key shape; canonical_concept is an attribute)
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
ORDER BY (accession, raw_tag, period_start, period_end, unit);

-- (3) Resolution target (AD-7/AD-8)
CREATE TABLE IF NOT EXISTS resolved_fact (
    cik               UInt32,
    canonical_concept String,
    unit              String,
    period_start      Date,
    period_end        Date,
    value_state       AggregateFunction(argMax, Float64, Tuple(Date, UInt8, String))
) ENGINE = AggregatingMergeTree
ORDER BY (cik, canonical_concept, unit, period_start, period_end);

-- (4) Resolution MV — populated on canonical_fact insert
CREATE MATERIALIZED VIEW IF NOT EXISTS resolved_fact_mv TO resolved_fact AS
SELECT cik, canonical_concept, unit, period_start, period_end,
       argMaxState(value, (filed_date, toUInt8(endsWith(form, '/A')), accession)) AS value_state
FROM canonical_fact
GROUP BY cik, canonical_concept, unit, period_start, period_end;

-- (5) Wide mart — curated concept columns (extensible; confirm labels w/ edgartools in 1.5)
CREATE OR REPLACE VIEW screening_mart AS
SELECT cik, period_start, period_end,
       argMaxMergeIf(value_state, canonical_concept = 'Revenues')      AS revenues,
       argMaxMergeIf(value_state, canonical_concept = 'NetIncomeLoss') AS net_income,
       argMaxMergeIf(value_state, canonical_concept = 'Assets')        AS assets,
       argMaxMergeIf(value_state, canonical_concept = 'Liabilities')   AS liabilities
FROM resolved_fact
GROUP BY cik, period_start, period_end;
```
Notes: verify `AggregateFunction(argMax, Float64, Tuple(Date, UInt8, String))` and `argMaxMergeIf` against the running container before finalizing (run the DDL, then `SELECT` from `system.tables`/`system.columns`). The `screening_mart` concept list is a **placeholder curated set** — the real standardized labels come from edgartools (Story 1.5); keep the set small and clearly extensible.

### Files to touch
```text
fintin/adapters/store/schema.py   # NEW — DDL constants + create_schema(client); sole DDL owner
fintin/adapters/store/client.py   # (maybe) UPDATE — allow targeting a database for test isolation
fintin/cli/app.py                 # UPDATE — add `schema-init` command (thin trigger)
tests/test_schema.py              # NEW — integration tests (engines, order-by, idempotency, AD-17)
```

### Testing standards
- `pytest` via `uv run pytest`; container-dependent tests `@pytest.mark.integration` (auto-skip when down). Default suite stays green without Docker.
- Introspect the schema via ClickHouse system tables (`system.tables.engine`, `system.tables.sorting_key`, `system.columns`) rather than parsing DDL strings.
- Never touch the real `default` DB — create/drop a unique throwaway database per test run.
- No live EDGAR (not relevant here — no network).

### Project Structure Notes
- `schema.py` lives in `adapters/store/` beside `client.py` — matches the architecture source tree and AD-18 ownership. No variances.
- `schema-init` command name is a suggestion; must not be `status` (reserved for FR-14 / Story 2.4).

### References
- [Source: _bmad-output/planning-artifacts/epics.md#Story-1.2] — user story + ACs
- [Source: .../ARCHITECTURE-SPINE.md#AD-5] [#AD-6] [#AD-7] [#AD-8] [#AD-9] [#AD-15] [#AD-17] [#AD-18] — governing decisions
- [Source: .../ARCHITECTURE-SPINE.md#Consistency-Conventions] — types, naming, state/mutation
- [Source: _bmad-output/planning-artifacts/architecture/.../BUILD-SPLIT.md#Epic-A] — schema deliverables + "MV before backfill" watch
- [Source: _bmad-output/implementation-artifacts/1-1-runnable-skeleton.md] — store adapter, CH password reality, test-gating pattern

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Claude Opus 4.8, 1M context) — bmad-dev-story workflow.

### Debug Log References

- All DDL validated against a live `clickhouse/clickhouse-server:26.3` container (`26.3.17.56`).
- Full suite: **20 passed** (15 prior + 5 new schema integration tests). Integration auto-skips without Docker.
- `system.tables` after `schema-init`: `raw_fact`/`canonical_fact` = `ReplacingMergeTree`, `resolved_fact` = `AggregatingMergeTree`, `resolved_fact_mv` = `MaterializedView`, `screening_mart` = `View`.

### Completion Notes List

- Ultimate context engine analysis completed — comprehensive developer guide created.
- **All 4 ACs verified against live CH 26.3:** AC-1 (Tier 0/1 `ReplacingMergeTree(version)` on the identity key `(accession, raw_tag, period_start, period_end, unit)`, `version UInt64` ingest-monotonic); AC-2 (resolution MV `AggregatingMergeTree` + `argMaxState`, wide mart created, all DDL in `adapters/store/schema.py` only — AD-18); AC-3 (idempotent — `create_schema` twice keeps 5 objects; `schema-init` re-run exit 0); AC-4 (instant `period_start == period_end` vs duration `start < end`, both flow through the MV into the mart).
- **Sole DDL owner (AD-18):** `fintin/adapters/store/schema.py` holds all DDL as named constants + `create_schema(client)` running the 5 statements in strict order (Tier 0 → Tier 1 → resolved_fact → MV → mart). Idempotent via `IF NOT EXISTS` / `CREATE OR REPLACE VIEW`.
- **AD-7 tiebreak encoded** as `argMax(value, (filed_date, toUInt8(endsWith(form,'/A')), accession))` — verified by `test_latest_filed_wins_smoke` (newer filed wins).
- **`get_client` gained a `database` override** (keyword-only) so tests build the schema in a unique throwaway DB (`fintin_test_<uuid>`) and never touch `default`.
- **`fintin schema-init`** added as a thin CLI trigger (loads config → `get_client` → `create_schema` → closes client). Not named `status` (reserved).
- ⚠️ **Open design item for review (flagged in spec):** the wide mart's concept columns are a **curated starter set** (`revenues`, `net_income`, `assets`, `liabilities`) using assumed edgartools standardized labels. The *exact* label strings must be confirmed once the standardization mapping lands (Story 1.5); the column set is extensible. `screening_mart` returns `0.0` (Float64 default) for a concept absent in a `(cik, period)` group — acceptable for v1, revisit if NULL semantics are wanted.
- **Ran `fintin schema-init` against the dev container's real `default` DB** — the actual v1 schema now exists there (idempotent), ready for Story 1.4 ingestion.
- No new dependencies.

### Change Log

- 2026-07-23 — Story 1.2 implemented: `adapters/store/schema.py` (sole DDL owner) creating Tier 0 `raw_fact`, Tier 1 `canonical_fact` (`ReplacingMergeTree`, ingest-monotonic `version`, identity key), resolution `resolved_fact` (`AggregatingMergeTree`, `argMaxState` with AD-7 tiebreak) + `resolved_fact_mv`, and the wide `screening_mart` view — created in order, idempotently. Added `fintin schema-init` CLI command and a `database` override on `get_client` for test isolation. 5 integration tests; 20 passed. Status → review.

### File List

**New:**
- `fintin/adapters/store/schema.py`
- `tests/test_schema.py`
- `_bmad-output/implementation-artifacts/deferred-work.md` (review — deferred migration item)

**Modified:**
- `fintin/adapters/store/client.py` (added `database` override to `get_client`; review: blank-override guard)
- `fintin/cli/app.py` (added `schema-init` command; review: connection-error handling)
- `tests/test_cli.py` (review: `schema-init` CLI tests)

### Change Log

- 2026-07-23 — Story 1.2 implemented: schema DDL, `schema-init` CLI, `get_client` database override. 20 tests.
- 2026-07-23 — Code review (adversarial, 3 layers): 7 patch findings applied, 1 deferred (schema migrations → `deferred-work.md`), 2 dismissed. **F1 (HIGH):** appended `version` (least-significant) to the resolution rank tuple + widened the state to `Tuple(Date, UInt8, String, UInt64)` so a recovery re-ingest supersedes at the query surface (guarded by a new supersession test). Mart now returns NULL (not 0.0) for absent concepts and pins monetary columns to `unit='USD'`. `schema-init` reports connection failures via `check_connection`. Leak-safe test fixture; added tiebreak / version / CLI tests; `get_client("")` falls back to config. Reconciled the dev container's `default` schema (manual drop/recreate — the deferred migration gap). Suite: 25 passed. Status → done.
