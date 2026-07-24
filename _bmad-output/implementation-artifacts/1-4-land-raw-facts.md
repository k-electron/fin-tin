---
baseline_commit: 898ab97abc9f75c1375658b05eaee89df8f2ef45
---

# Story 1.4: Land one company's raw facts in Tier 0

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the builder,
I want to ingest a single company's standard-taxonomy facts into Tier 0 with full provenance,
so that the raw local mirror exists for that company.

## Acceptance Criteria

1. **Given** a CIK **When** I ingest it **Then** its `us-gaap`/`dei`/`srt` **numerical** facts land in `raw_fact` keyed by raw-fact identity `(accession, raw_tag, period_start, period_end, unit)` with provenance — `raw_tag`, `raw_label`, `taxonomy`, `unit`, period, `filed_date`, `content_hash` (= sha256 of the normalized fact tuple), `taxonomy_version` (edgartools version) — and `cik`/`value`/`form`/`version` (FR-3, AD-14, AD-5/AD-15).
2. **Given** facts carrying dimensional/segment axis members **Then** they are **NOT** ingested (consolidated-only, AD-15).
3. **Given** the same CIK is ingested twice **Then** Tier 0 is unchanged **on read** (idempotent by identity key; ingest-monotonic `version`; read via `FINAL`/`argMax`) (AD-6).
4. **Given** a tag outside the standard taxonomies (`us-gaap`/`dei`/`srt`) **Then** it is not landed in Tier 0 (AD-9 scope).
5. **Given** the fetch path **Then** EDGAR is reached **only** through the Story 1.3 `EdgarClient.run(...)` (rate-limited + cool-down, AD-3); tests use fixtures / constructed facts and **never** hit live EDGAR (NFR-7).

## Tasks / Subtasks

- [x] **Task 1 — Thin EDGAR fetch through the rate-limited client (AD-3)** (AC: 1, 5)
  - [x] New `fintin/adapters/edgar/facts.py` — `fetch_company_facts(client: EdgarClient, cik: int) -> EntityFacts` that returns `client.run(lambda: edgar.get_company_facts(int(cik)), description=f"companyfacts CIK {cik}")`. This keeps the `edgar` import inside `adapters/edgar/` (AD-3) and applies the Story 1.3 cool-down to the companyfacts download (which raises `TooManyRequestsError` on a 429). Do NOT call `edgar.get_company_facts` anywhere else.
  - [x] `edgar.get_company_facts(cik)` (exported from `edgar/__init__.py`) downloads `https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010}.json` via edgar's throttled client and parses it into an `EntityFacts` (iterable of `FinancialFact`). ⚠️ It also consults an in-memory + on-disk cache first; that's fine — a cache hit simply skips the network.

- [x] **Task 2 — Pure transform: FinancialFact → Tier 0 rows (filters + provenance)** (AC: 1, 2, 4)
  - [x] New `fintin/core/ingest.py` (pure, no I/O) — `to_raw_fact_rows(facts: Iterable[FinancialFact], *, cik: int, taxonomy_version: str, version: int) -> list[RawFactRow]`. `RawFactRow` is a frozen dataclass/`NamedTuple` mirroring the `raw_fact` columns in order.
  - [x] **Filters (drop, do not land):**
    - **Consolidated-only (AD-15):** skip `fact.is_dimensioned` (it's a `@property` — no parens; `bool(fact.dimensions)`).
    - **Standard taxonomies only (AD-9):** keep only `fact.taxonomy in {"us-gaap", "dei", "srt"}`.
    - **Numerical only (AC-1 / FR-3):** skip when `fact.numeric_value is None`.
  - [x] **Field mapping** (`FinancialFact` → `raw_fact`):
    - `cik` ← the ingested CIK (`UInt32`); `raw_tag` ← `fact.concept` (the **full qualified** element, e.g. `"us-gaap:Revenues"` — keep the namespace so the identity key can't collide across taxonomies, since `taxonomy` is not in the key); `raw_label` ← `fact.label`; `taxonomy` ← `fact.taxonomy`; `unit` ← `fact.unit`; `value` ← `float(fact.numeric_value)`; `form` ← `fact.form_type`; `filed_date` ← `fact.filing_date`; `accession` ← `fact.accession` (ensure the dashed 20-char canonical form, e.g. `0000320193-24-000123`; normalize if edgar hands back a bare form).
    - ⚠️ **`value` scale — verify (off-by-1000× risk):** SEC `companyfacts` `val` is the **actual** reported value (already in `unit`, not scaled). Confirm against the installed source whether `fact.numeric_value` is that actual value or has `fact.scale` applied/removed, and store the **actual** value. Add a fixture assertion pinning a known value so a scale regression is caught.
    - **Period (AD-17):** duration → `period_start = fact.period_start`, `period_end = fact.period_end`; **instant** (`fact.period_start is None`, `fact.period_type == "instant"`) → `period_start = period_end = fact.period_end`.
    - `taxonomy_version` ← the passed edgartools version string; `version` ← the passed ingest-monotonic version.
  - [x] **`content_hash` (AD-14)** = `sha256` of a **stable** serialization of the normalized fact tuple — identity + value + provenance, fixed field order, a separator that can't appear in the fields (e.g. `"\x1f".join([...])`), value as a round-trippable string (`repr(float(value))`), dates as `.isoformat()`. Deterministic across runs (it detects at-rest corruption later). Put the hash helper in `core/ingest.py`.
  - [x] Skip facts missing an `accession` or `period_end` (can't form the identity key) rather than crashing; count/log them.

- [x] **Task 3 — Store adapter: insert into `raw_fact` (AD-4, AD-18)** (AC: 1, 3)
  - [x] New `fintin/adapters/store/raw_fact_repo.py` — `insert_raw_facts(client, rows: Sequence[RawFactRow]) -> int` using clickhouse-connect `client.insert("raw_fact", data, column_names=[...])` with the columns in schema order: `cik, accession, raw_tag, raw_label, taxonomy, period_start, period_end, unit, value, form, filed_date, content_hash, taxonomy_version, version`. Returns the number of rows inserted. **The store adapter owns all ClickHouse writes** (no DDL here — Story 1.2 owns DDL). Empty input → no-op, return 0.
  - [x] Insert-only (AD-6): never `UPDATE`/`DELETE`. `raw_fact` is `ReplacingMergeTree(version)` on the identity key, so a re-ingest with a higher `version` supersedes on read; duplicates collapse under `FINAL`/`argMax`.

- [x] **Task 4 — Orchestrator + ingest-monotonic version (AC: 1, 3, 5)**
  - [x] In `fintin/core/ingest.py` — `ingest_company(edgar_client: EdgarClient, store_client, cik: int, *, version: int | None = None) -> IngestResult`: fetch (`fintin.adapters.edgar.facts.fetch_company_facts`) → `to_raw_fact_rows(...)` (with `taxonomy_version = edgar-version`, `version`) → `insert_raw_facts(...)`. Return a small `IngestResult` (cik, facts_seen, rows_landed, dropped counts). Get the edgartools version via `importlib.metadata.version("edgartools")` (or `edgar.__version__`) — pass it in so `core` stays free of the `edgar` import (AD-3: the version string is data; the *fetch* import lives in the adapter).
  - [x] **Ingest-monotonic `version` (AD-6):** default to `time.time_ns()` captured once per run and stamped on every row of that run (`UInt64`, monotonic across runs, **not** `filed_date`). A later re-ingest gets a higher `version` and supersedes a corrupted prior copy at the query surface.

- [x] **Task 5 — Thin CLI trigger `ingest-company` (AD-2)** (AC: 1, 5)
  - [x] Add `fintin ingest-company CIK` to `fintin/cli/app.py` — load config, build `EdgarClient(cfg)` (its gate enforces a real contact email before any fetch) + a store client via `get_client`, call `ingest_company(...)`, print a clear summary line (facts seen, rows landed, dropped), close the store client in a `finally`. Dumb trigger — no ingestion logic in the CLI (AD-2). This is a **single-company** ingest; catch-up / backfill / reconciler / lease are Epic 3 (out of scope — see fences).
  - [x] Reuse the Story 1.1 patterns: `ConfigError`/`StoreConnectionError` → clear message + non-zero exit (not a traceback); structured logging to stdout. If `[edgar]` is missing/placeholder, `EdgarClient` raises `EdgarConfigError` → render it cleanly (tell the user to set their real contact email in `fintin.toml`).

- [x] **Task 6 — Tests (fixtures / constructed facts; NEVER live EDGAR)** (AC: 1, 2, 3, 4, 5)
  - [x] New `tests/test_ingest.py` — **pure transform** unit tests (no container, no network) building `FinancialFact` objects directly (⚠️ verify the dataclass' required fields against the installed `edgar/entity/models.py`; `numeric_value`, `period_type`, etc.):
    - us-gaap numeric fact lands with all provenance columns populated; `dei` and `srt` also land.
    - **dimensioned fact dropped** (`dimensions={"...Axis": "...Member"}` → `is_dimensioned` True) — AC-2. (The companyfacts API never emits dimensioned facts, so only a directly-constructed fact can exercise this branch.)
    - **non-standard taxonomy dropped** (e.g. `taxonomy="ifrs-full"` or a custom namespace) — AC-4.
    - **non-numeric dropped** (`numeric_value=None`).
    - **AD-17:** an instant fact → `period_start == period_end`; a duration → `period_start < period_end`.
    - **`content_hash`** is deterministic (same fact → same hash across calls) and differs when any identity/value field differs.
    - `version` / `taxonomy_version` stamped from the passed values.
  - [x] `tests/test_ingest.py` (or `test_edgar_facts.py`) — **fetch routing (AC-5):** monkeypatch `edgar.get_company_facts` to return a sentinel (or a parsed fixture) and assert `fetch_company_facts(client, cik)` goes **through** `client.run` (e.g. inject a client whose `run` records the call). No network.
  - [x] `tests/test_raw_fact_repo.py` — `@pytest.mark.integration` against a **throwaway database** (reuse the Story 1.2 `schema_client` pattern: unique `fintin_test_<uuid>` DB, `create_schema`, drop in `finally`): insert rows, read them back via `SELECT ... FROM raw_fact FINAL`; assert provenance columns; **idempotency (AC-3):** insert the same rows twice with a **higher** `version` on the second pass → `FINAL` row count and values unchanged.
  - [x] `tests/test_ingest.py` — **end-to-end offline:** a fake `EdgarClient` whose `run` returns an `EntityFacts` (built from a small **hand-crafted companyfacts JSON fixture** parsed via `edgar.entity.parser.EntityFactsParser.parse_company_facts`, OR a list of constructed `FinancialFact`) + a real throwaway-DB store client → `ingest_company(...)` lands the expected consolidated/standard/numeric rows and drops the rest.
  - [x] `tests/test_cli.py` — `ingest-company` help/wiring: missing config → clean `ConfigError` (exit ≠ 0, no traceback); missing/placeholder `[edgar]` → clean `EdgarConfigError` message. Keep it offline (mock the ingest).
  - [x] If a recorded/hand-crafted `companyfacts` JSON fixture is used, put it under `tests/fixtures/`. **Hard rule:** the test suite makes **zero** live EDGAR calls (NFR-7).

## Dev Notes

### What this story IS
The **first ingestion**: fetch one company's `companyfacts` through the Story 1.3 rate-limited client, filter to consolidated standard-taxonomy numeric facts, stamp full provenance + a content hash + an ingest-monotonic version, and land them in the Tier 0 `raw_fact` table (Story 1.2 schema). It makes `fintin ingest-company <CIK>` real. This is the raw local mirror everything downstream (Tier 1 mapping in 1.5, resolution/mart in 1.6) derives from.

### What this story is NOT (scope fences — do not implement)
- ❌ **No Tier 1 / canonical mapping.** `raw_tag` stays the as-tagged element; `canonical_fact` and the edgartools standardization mapping are **Story 1.5**. AC-4 here just means non-standard-taxonomy tags never land in Tier 0 — not that we map anything.
- ❌ **No resolution MV / mart querying.** Story 1.6. (Landing `raw_fact` doesn't touch the MV — the MV reads `canonical_fact` inserts, not `raw_fact`.)
- ❌ **No reconciler / work-list / high-water mark / catch-up / multi-company backfill / Universe.** Epic 3 (FR-7/8/9). This story ingests **one explicitly-provided CIK**.
- ❌ **No single-flight lease / heartbeat (AD-12).** Epic 3.
- ❌ **No bulk `companyfacts.zip` strategy (AD-13).** Per-company API only (v1).
- ❌ **No dimensional/segment facts (AD-15).** Dropped, not stored.
- ❌ **No new DDL.** Story 1.2 owns schema (AD-18); this story only inserts.

### Builds directly on Stories 1.2 & 1.3 (previous-story intelligence)
- **Story 1.3 `EdgarClient`** (`fintin/adapters/edgar/client.py`): construct from `Config`; its gate rejects a blank/placeholder contact email before any fetch. **All EDGAR access must go through `client.run(operation, *, description=...)`** — that's where the ≥10-min cool-down / bounded retry lives. `run` re-raises `EdgarThrottleError` after exhausting retries and lets non-throttle errors propagate. The AC-5 structural test (no `edgar`/`httpx` import outside `adapters/edgar/`) from 1.3 will now also cover `facts.py` — keep `edgar` imports confined there (`core/ingest.py` must NOT import `edgar`; take the version string as a parameter).
- **Story 1.2 `raw_fact`** (`fintin/adapters/store/schema.py`): `ReplacingMergeTree(version)`, `ORDER BY (accession, raw_tag, period_start, period_end, unit)`, columns `cik UInt32, accession String, raw_tag String, raw_label String, taxonomy LowCardinality(String), period_start Date, period_end Date, unit String, value Float64, form LowCardinality(String), filed_date Date, content_hash String, taxonomy_version String, version UInt64`. `version` is **ingest-monotonic, not `filed_date`** (AD-6). Readers use `FINAL`/`argMax` (never assume a merge ran).
- **Store client** (`fintin/adapters/store/client.py`): `get_client(cfg, *, database=None)`; always `close()` in a `finally` (1.1/1.2 review). clickhouse-connect: `client.insert(table, data, column_names=[...])` for writes; `client.query("SELECT … FINAL").result_rows` for reads.
- **Test harness:** reuse `tests/conftest.py` integration gating + the Story 1.2 throwaway-DB `schema_client` fixture pattern for anything touching ClickHouse. EDGAR-touching tests are pure unit (no `@pytest.mark.integration`), offline.
- **Runtime:** `uv run …`; Python ≥ 3.12.

### edgartools 5.43.0 facts API (⚠️ verify against the *installed* package; never fetch live to verify)
Verified against installed source (2026-07-24). Key modules: `edgar/entity/entity_facts.py`, `edgar/entity/models.py`, `edgar/entity/parser.py`.
- **Fetch:** `edgar.get_company_facts(cik: int) -> EntityFacts` (`entity/entity_facts.py:97`). Downloads via `download_company_facts_from_sec` → `download_json(build_company_facts_url(cik))` — **goes through edgar's throttled `http_client`** (so `TooManyRequestsError` is possible → wrap in `EdgarClient.run`). Checks an in-memory + on-disk cache first.
- **`EntityFacts`** (`entity/entity_facts.py:136`) is **iterable** (`__iter__` yields `FinancialFact`), has `__len__`, `get_all_facts() -> list`, and a `query()` builder. For landing, just iterate it.
- **`FinancialFact`** (`entity/models.py:~24`) fields: `concept: str` (e.g. `"us-gaap:Revenues"`), `taxonomy: str` (`"us-gaap"`/`"dei"`/`"srt"`/…), `label: str`, `value`, `numeric_value: Optional[float]`, `unit: str`, `scale`, `period_start: Optional[date]`, `period_end: date`, `period_type: 'instant'|'duration'`, `fiscal_year`, `fiscal_period`, `filing_date: date`, `form_type: str`, `accession: str`, `dimensions: Optional[Dict[str,str]]`, and **`is_dimensioned` (a `@property`)**. Use `numeric_value` (not `value`) for the numeric column; drop `numeric_value is None`.
- **Fixture parsing (offline):** `edgar.entity.parser.EntityFactsParser.parse_company_facts(json_dict) -> EntityFacts` builds facts from a companyfacts JSON with **no network** — the way to exercise the fetch→parse→transform path from a saved/hand-crafted fixture. companyfacts JSON shape: `{"cik":…, "entityName":…, "facts": {"us-gaap": {"<Tag>": {"label":…, "units": {"USD": [{"start":…, "end":…, "val":…, "accn":"0000…-..-……", "fy":…, "fp":…, "form":"10-K", "filed":"YYYY-MM-DD"}]}}}, "dei": {…}}}`. Note: the companyfacts API returns **consolidated** values only — it never emits dimensioned facts, so the AC-2 drop branch must be tested with a **directly-constructed** dimensioned `FinancialFact`.
- **Version:** `edgar.__version__ == "5.43.0"`; or `importlib.metadata.version("edgartools")`. This is `taxonomy_version` (AD-14) — the mapping-library version, distinct from the per-fact `taxonomy` namespace.

### Architecture decisions this story must obey
| AD | Rule as it applies here |
| --- | --- |
| **AD-3** | Every EDGAR request through the one `EdgarClient` (`.run`). `edgar` imported only in `adapters/edgar/`. `core/ingest.py` stays edgar-free (version passed in). |
| **AD-4** | Tier 0 = immutable raw landing, hoards **all** standard-taxonomy facts (not only mappable ones). Recovery flows EDGAR→Tier 0 only. |
| **AD-5 / AD-15** | Identity key `(accession, raw_tag, period_start, period_end, unit)`; consolidated-only (drop dimensioned). `raw_tag` = full qualified concept so the key can't collide across namespaces. |
| **AD-6** | Insert-only; `version` ingest-monotonic (`time.time_ns()`/run), **not** `filed_date`; reads via `FINAL`/`argMax`; a re-ingest supersedes a corrupted copy. |
| **AD-9** | Only `us-gaap`/`dei`/`srt` land; anything else stays out of Tier 0. Every row stamped with `taxonomy_version` (edgartools version). |
| **AD-14** | Provenance on every fact: `raw_tag`, `raw_label`, `unit`, period, `filed_date`, `content_hash` = sha256(normalized tuple), `taxonomy_version`. `content_hash` detects at-rest corruption later. |
| **AD-17** | Instant → `period_start = period_end`; duration → `period_start < period_end`. |
| **AD-18** | No DDL here — the store adapter (Story 1.2) is the sole schema owner; this story only inserts. |
| **AD-2** | `ingest-company` CLI is a dumb trigger; ingestion logic lives in `core`/adapters. |

### Files to touch
```text
fintin/adapters/edgar/facts.py     # NEW — fetch_company_facts(client, cik) via EdgarClient.run (sole get_company_facts caller)
fintin/core/ingest.py              # NEW — pure to_raw_fact_rows(...) + content_hash + ingest_company(...) orchestrator (NO edgar import)
fintin/adapters/store/raw_fact_repo.py  # NEW — insert_raw_facts(client, rows) into raw_fact (store owns writes)
fintin/cli/app.py                  # UPDATE — add `ingest-company CIK` thin trigger
tests/test_ingest.py               # NEW — pure transform + fetch-routing + end-to-end-offline tests
tests/test_raw_fact_repo.py        # NEW — integration insert + idempotency (throwaway DB)
tests/test_cli.py                  # UPDATE — ingest-company wiring / clean errors
tests/fixtures/                    # NEW (if a companyfacts JSON fixture is used) — never live
```

### Testing standards
- `pytest` via `uv run pytest`. Transform/fetch/CLI tests are **unit** (no container, no network); repo/idempotency tests are `@pytest.mark.integration` (throwaway DB, auto-skip without Docker).
- **Absolute rule (NFR-7):** the suite makes **zero** live EDGAR calls — build `FinancialFact` directly and/or parse a saved companyfacts JSON via `EntityFactsParser`; fake `EdgarClient.run` for the orchestrator test.
- Verify `FinancialFact` construction args and `EntityFacts` iteration against the installed `edgar/entity/models.py` + `entity_facts.py` — don't guess field names.
- Never touch the real `default` DB — unique throwaway DB per integration test (Story 1.2 pattern).

### Project Structure Notes
- `facts.py` (fetch) in `adapters/edgar/`; `raw_fact_repo.py` (write) in `adapters/store/`; `ingest.py` (pure transform + orchestration) in `core/`. Matches the hexagonal source tree: `core` depends on nothing outward and stays `edgar`-free; adapters do the I/O. No variances.
- `ingest-company` is the story's trigger; the full catch-up/backfill CLI (`catch-up`, `backfill`, `status`) arrives in Epic 3 — don't pre-build it.

### References
- [Source: _bmad-output/planning-artifacts/epics.md#Story-1.4] — user story + ACs (FR-3, AD-14, AD-15, AD-6, AD-9)
- [Source: .../architecture/.../ARCHITECTURE-SPINE.md#AD-4] [#AD-5] [#AD-6] [#AD-9] [#AD-14] [#AD-15] [#AD-17] [#AD-3] [#AD-2] [#AD-18] — governing decisions
- [Source: .../ARCHITECTURE-SPINE.md#Consistency-Conventions] — cik UInt32, accession dashed 20-char, value Float64, dates Date, snake_case
- [Source: .../architecture/.../BUILD-SPLIT.md#Epic-C] — ingestion deliverables + "consolidated-only; unmappable tags stay in Tier 0" watch
- [Source: .../prds/.../prd.md#FR-3] — immutable raw fact landing (testable consequences)
- [Source: _bmad-output/implementation-artifacts/1-3-edgar-client.md] — EdgarClient.run, gate, structural import guard
- [Source: _bmad-output/implementation-artifacts/1-2-store-schema-ddl.md] — raw_fact schema, throwaway-DB test pattern, FINAL reads
- edgartools installed source: `edgar/entity/entity_facts.py` (`get_company_facts`, `EntityFacts`), `edgar/entity/models.py` (`FinancialFact`, `is_dimensioned`), `edgar/entity/parser.py` (`EntityFactsParser.parse_company_facts`)

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Claude Opus 4.8, 1M context) — bmad-dev-story workflow.

### Debug Log References

- **edgartools 5.43.0 facts API verified against the installed source (offline):** `edgar.get_company_facts(cik) -> EntityFacts` (iterable of `@dataclass(slots=True) FinancialFact`); required ctor fields `concept, taxonomy, label, value, numeric_value, unit`; `is_dimensioned` is a property over `dimensions`. `EntityFactsParser.parse_company_facts(json)` builds facts offline.
- **value/scale resolved (off-by-1000× risk closed):** `entity/parser.py` sets `value = fact_data.get('val')` then `numeric_value = float(value)` — i.e. `numeric_value` is the **actual** SEC companyfacts `val`; `scale` is separate metadata that edgartools does **not** apply. So `value = float(fact.numeric_value)` stores the real reported value.
- **Layering / AD-3 guard:** `core/ingest.py` imports no `edgar` and no ClickHouse — the transform types facts via a local `FactLike` Protocol (edgar's `FinancialFact` satisfies it structurally), and the orchestrator takes injected `fetch_facts`/`insert_rows` callables. `edgar` is imported only in `adapters/edgar/facts.py`. The Story 1.3 structural import-guard test still passes.
- **CLI startup perf:** the ingest command imports the edgar/ingest modules lazily (in-function) so `--help` / `check-connection` / `schema-init` don't pay the heavy edgartools import.
- Full suite: **86 passed** (63 prior + 23 new). Integration (`raw_fact` insert/idempotency) ran against live CH `26.3.17.56`; all EDGAR-touching tests are offline (constructed `FinancialFact` + faked `run`). No live EDGAR.

### Completion Notes List

- **All 5 ACs satisfied:**
  - **AC-1** — `to_raw_fact_rows` maps a `FinancialFact` to a `raw_fact` row with full provenance (`raw_tag`=full qualified concept, `raw_label`, `taxonomy`, `unit`, period, `filed_date`, `content_hash`=sha256 of the normalized tuple, `taxonomy_version`=`edgar.__version__`) + `cik`/`value`/`form`/`version`; asserted in `test_ingest.py`.
  - **AC-2** — dimensioned facts dropped (`fact.is_dimensioned`); tested with a directly-constructed dimensioned fact (companyfacts never emits one).
  - **AC-3** — re-ingesting the same CIK leaves Tier 0 unchanged on read (`ReplacingMergeTree(version)` + `FINAL`); integration-tested, plus a corrected-higher-version supersession test (survives `OPTIMIZE … FINAL`).
  - **AC-4** — non-standard taxonomies dropped (`taxonomy not in {us-gaap,dei,srt}`).
  - **AC-5** — the only `edgar.get_company_facts` call is inside `fetch_company_facts`, routed through `EdgarClient.run(...)`; the suite makes zero live EDGAR calls.
- **Structure (hexagonal):** pure transform + orchestration in `core/ingest.py` (no I/O, no `edgar`); fetch in `adapters/edgar/facts.py`; `raw_fact` insert in `adapters/store/raw_fact_repo.py`; `fintin ingest-company CIK` thin trigger in the CLI (builds `EdgarClient` — its gate enforces a real contact email — + a store client, wires the ports, prints a summary).
- **Ingest-monotonic `version`** defaults to `time.time_ns()` per run (AD-6); insert-only; reads via `FINAL`.
- Scope held: no Tier 1/canonical mapping (1.5), no MV/mart querying (1.6), no reconciler/backfill/lease (Epic 3), no bulk zip (AD-13), no new DDL (1.2 owns schema). No new dependencies (edgartools already added in 1.3).
- `tests/fixtures/` not needed — the transform is exercised with constructed `FinancialFact` objects (the robust way to cover the dimensioned-drop branch) and the orchestrator with faked ports; a recorded companyfacts JSON would add nothing here and risks a live fetch to obtain.

### File List

**New:**
- `fintin/core/ingest.py` — `FactLike` protocol, `RawFactRow`, `IngestResult`, `to_raw_fact_rows`, `content_hash`, `normalize_accession`, `ingest_company` (pure; no edgar/CH imports)
- `fintin/adapters/edgar/facts.py` — `fetch_company_facts(client, cik)` via `EdgarClient.run`; `edgartools_version()` (sole `edgar.get_company_facts` caller)
- `fintin/adapters/store/raw_fact_repo.py` — `insert_raw_facts(client, rows)` into `raw_fact`
- `tests/test_ingest.py` — pure transform, fetch-routing, orchestrator (all offline)
- `tests/test_raw_fact_repo.py` — `raw_fact` insert + idempotency + supersession (throwaway DB)

**Modified:**
- `fintin/cli/app.py` — `ingest-company CIK` thin trigger (lazy edgar imports)
- `tests/test_cli.py` — `ingest-company` help + clean-error (missing config / missing-or-placeholder `[edgar]`) tests

### Change Log

- 2026-07-24 — Story 1.4 implemented: Tier 0 ingestion of one company's facts. `core/ingest.py` (pure) filters fetched facts to consolidated (`is_dimensioned` drop, AD-15), standard-taxonomy (AD-9), numeric facts and maps them to `raw_fact` rows with full provenance + `content_hash` (sha256 normalized tuple, AD-14) + AD-17 periods + ingest-monotonic `version` (AD-6); `adapters/edgar/facts.py` fetches `companyfacts` through `EdgarClient.run` (AD-3); `adapters/store/raw_fact_repo.py` inserts into `raw_fact`; `fintin ingest-company CIK` CLI trigger. Verified `numeric_value` is the actual (unscaled) SEC `val`. 23 new tests; **86 passed**. Status → review.
