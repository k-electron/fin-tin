---
baseline_commit: 182f646251eba7d759f829dcc252d71f15c70782
---
# Story 2.2: DB-derived work list via membership over lookback

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the builder,
I want the outstanding ingestion work derived from the store's contents + EDGAR's filing index over a lookback window (no maintained cursor),
so that catch-up ingests exactly what's new or restated, restatements are caught, and nothing already present is re-fetched.

## Acceptance Criteria

1. **Given** the store and a resolved Universe **When** the work list is computed **Then** it = accessions in the EDGAR **index** over the lookback window, restricted to the Universe CIKs, **minus** accessions already present in the store (per-accession **membership** is the authority, AD-16) — and `SELECT max(filed_date)` is used only as a **scan-sizing hint** that sizes the window, never as the thing that decides done-ness (FR-9, FR-2 — discovery reads EDGAR's multi-filer index, not per-company pages).
2. **Given** an accession already present in the store **When** the work list is computed **Then** it is **not** in the list (not re-fetched).
3. **Given** a newly-filed accession (incl. a `10-K/A` amendment) restating an old period, whose `filing_date` falls in the lookback window **Then** it appears in the work list.
4. **Given** the discovery path touches EDGAR **Then** it goes through the single rate-limited `EdgarClient` (AD-3, FR-1) — one index fetch per calendar quarter the window spans (FR-2), never per-company crawling; and tests never hit live EDGAR (NFR-7).

## 🔑 Key design decisions (settled)

**1. Index-based discovery via `edgar.get_filings` (verified against installed edgartools 5.43.0).**
- `edgar.get_filings(filing_date="A:B", form=["10-K","10-Q"])` downloads SEC's **quarterly full-index** (`form.gz`) — the **multi-filer** index of every filer's filings for the quarter — and returns a `Filings` backed by a **pyarrow Table** (`filings.data`) with columns `form`, `company`, `cik` (**int32**), `filing_date` (**date32**), `accession_number` (**dashed 20-char**, e.g. `0000320193-24-000123`). One gzip request **per calendar quarter** the window spans (FR-2), regardless of day count. Filter to the ~500 Universe CIKs locally with `pyarrow.compute.is_in` on the int32 `cik` column.
- **This path hits the network and requires identity.** `get_filings` → `download_text` → `HTTP_MGR` (rate-limited) and is wrapped by `@with_identity` (raises `IdentityNotSetException` if unset). So — **unlike `universe` (Story 2.1, offline)** — the `work-list` command **constructs `EdgarClient`** (which sets identity + rate, AD-3) and runs the fetch through `EdgarClient.run(...)` (so the fair-access cool-down/retry covers it). It therefore **requires a real `[edgar]` contact email** (ban-safety gate), like `ingest-company`.
- `form=["10-K","10-Q"]` with `amendments=True` (the default) auto-includes `/A`; the form filter is client-side (doesn't reduce requests) but trims rows. Target **only the financial-statement forms** (`10-K`, `10-Q`, + `/A`) — matching the mart's periodic-form filter — so we don't propose non-financial accessions (e.g. Form 4) that never produce Tier 0 facts and would look permanently "missing."
- `get_filings` returns `None` on an invalid/out-of-range date, or a `Filings` wrapping an **empty table** (`len == 0`) for a valid period with no rows — guard for both. **No dedup**: a co-filed accession appears once per filer CIK, so dedup by accession ourselves.

**2. Membership is the authority; HWM is only a scan-sizing hint (AD-16).** Work list = candidate index accessions − accessions already in `raw_fact`. The scan window = `[ (HWM − LOOKBACK), today ]` where `HWM = SELECT max(filed_date) FROM raw_fact` (a **hint** that bounds the scan, never the done-ness test). `LOOKBACK` (config) spans the plausible filing-order skew so a filing filed slightly before the HWM but not yet committed (per-company commits can be out of order, AD-11) is still re-checked. On an **empty store** (HWM = None) the window is `[ today − LOOKBACK, today ]` — a bounded recent window; **full history from empty is Story 2.3's per-company backfill**, not this incremental reconciler (the `work-list` command notes this when the store is empty).

**3. No maintained cursor (AD-1, AD-10).** The work list is **derived** each run from `(EDGAR index ∩ Universe) − (store membership)`. Nothing persists a progress cursor/checkpoint. This is the FR-9/FR-10 reconciler that Epic 3 catch-up reuses and Story 2.3 backfill leans on for per-company resumability (Epic 2 refinement: "build the resumability mechanism once here").

**4. Discovery is per-accession; ingestion stays per-company (v1).** Story 1.4 ingests via `get_company_facts(cik)` (whole company). This story only **discovers** which accessions are new — the accession-level diff is the AD-16 correctness check. Wiring discovery → per-company re-ingest is Story 2.3; here the deliverable is the derived work list and a `work-list` command that **displays** it (a dry-run of catch-up; it does not ingest).

## Tasks / Subtasks

- [x] **Task 1 — `[reconcile]` config (LOOKBACK)** (AC: 1) — `fintin/config.py`
  - [x] Add a frozen `ReconcileConfig(lookback_days: int)` + `reconcile: ReconcileConfig | None = None` on `Config` (optional section, mirror `[edgar]`/`[universe]`; absent → a default `ReconcileConfig(lookback_days=DEFAULT_LOOKBACK_DAYS)` so the reconciler always has a value). `DEFAULT_LOOKBACK_DAYS = 7`.
  - [x] `_parse_reconcile`: `lookback_days` must be an `int >= 1` (reject `bool` before `int`, reject `< 1`), mirroring the existing type/range guards.
- [x] **Task 2 — Pure reconciler in core** (AC: 1, 2, 3) — `fintin/core/reconcile.py` (NEW, pure; no `edgar`, no ClickHouse, no pyarrow)
  - [x] `WorkItem(NamedTuple)`: `accession: str`, `cik: int`, `form: str`, `filed_date: date`. `WorkList(NamedTuple)`: `items: tuple[WorkItem, ...]`, `scanned: int` (distinct candidate accessions), `already_present: int`.
  - [x] `resolve_window(hwm: date | None, lookback_days: int, today: date) -> tuple[date, date]`: returns `(window_start, today)` where `window_start = (hwm or today) - timedelta(days=lookback_days)`. Pure.
  - [x] `compute_work_list(candidates: Iterable[WorkItem], present_accessions: Container[str]) -> WorkList`: dedup candidates by accession (first-wins), drop those whose accession is in `present_accessions` (membership, AD-16), return the survivors **sorted** by `(filed_date, accession)` for deterministic output, with `scanned`/`already_present` counts.
  - [x] Both are pure and unit-testable with plain data — no ports needed here (the adapter produces `candidates`, the store produces `present_accessions`).
- [x] **Task 3 — EDGAR filing-index discovery adapter** (AC: 1, 3, 4) — `fintin/adapters/edgar/filings_index.py` (NEW)
  - [x] `FINANCIAL_STATEMENT_FORMS = ("10-K", "10-Q")` (module constant; `/A` auto-included by `amendments=True`).
  - [x] `fetch_work_candidates(edgar_client, *, filing_date: str, ciks: Collection[int], forms=FINANCIAL_STATEMENT_FORMS) -> list[WorkItem]`: calls `edgar.get_filings(filing_date=filing_date, form=list(forms))` **through `edgar_client.run(...)`** (AD-3 cool-down/retry); handles `None`/empty (`len == 0`) → `[]`; delegates row→WorkItem conversion + CIK filtering to `_filings_to_work_items`.
  - [x] `_filings_to_work_items(table, ciks) -> list[WorkItem]`: pyarrow filter — `pc.is_in(table["cik"], value_set=pa.array(sorted(ciks), type=table.schema.field("cik").type))`, then read `accession_number`/`cik`/`form`/`filing_date` columns in bulk (`.to_pylist()`) and build `WorkItem`s (normalize each accession via `core.ingest.normalize_accession` — index is already dashed, so this is a safe validation). This helper is the real logic and is **offline-testable** with a synthetic `pa.Table`.
  - [x] Only `edgar`/`pyarrow` imports live here (`adapters/edgar/`); imports `WorkItem` from `core.reconcile` (adapter → core, correct direction).
- [x] **Task 4 — Store membership + HWM queries** (AC: 1, 2) — `fintin/adapters/store/raw_fact_repo.py` (MOD)
  - [x] `high_water_mark(client) -> date | None`: `SELECT max(filed_date) FROM raw_fact` → a `date` or `None` (empty store). The scan-sizing hint (AD-16), never the done-ness test.
  - [x] `present_accessions(client, *, ciks: Collection[int], since: date) -> set[str]`: `SELECT DISTINCT accession FROM raw_fact WHERE cik IN %(ciks)s AND filed_date >= %(since)s` (parameterized; never string-interpolated), returning the set of present accessions in the scan window. `FINAL` is unnecessary (membership is existence, and dedup rows share the same accession). Empty `ciks` → return `set()` without a query.
- [x] **Task 5 — `work-list` CLI command** (AC: 1, 2, 3, 4) — `fintin/cli/app.py`
  - [x] New `work-list` command: load config (→ exit 2 on `ConfigError`); require a non-empty `[universe]` (else clean error, exit 2); construct `EdgarClient` (→ exit 2 on `EdgarConfigError`, so a blank/placeholder email fails loudly before any request); resolve the Universe (Story 2.1 `resolve_universe`; if it resolves to zero CIKs, error + exit 1 like `universe`); `check_connection` (→ exit 1); query HWM + present accessions; `resolve_window`; `fetch_work_candidates` (through the client); `compute_work_list`; render.
  - [x] Render: the window used, `N` outstanding filings across `M` companies (and note when the store is empty that this is only the recent lookback window — run backfill for full history); with `--show-items`, print each `accession  cik  form  filed_date`. Wrap the fetch/compute in `try/except` for `EdgarThrottleError` (→ exit 1, "EDGAR throttled") and generic `Exception` (→ clean "Work-list failed: …" + exit 1) — never a traceback.
  - [x] Lazy-import `edgar`-touching modules inside the command body (keep `--help`/config-error paths fast), as `ingest-company` does.
- [x] **Task 6 — Tests (never live EDGAR; NFR-7)** — `tests/`
  - [x] `tests/test_reconcile.py` (NEW, pure): `resolve_window` (HWM present → `hwm - lookback`; HWM None → `today - lookback`; window_end == today); `compute_work_list` (present accession excluded — AC-2; a new/`/A` accession included — AC-3; dedup of co-filed duplicate accessions; sorted output; scanned/already_present counts; empty candidates). AST guard: `core/reconcile.py` imports no `edgar`/`clickhouse`/`pyarrow`.
  - [x] `tests/test_filings_index.py` (NEW, offline): `_filings_to_work_items` with a **synthetic `pa.Table`** (build columns `form`/`company`/`cik`(int32)/`filing_date`(date32)/`accession_number`) — asserts CIK filtering (only Universe CIKs kept), WorkItem fields, `/A` rows kept, dashed accession preserved. `fetch_work_candidates` with a **fake `edgar_client`** (`.run(op)` calls `op()`) and `edgar.get_filings` monkeypatched to return a fake `Filings` (object with `.data` = synthetic table, `__len__`) — and the `None`/empty-table → `[]` guards. **No network.**
  - [x] `tests/test_raw_fact_repo.py` (MOD, integration/throwaway-DB): `high_water_mark` (None on empty; max filed_date otherwise); `present_accessions` (returns present accessions for the CIKs since the window; excludes out-of-window/other-CIK; empty ciks → `set()` no query). Use the existing throwaway-DB pattern.
  - [x] `tests/test_config.py` (MOD): `[reconcile]` parse — valid `lookback_days`; absent section → default; `lookback_days` non-int/`bool`/`< 1` → `ConfigError`.
  - [x] `tests/test_cli.py` (MOD): `work-list` help lists it; missing config → exit 2; missing `[universe]` → exit 2; missing `[edgar]`/placeholder email → `EdgarConfigError` exit 2 (offline, before any request); all no-traceback. (Happy path is covered by the core/adapter/store tests — the network fetch is not CLI-tested, per NFR-7, matching `ingest-company`.)
- [x] **Task 7 — Validate & document**
  - [x] `uv run pytest` green (unit + integration with ClickHouse up).
  - [x] README: document `[reconcile].lookback_days` in Configuration, and a short "Preview outstanding work" note (`fintin work-list` — hits EDGAR's index over the lookback window, so it needs a real contact email; shows what catch-up would fetch; ingestion arrives with backfill/catch-up).

## Dev Notes

### Current substrate (Epic 1 + Story 2.1, on main)

- **EdgarClient** (`fintin/adapters/edgar/client.py`): construct from `Config`; sets `edgar.set_identity(...)` + rate; `client.run(op, description=...)` wraps an EDGAR call in the fair-access cool-down/retry (catches `TooManyRequestsError`, raises `EdgarThrottleError` after retries). The ban-safety gate rejects a blank/placeholder email at construction. Reuse it — `get_filings` needs the same identity + rate it configures.
- **facts.py** (`fintin/adapters/edgar/facts.py`): the pattern to mirror — `client.run(lambda: edgar.get_company_facts(cik), description=...)`. `fetch_work_candidates` is the same shape over `edgar.get_filings`.
- **ingest core** (`fintin/core/ingest.py`): `normalize_accession(accn)` (dashed 20-char) + `_ACCESSION_RE` (`^\d{10}-\d{2}-\d{6}$`) — reuse `normalize_accession` in the adapter. `RawFactRow` carries `accession`, `cik`, `filed_date`, `form`.
- **raw_fact_repo** (`fintin/adapters/store/raw_fact_repo.py`): parameterized reads (`%(cik)s`), `client.query(...).result_rows`. Add the HWM + membership queries here.
- **Universe** (`fintin/core/universe.py` + `fintin/adapters/edgar/universe.py`, Story 2.1): `resolve_universe(cfg.universe, resolve_tickers=resolve_tickers) -> ResolvedUniverse(.ciks, .gaps, …)`. `work-list` resolves the Universe to get its CIKs; an empty resolved Universe → error + exit 1 (as in `universe`).
- **CLI patterns** (`fintin/cli/app.py`): `_configure_logging()`, lazy `edgar` imports, `ConfigError` → exit 2, `EdgarConfigError` → exit 2, connection/op failures → exit 1, `typer.secho(fg=RED, err=True)`, never a traceback. `ingest-company` is the closest shape (constructs EdgarClient + connects to ClickHouse).

### edgartools 5.43.0 — verified filing-index facts (do not re-investigate)

- `edgar.get_filings(filing_date="A:B", form=["10-K","10-Q"])` → multi-filer quarterly full-index; `Filings.data` is a `pyarrow.Table`, cols `form`(str), `company`(str), `cik`(int32), `filing_date`(date32), `accession_number`(dashed 20-char str). One gzip request per calendar quarter the window spans.
- Network via `HTTP_MGR` (rate-limited); `@with_identity` raises `IdentityNotSetException` if identity unset — so route through `EdgarClient` (which set_identity's on construction).
- `amendments=True` (default) → `/A` rows included; form filter is client-side. Returns `None` on invalid/out-of-range date; empty `Filings` (`len==0`) on a valid empty period. No dedup — dedup by accession ourselves. Bulk column read: `table["accession_number"].to_pylist()`, etc. CIK filter: `pc.is_in(table["cik"], pa.array(ciks, int32))`.
- ⚠️ **Public repo:** never write a real contact email into a tracked file (story, code, tests, or `fintin.toml.example`). Use `you@example.com` in any example; identity comes from the operator's gitignored `fintin.toml`.

### Architecture constraints (authoritative)

- **AD-16** — membership is the correctness authority over a reordering-safe LOOKBACK window; `max(filed)` is only a scan-bounding hint. [ARCHITECTURE-SPINE.md#AD-16]
- **AD-10** — work = newly-filed accessions via EDGAR's index (`get_filings(filing_date=…)`) minus those present, over a window; no stored cursor; empty delta = no-op. [#AD-10]
- **AD-1** — derive state, never persist a driftable cursor/checkpoint. [#AD-1]
- **AD-3 / FR-1** — all EDGAR access through the one rate-limited client; the index fetch runs through `EdgarClient.run`. [#AD-3]
- **AD-2** — pure engine, dumb trigger: the reconciler is pure `core`; `work-list` is a dumb CLI trigger. [#AD-2]
- **FR-2** — minimize requests: index-based discovery (one fetch/quarter), not per-company crawling. [epics.md#FR-2]
- **AD-11 / FR-10** — per-company idempotent commits + membership-derived resume; this story builds the membership/work-list mechanism the backfill (2.3) and catch-up (Epic 3) reuse (Epic 2 refinement). [#AD-11]
- **Testing / NFR-7** — EDGAR-touching code tested against fixtures/injection; never live EDGAR. [#Testing]

### Project Structure Notes

- New: `fintin/core/reconcile.py` (pure), `fintin/adapters/edgar/filings_index.py`. Modified: `fintin/config.py`, `fintin/adapters/store/raw_fact_repo.py`, `fintin/cli/app.py`, `README.md`. New tests: `tests/test_reconcile.py`, `tests/test_filings_index.py`; extended: `tests/test_raw_fact_repo.py`, `tests/test_config.py`, `tests/test_cli.py`.
- No schema/DDL change (reads only `raw_fact`; `adapters/store` owns DDL but this adds no tables — AD-18 untouched). No new dependency (`pyarrow` ships with edgartools).
- `work-list` does NOT ingest and writes nothing — it's a read-only dry-run of catch-up.

### Previous Story Intelligence (Epic 1 + 2.1)

- **Injected-port purity** (`ingest_company`, `resolve_universe`): keep `core` edgar-free; the adapter produces `candidates`, the store produces `present_accessions`, core just diffs. AST import-guard test locks purity (reuse the `tests/test_universe.py` pattern; here also assert no `pyarrow`/`clickhouse`).
- **Offline adapter testing** (Story 2.1): test the real logic (`_filings_to_work_items`) with synthetic data; for the network wrapper, inject a fake client + monkeypatch the edgar call — never hit the network.
- **`bool` is an `int` subclass** — reject it before the int/range check in `_parse_reconcile`.
- **Determinism** (kboss): dedup + sort the work list; `resolve_window` is a pure function of (hwm, lookback, today).
- **Throwaway-DB integration pattern** (`tests/test_schema.py` / `test_raw_fact_repo.py`): unique DB per test, auto-skip when ClickHouse is down.
- **CLI error-path testing** (Story 2.1): the network happy-path isn't CLI-tested; error paths (config/edgar) are, asserting exit codes + no `Traceback`.

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story-2.2, #Epic-2 refinement, #FR-2/FR-9/FR-10]
- [Source: ARCHITECTURE-SPINE.md#AD-16, #AD-10, #AD-1, #AD-3, #AD-2, #AD-11]
- [Source: fintin/adapters/edgar/client.py#EdgarClient.run — the throttle wrapper], [fintin/adapters/edgar/facts.py — client.run call shape]
- [Source: fintin/core/ingest.py#normalize_accession, #RawFactRow], [fintin/adapters/store/raw_fact_repo.py — parameterized query pattern]
- [Source: fintin/core/universe.py#resolve_universe — Universe → CIKs (Story 2.1)]
- [Source: edgartools 5.43.0 installed — edgar/_filings.py:1240 get_filings, :313/:485 index schema, :445-524 multi-filer fetch; edgar/httprequests.py:618-635 @with_identity; edgar/filtering.py:94-103 filter_by_cik]

## Review Findings

_(to be filled by code-review)_

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Opus 4.8, 1M context)

### Debug Log References

- Full suite green: `uv run pytest -q` → **207 passed** (was 175; +32). Integration ran (ClickHouse up); no regressions.
- Verified clickhouse-connect list-parameter binding: `WHERE cik IN %(ciks)s` with a list renders a proper IN list; `filed_date >= %(since)s` binds a Python `date`. ClickHouse `max()` on an empty table returns `1970-01-01` (not NULL) → `high_water_mark` guards with `count()`.
- **Live end-to-end smoke** (default DB, 49,704 rows, HWM `2026-05-01`, 71 Apple accessions): `fintin work-list` (Universe `["AAPL"]`, 7-day lookback → window `2026-04-24..2026-07-24`, 2 quarterly index fetches through the rate-limited client) → `1 scanned, 1 already present, 0 outstanding`. Confirms the real `get_filings` output matches the adapter (`.data` pyarrow table, columns, `len`), the CIK filter, and the AD-16 membership diff (Apple's `2026-05-01` filing is present → correctly excluded). Exit 0. Temp config (real email, scratchpad-only, untracked) removed after.

### Completion Notes List

- **AC-1/AC-4 (index discovery, one fetch/quarter, through the client).** `fetch_work_candidates` runs `edgar.get_filings(filing_date="A:B", form=["10-K","10-Q"])` through `EdgarClient.run` (AD-3 cool-down/retry); `_filings_to_work_items` filters the pyarrow table to Universe CIKs (`pc.is_in`, int32) and builds `WorkItem`s. Discovery is the multi-filer quarterly index (FR-2), not per-company crawling. Requires a real `[edgar]` email (EdgarClient gate).
- **AC-1/AC-2 (membership authority).** `compute_work_list` diffs candidates against `present_accessions(raw_fact)`; an already-present accession is excluded (not re-fetched). `high_water_mark` is the scan-sizing hint only. Verified live (Apple's present filing excluded).
- **AC-3 (restatements).** `10-K/A` amendments are distinct index rows with their own accession + filing_date; `_filings_to_work_items` keeps them and `compute_work_list` includes any not yet present (`test_new_and_amendment_accessions_included`, `test_work_item_fields_and_amendment_kept`).
- **AD-1/AD-10 (no cursor).** The work list is derived each run from `(index ∩ Universe) − store membership`; nothing persisted. Pure `core/reconcile.py` (AST guard: no `edgar`/`clickhouse`/`pyarrow`).
- **NFR-7.** All tests offline: pure reconciler with plain data; adapter with a synthetic pyarrow table + fake client + monkeypatched `get_filings`; membership via the throwaway-DB integration pattern; CLI error-paths only (config/edgar) before any request. The live smoke is a manual dev-check, not a test.
- **Config.** `[reconcile].lookback_days` (default 7, `int >= 1`, `bool`-rejected). `Config.reconcile` is always populated (default when the section is absent) so the reconciler never sees `None`.
- **`work-list` CLI.** Read-only dry-run; constructs EdgarClient (real email), resolves the Universe (empty → exit 1), queries HWM + membership, fetches the index over the window, renders the outstanding count (+ empty-store note; `--show-items`). Error paths render cleanly (config → exit 2, edgar → exit 2, throttle/failure → exit 1), never a traceback.

### File List

- `fintin/config.py` (MOD) — `ReconcileConfig` + `DEFAULT_LOOKBACK_DAYS` + `_parse_reconcile`; `Config.reconcile` (always populated); wired into `load_config`.
- `fintin/core/reconcile.py` (NEW) — pure `WorkItem`/`WorkList`, `resolve_window`, `compute_work_list` (AD-16 diff).
- `fintin/adapters/edgar/filings_index.py` (NEW) — `fetch_work_candidates` (through `EdgarClient.run`) + `_filings_to_work_items` (pyarrow CIK filter); `FINANCIAL_STATEMENT_FORMS`.
- `fintin/adapters/store/raw_fact_repo.py` (MOD) — `high_water_mark` (count-guarded), `present_accessions` (parameterized cik-IN + filed_date window).
- `fintin/cli/app.py` (MOD) — `work-list` command (`--show-items`; offline-until-fetch error paths; empty-Universe exit 1).
- `fintin.toml.example` (MOD) — `[reconcile].lookback_days` block.
- `README.md` (MOD) — `[reconcile]` in Configuration + a "Preview outstanding work" section.
- `tests/test_reconcile.py` (NEW) — window + diff + dedup + sort + purity guard.
- `tests/test_filings_index.py` (NEW) — synthetic-table CIK filter + fake-client/monkeypatched fetch (None/empty guards).
- `tests/test_raw_fact_repo.py` (MOD) — HWM (None on empty; latest otherwise) + `present_accessions` (cik/window scoping; empty ciks → set()).
- `tests/test_config.py` (MOD) — `[reconcile]` parse + rejection cases.
- `tests/test_cli.py` (MOD) — `work-list` help + config/universe/edgar error paths.

## Change Log

- 2026-07-24 — Story 2.2 implemented: DB-derived work list via membership over a lookback window. Index-based discovery through `edgar.get_filings` (multi-filer quarterly index, one request/quarter, via the rate-limited `EdgarClient`); pure core reconciler (`resolve_window` + `compute_work_list`) diffing index candidates against `raw_fact` membership (AD-16 authority; HWM only a scan-sizing hint); store `high_water_mark` + `present_accessions` queries; `[reconcile].lookback_days` config; a read-only `work-list` CLI dry-run (`--show-items`). No maintained cursor (AD-1). Restatements/amendments caught by scoping on filing_date. 207 tests pass (+32); verified live on Apple (0 outstanding, its filing correctly seen as present). Status → review.
