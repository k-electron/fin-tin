# Story 1.5: Map raw facts to canonical Tier 1

Status: ready-for-dev

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the builder,
I want Tier 0 facts mapped to canonical concepts in Tier 1 with zero network,
so that facts become cross-company comparable.

## Acceptance Criteria

1. **Given** Tier 0 has a company's facts **When** I run the mapping **Then** `canonical_fact` is populated via the edgartools standardization taxonomy, keyed by raw-fact identity, with `canonical_concept` + `taxonomy_version` as attributes, **issuing zero EDGAR/network requests** (FR-4, AD-4, AD-9).
2. **Given** a raw tag edgartools cannot standardize **Then** no `canonical_fact` row is created for it (it remains only in Tier 0).
3. **Given** the mapping is re-run **Then** it is an in-place upsert with no orphaned/duplicate rows on read (AD-5, AD-6).

## Tasks / Subtasks

- [ ] **Task 1 — Offline standardization adapter** (AC: 1, 2) — `fintin/adapters/edgar/standardize.py` (NEW)
  - [ ] `standardize_concept(raw_tag: str) -> str | None`: strip the namespace prefix (`us-gaap:` / `dei:` / `srt:` → local name) then return `edgar.xbrl.standardization.reverse_index.get_standard_concept(local_name)`. Returns `None` for unmappable **or** excluded tags — this is the AC-2 "no canonical row" signal.
  - [ ] `taxonomy_version() -> str`: return `edgar.__version__` (`"5.43.0"`) — the AD-9/AD-14 taxonomy_version. (Self-contained; do NOT import from `facts.py`/`client.py` so the map path never drags in the rate-limited client.)
  - [ ] This module imports `edgar` but MUST NOT construct `EdgarClient`, call `edgar.set_identity`, or call any fetch (`get_company_facts`, `get_filings`, …). It is the second `edgar` importer (alongside `client.py`/`facts.py`) but a **pure-offline** one.
- [ ] **Task 2 — Pure core mapping transform** (AC: 1, 2) — `fintin/core/canonical.py` (NEW)
  - [ ] `CanonicalFactRow` NamedTuple in **exact `canonical_fact` column order**: `cik, accession, raw_tag, canonical_concept, raw_label, period_start, period_end, unit, value, form, filed_date, content_hash, taxonomy_version, version`.
  - [ ] `MapResult` NamedTuple: `cik, raw_seen, mapped, unmapped, version` (+ a `.dropped`-style clarity property if useful).
  - [ ] `to_canonical_fact_rows(raw_rows, *, cik, standardize, taxonomy_version, version) -> (list[CanonicalFactRow], MapResult)`: for each `RawFactRow`, look up `canonical_concept = standardize(row.raw_tag)`; if `None`, count `unmapped` and emit nothing; else emit a `CanonicalFactRow` **carrying over** `content_hash` from the Tier 0 row (AD-5 attribute / AD-14 provenance), preserving identity fields verbatim, stamping the supplied `taxonomy_version` and `version`.
  - [ ] **DISASTER GUARD — construct by NAMED field, never positionally.** `RawFactRow` has `taxonomy` at index 4; `CanonicalFactRow` has `canonical_concept` at index 3 and NO `taxonomy`. A positional copy would silently write the raw taxonomy into `canonical_concept` and shift every later column. Build `CanonicalFactRow(cik=r.cik, accession=r.accession, raw_tag=r.raw_tag, canonical_concept=concept, raw_label=r.raw_label, …)` explicitly by keyword; drop `taxonomy`.
  - [ ] `standardize` is an injected **port** (`Callable[[str], str | None]`) — `core` imports NO `edgar`. Add the AD-3-style AST import guard test.
  - [ ] No intra-batch dedup: `raw_rows` come from `raw_fact FINAL` (already unique by identity key), and `raw_tag` stays in the Tier 1 key, so each Tier 0 row maps 1:1 to at most one Tier 1 row. (Two different raw tags that map to the *same* canonical concept keep DISTINCT identity keys — resolving those is Story 1.6's latest-filed-wins job, not this story's.)
- [ ] **Task 3 — Store: read Tier 0, write Tier 1** (AC: 1, 3)
  - [ ] `fintin/adapters/store/raw_fact_repo.py` (UPDATE): add `read_raw_facts(client, cik) -> list[RawFactRow]` — `SELECT <cols> FROM raw_fact FINAL WHERE cik = %(cik)s`, parameterized (never string-interpolate the CIK), returning `RawFactRow` tuples (Tier 0 → Tier 1 derivation is one-way, AD-4). **The SELECT column list MUST enumerate `RAW_FACT_COLUMNS` in `RawFactRow` field order** (don't rely on `SELECT *` ordering) so `RawFactRow(*row)` is correct; assert this with a round-trip test.
  - [ ] `fintin/adapters/store/canonical_fact_repo.py` (NEW): `CANONICAL_FACT_COLUMNS` (schema order); `insert_canonical_facts(client, rows) -> int` (empty = no-op); `next_canonical_version(client) -> int` = `max(version)+1` over `canonical_fact` (1 if empty) — Tier 1's own ingest-monotonic sequence (AD-6), independent of Tier 0's.
- [ ] **Task 4 — Core orchestrator** (AC: 1, 2, 3) — `fintin/core/canonical.py`
  - [ ] `map_company(cik, *, read_raw_facts, standardize, insert_rows, taxonomy_version, version) -> MapResult`: `read_raw_facts(cik)` → `to_canonical_fact_rows(...)` → `insert_rows(rows)`; return the `MapResult`. Injected ports only (no adapter imports in `core`).
- [ ] **Task 5 — CLI trigger** (AC: 1, 2, 3) — `fintin/cli/app.py` (UPDATE): `map-canonical CIK`
  - [ ] Lazy-import the edgar/standardize/core/store modules (keep `--help`/`check-connection`/`schema-init` fast).
  - [ ] Validate `1 <= cik <= 4_294_967_295` (exit 2) before any work — mirror `ingest-company`.
  - [ ] **No `EdgarClient`, no contact-email gate** — mapping is zero-network (AC-1), so it must run even with a placeholder email. This is the structural embodiment of "zero EDGAR requests".
  - [ ] `check_connection` (surface CH problems), `get_client`, `version = next_canonical_version(client)`, then `map_company(...)`.
  - [ ] If `result.raw_seen == 0`: emit a clear yellow message ("no Tier 0 facts for CIK N — run `ingest-company N` first") and exit 1 (the analog of `NoCompanyFactsError`).
  - [ ] Success line: `Mapped CIK N: {mapped} canonical facts from {raw_seen} Tier 0 facts ({unmapped} tags unmappable, kept in Tier 0 only).`
  - [ ] `close()` the client in `finally`.
- [ ] **Task 6 — Reconcile screening_mart labels to real edgartools output** (supporting; unblocks AC-1 end-to-end + Story 1.6) — `fintin/adapters/store/schema.py` (UPDATE)
  - [ ] The mart's canonical-label filters were provisional guesses in Story 1.2 (`schema.py:104` "extend as the mapping (Story 1.5) lands"). Empirically, `get_standard_concept` emits `Revenue` (not `Revenues`) and `NetIncome` (not `NetIncomeLoss`); `Assets`/`Liabilities` already match. Change the two mismatched **filter labels** in `SCREENING_MART`: `'Revenues' → 'Revenue'`, `'NetIncomeLoss' → 'NetIncome'`. Keep the SQL **column aliases** (`revenues`, `net_income`, `assets`, `liabilities`) unchanged so downstream queries are unaffected.
  - [ ] `SCREENING_MART` is `CREATE OR REPLACE VIEW`, so `schema-init` re-applies it live (no migration needed). Update the accompanying comment. AD-18: this is the store adapter, the sole DDL owner — compliant.
  - [ ] **REGRESSION — update Story 1.2's mart tests in lockstep.** `tests/test_schema.py` inserts synthetic `canonical_fact` rows whose `canonical_concept` literal (the 4th VALUES field) is `'Revenues'` and asserts the mart `revenues` column (lines ~117, 152, 171, 190). Changing the mart filter to `'Revenue'` makes those rows stop matching → tests fail. Fix: change only the `canonical_concept` literal `'Revenues' → 'Revenue'` in those synthetic inserts (leave `raw_tag`/`raw_label` as `'us-gaap:Revenues'`/`'Revenues'`). The `net_income` column is only ever asserted `None` (no NetIncome row is inserted), so the `NetIncomeLoss → NetIncome` filter change needs no test data change. Run `test_schema.py` green after.
- [ ] **Task 7 — Tests (never hit live EDGAR; NFR-7)**
  - [ ] `tests/test_canonical.py` (pure/offline): map/unmap/None-drop; provenance carry-over (content_hash carried, taxonomy_version + version stamped, identity preserved); two-tags→same-concept keeps two rows; `map_company` wiring; AST import guard (no `edgar` in `core/canonical.py`).
  - [ ] `tests/test_standardize.py` (offline, real edgartools mapping): `standardize_concept('us-gaap:Assets') == 'Assets'`, `('us-gaap:Revenues') == 'Revenue'`, `('us-gaap:NetIncomeLoss') == 'NetIncome'`, `('us-gaap:ZzzFakeConceptXyz') is None`, prefix-strip parity (`'Assets'` == `'us-gaap:Assets'`); **`test_standardize_is_offline`**: block `socket.socket.connect`/`socket.create_connection` and assert a lookup still succeeds (proves AC-1 zero-network); `taxonomy_version() == edgar.__version__`.
  - [ ] `tests/test_canonical_fact_repo.py` (integration, throwaway DB): `read_raw_facts` round-trip; `insert_canonical_facts` + FINAL read-back; `next_canonical_version` monotonic; **re-map idempotency-on-read** (insert v1 then v2 same identity, corrected value → FINAL count unchanged, higher `version`/value wins, holds after `OPTIMIZE … FINAL`) = AC-3; **end-to-end mart seam**: land raw `Revenues`+`Assets` facts → `read_raw_facts` → map (real standardizer) → `insert_canonical_facts` → assert `screening_mart` `revenues`/`assets` columns are populated (proves Task 6 + MV auto-populate).
  - [ ] `tests/test_cli.py` (UPDATE): `--help` lists `map-canonical`; invalid CIK → exit 2; missing config → clean error (exit 2, no traceback); (integration) empty-Tier-0 CIK → yellow "ingest first", exit 1.
- [ ] **Task 8 — Validate & document**
  - [ ] `uv run pytest` green (unit + integration with local ClickHouse up).
  - [ ] Update `README.md`: add the `map-canonical CIK` step to the pipeline flow (`ingest-company` → `map-canonical`), noting it is offline and needs no EDGAR email.
  - [ ] Append any deferrals to `_bmad-output/implementation-artifacts/deferred-work.md`.
  - [ ] Fill Dev Agent Record + File List; set Status appropriately.

## Dev Notes

### The standardization API (verified against installed edgartools 5.43.0, fully offline)

- **Use `edgar.xbrl.standardization.reverse_index.get_standard_concept(tag)`** — the raw-concept → canonical-concept-id map. It is name-only (no statement context needed), case-insensitive, and returns `None` for unknown **or excluded** tags. This is a DIFFERENT system from `edgar.entity.mappings_loader` (whose import-time logs "Loaded 114 learned concept mappings" / "Loaded canonical structures for 106 statement types" are about statement *structure*, not label standardization — do NOT use those).
- **Prefix handling:** the API auto-strips `us-gaap:` / `dei:` / `ifrs-full:` but **not** `srt:`. To be namespace-agnostic and correct for all three standard taxonomies, the adapter strips the prefix itself (`tag.split(':', 1)[1] if ':' in tag else tag`) before the lookup. (srt axes generally return `None` anyway — correct, they aren't canonical facts.)
- **canonical_concept = the standard-concept id** (e.g. `Assets`, `Revenue`, `NetIncome`, `TradePayables`, `SharesYearEnd`), **not** the display name (`get_display_name` → `Total Assets`, `Accounts Payable`). Rationale: the id is the stable cross-company join key, and the existing `screening_mart` already filters on ids (`Assets`, not `Total Assets`). Decision recorded for review.
- **Data is bundled JSON** at `.venv/.../edgar/xbrl/standardization/gaap_mappings.json` (+ `display_names.json`, hardcoded `exclusions.py`); loaded via `open()`+`json.load`, cached as a module singleton. No `requests`/`httpx`/`urllib`/`socket` in the load or lookup path — **empirically confirmed offline** (lookups succeed with `socket.connect` blocked).
- **Ambiguous tags do NOT return `None`.** ~206 tags map to multiple candidate concepts; with no context supplied, `get_standard_concept` deterministically returns the first (primary) candidate — so they DO produce a canonical row. Acceptable for v1; context/industry-based disambiguation is deferred. No special handling needed — just take the returned value.
- **taxonomy_version = `edgar.__version__` = `"5.43.0"`** per AD-9/AD-14 ("the edgartools package version string"). The standardization index also carries its own version (`get_reverse_index().metadata['version']` → `"3.0.0"`), but since the mapping data is bundled *in* the package, the package version transitively pins it. Keeping Tier 0 and Tier 1 `taxonomy_version` identical (`5.43.0`) is the AD-literal choice. Decision recorded for review; the `3.0.0` mapping version is available if finer provenance is ever wanted (schema has a single `taxonomy_version` column).

### Measured behavior (Apple, CIK 320193, from the live-verified Tier 0)

- 505 distinct raw tags → **275 map** to a canonical concept, **230 unmappable** (stay in Tier 0 only — the AC-2 path, expected and correct). 117 distinct canonical concepts. A mapped fraction well below 100% is normal: obscure disclosure tags have no cross-company canonical.
- All four `screening_mart` concepts (`Revenue`, `NetIncome`, `Assets`, `Liabilities`) are present in Apple's canonical set → with Task 6's label fix the mart lights up.

### Architecture constraints (authoritative)

- **AD-4 — one-way derivation, zero network for Tier 1:** Tier 1 is rebuildable from Tier 0 with zero EDGAR. `map-canonical` reads `raw_fact` and writes `canonical_fact`; it never reaches EDGAR. Tier 1 is never a source for raw data. [Source: ARCHITECTURE-SPINE.md#AD-4]
- **AD-5 — Tier 1 keyed by raw-fact identity:** key = `(accession, raw_tag, period_start, period_end, unit)` — same shape as Tier 0. `canonical_concept`, `taxonomy_version`, `raw_label`, `filed_date`, `content_hash` are **attributes**, never key parts. A re-map is an in-place upsert on this key. [Source: ARCHITECTURE-SPINE.md#AD-5]
- **AD-6 — insert-only, ingest-monotonic version, correctness at read:** `canonical_fact` is `ReplacingMergeTree(version)`. Re-map = INSERT at a higher `version`; readers use `FINAL`/`argMax`. The version comes from the store (`next_canonical_version`), never a wall clock. [Source: ARCHITECTURE-SPINE.md#AD-6]
- **AD-9 — concept space = edgartools standardization; standard taxonomies only:** canonical concepts come solely from the edgartools standardization taxonomy; unmappable tags stay in Tier 0 and never enter Tier 1; every Tier 1 row carries `taxonomy_version`. (Tier 0 already enforced us-gaap/dei/srt-only in Story 1.4, so Tier 1 inherits that scope.) [Source: ARCHITECTURE-SPINE.md#AD-9]
- **AD-8 caveat (why re-map is safe here):** the Resolution MV (`resolved_fact_mv`) auto-fires on `canonical_fact` INSERT. Re-running the *same-version* mapping produces the *same* `canonical_concept` per identity key, so no MV column-retraction problem arises (the deferred cross-taxonomy re-map that *changes* a concept — which WOULD need a mart rebuild — is out of scope). The MV's `argMaxState(value, (filed_date, is_amendment, accession, version))` rank carries `version` last, so a re-map's duplicate contribution resolves to the higher-version copy with the same value. [Source: ARCHITECTURE-SPINE.md#AD-8; schema.py:90-101]
- **AD-2 — dumb trigger:** `map-canonical` parses args and invokes the core `map_company`; no mapping policy in the CLI. [Source: ARCHITECTURE-SPINE.md#AD-2]
- **AD-18 — single DDL owner:** the Task 6 mart-label change lives in `adapters/store/schema.py` (the sole DDL owner). No other module issues DDL. [Source: ARCHITECTURE-SPINE.md#AD-18]

### Project Structure Notes

- Mirrors Story 1.4's proven layering exactly: pure transform + orchestrator in `core/` (edgar-free via an injected port + AST guard), the edgar-touching piece isolated in `adapters/edgar/`, per-table access in `adapters/store/`, a dumb Typer command in `cli/`.
- `canonical_fact` DDL already exists (Story 1.2, `schema.py:56-74`) — this story writes to it, issues **no** DDL except Task 6's `CREATE OR REPLACE VIEW` label fix.
- `canonical_fact` = `raw_fact` minus the `taxonomy` column, plus `canonical_concept` (positioned right after `raw_tag`). `CanonicalFactRow` field order must match `CANONICAL_FACT_COLUMNS` must match the DDL.

### Previous Story Intelligence (Story 1.4)

- **`RawFactRow`** (`core/ingest.py:64`) is the Tier 0 row shape — reuse it as the *input* to the mapping (store `read_raw_facts` returns it). `content_hash`/`taxonomy_version`/`version` semantics established there carry over.
- **`content_hash`** in Story 1.4 is `sha256(repr(tuple(... incl taxonomy, raw_label ...)))` (`core/ingest.py:118`). Tier 1 **carries it over verbatim** — do not recompute (canonical_fact drops the `taxonomy` field, and the hash is provenance of the *source* Tier 0 row).
- **`next_ingest_version`** pattern (`raw_fact_repo.py:42`, `max(version)+1`) is the template for `next_canonical_version` — same monotonic-from-store guarantee.
- **Idempotency-on-read** was proven for Tier 0 both by the integration test (`test_raw_fact_repo.py`) and live (re-ingesting Apple: raw rows doubled to 49,704, FINAL stayed 24,852, all winning rows at the higher version). Replicate that exact test shape for `canonical_fact`.
- **CLI conventions:** lazy heavy imports; validate CIK first; `check_connection` before work; `close()` in `finally`; distinct exit codes (config=2, invalid-CIK=2, operational=1); no Python tracebacks in user-facing errors (`test_cli.py` asserts "Traceback" absent).
- **Test gating:** `@pytest.mark.integration` auto-skips when ClickHouse isn't listening (`conftest.py`); integration tests use a unique throwaway `fintin_test_<uuid>` DB and drop it in teardown (`test_raw_fact_repo.py:26`). The `local_clickhouse_config` fixture reads the real `fintin.toml`.

### Git Intelligence

- Recent commits (be570aa/4d1d622/56ccfc1 → merge 1d8ea30) established the docs→feat→fix→merge rhythm on a `story/1-<n>-<slug>` branch off `main`. Story 1.5 continues on `story/1-5-map-canonical-tier1`.
- Conventional-Commit prefixes; commit messages end with the `Co-Authored-By: Claude Opus 4.8 (1M context)` trailer. `_bmad-output/` is tracked; `fintin.toml` is gitignored (public repo — no real email/PII in tracked files).

### Testing Standards

- EDGAR-touching code tested against offline fixtures/constructed objects — **never live EDGAR** (NFR-7, ban risk). Here the standardizer IS offline, so `test_standardize.py` may call the real edgartools mapping directly (and a dedicated test proves it works with sockets blocked).
- Pure-core tests need no ClickHouse; repo/mart tests are `@pytest.mark.integration`.
- Assert AC-3 idempotency through a background merge (`OPTIMIZE TABLE canonical_fact FINAL`), matching the Tier 0 test.

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story-1.5]
- [Source: ARCHITECTURE-SPINE.md#AD-4, #AD-5, #AD-6, #AD-8, #AD-9, #AD-18, Consistency-Conventions]
- [Source: fintin/adapters/store/schema.py#canonical_fact (56-74), #resolved_fact_mv (90-101), #screening_mart (107-119)]
- [Source: fintin/core/ingest.py#RawFactRow (64), #content_hash (118); fintin/adapters/store/raw_fact_repo.py#next_ingest_version (42)]
- edgartools offline API: `edgar.xbrl.standardization.reverse_index.get_standard_concept`; version via `edgar.__version__`; data at `edgar/xbrl/standardization/gaap_mappings.json`.

## Dev Agent Record

### Agent Model Used

### Debug Log References

### Completion Notes List

### File List
