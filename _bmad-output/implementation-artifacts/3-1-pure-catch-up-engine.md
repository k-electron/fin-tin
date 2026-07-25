---
baseline_commit: b9ad252310e74283c17f6350e0552726ec56a285
---

# Story 3.1: Pure catch-up engine + CLI trigger

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the builder,
I want a single `fintin catch-up` command that brings the store current,
so that I can catch up to today before screening — any time, safely, and idempotently.

## Acceptance Criteria

1. **Given** the store at some high-water mark **When** I run `fintin catch-up` **Then** everything filed since is ingested **via the Epic 2 work-list mechanism, reused not re-implemented** (`resolve_window` + `fetch_work_candidates` + `present_accessions` + `compute_work_list` for discovery; `backfill_universe`/`ingest_company` for the per-company ingest), **And** the run reports `STARTED`→`COMPLETED` (FR-8).
2. **Given** the catch-up engine **Then** it is a **pure `core` command with no knowledge of its caller** — the CLI is a dumb trigger; the engine imports no `edgar`/ClickHouse/`pyarrow` (AST-guarded) and receives the already-computed `WorkList` + injected fetch strategy + insert callable (FR-12, AD-2). Throttle handling lives in the engine (injected `fatal_errors`); **single-flight/lease is Story 3.2, out of scope here.**
3. **Given** nothing new filed **When** catch-up runs **Then** it ingests nothing and returns **`NOTHING_TO_DO`** — no per-company `companyfacts` requests, exit 0.
4. **Given** the run's status vocabulary **Then** `STARTED` / `NOTHING_TO_DO` / `COMPLETED` are **all exit-0** (success outcomes a cron/trigger must not log-as-error). A per-company fetch failure is a **recorded explained gap**, not fatal, and the run still reaches `COMPLETED` (SM-2). Only an EDGAR **throttle** exhaustion (ban-safety, SM-C1) or a **systemic** failure (store dropped mid-run) aborts with a loud **exit 1** — those are *not* in the success vocabulary.
5. **Given** the offline test suite **Then** it covers the pure catch-up engine (fakes — `NOTHING_TO_DO` / `STARTED`→`COMPLETED` / affected-CIK derivation / failure-recorded / throttle-abort / systemic-abort / observer-robustness / purity guard) and the CLI error + ban-safety wiring paths, with **zero live EDGAR** (NFR-7). The catch-up *happy path* hits EDGAR (index **and** `companyfacts`), so — like `backfill`/`work-list` — it is exercised offline through the engine + adapter unit tests, never live; only the `NOTHING_TO_DO` branch is CLI-drivable offline (discovery stubbed).

## 🔑 Key design decisions (settled)

1. **Catch-up is a thin composition of primitives that already exist — it re-implements nothing (the epic guardrail).** Epic 3's refinement note is explicit: it *"Reuses (never re-implements) Epic 2's resumability / work-list mechanism"* and *"a story-review guardrail prevents a second implementation."* So:
   - **Discovery** = the exact `work-list` command pipeline: `high_water_mark` → `resolve_window(hwm, lookback, today)` → `fetch_work_candidates(edgar_client, filing_date=…, ciks=resolved.ciks)` → `present_accessions(client, accessions=…)` → `compute_work_list(candidates, present)`. Unchanged, reused verbatim at the CLI seam.
   - **Ingest** = `backfill_universe(affected_ciks, …, already_present=frozenset())` — the Story 2.3 per-company engine, scoped to the companies the work list touches, with **nothing skipped** (the whole point is to re-ingest a company that has a *new* filing).
   - **New `core/catchup.py` adds ONLY**: (a) derive the affected CIK set from the `WorkList`, (b) the `STARTED`/`NOTHING_TO_DO`/`COMPLETED` status vocabulary, (c) wrap the `BackfillReport` in a `CatchUpReport`. That composition *is* catch-up's semantics and belongs in pure `core` (AD-2), not the CLI.
2. **Per-accession work list → per-company `companyfacts` ingest (the v1 mechanism, AD-13).** The work list is *accessions*; the only v1 fetch strategy is per-company `companyfacts` (bulk `.zip` deferred, AD-13). So catch-up derives the **distinct CIKs** the outstanding accessions belong to and re-fetches each affected company's full `companyfacts` — which now includes the newly-filed accession. The new filing's facts land; a restated period's facts land with a **higher ingest-monotonic `version`** and supersede on read (AD-6). This is *exactly* how catch-up absorbs restatements: a restating amendment is a new accession → its company appears in the work list → re-ingest `companyfacts` → newer values win in the mart (latest-filed-wins, AD-7). Re-ingesting a company's already-present history alongside the new filing is a **read no-op** (idempotent, AD-6). *(A narrower per-accession fetch is a future optimization tied to a per-accession strategy — deferred; the edgartools path we use is `companyfacts`.)*
3. **Status vocabulary — all exit-0; this story ships `STARTED` / `NOTHING_TO_DO` / `COMPLETED`.** From the architecture brief's "success-not-failure status vocabulary, all exit-0" (critical for cron: an overlapping/empty trigger must not log-as-error). The engine owns the vocabulary (single source of truth):
   - **empty work list** → `NOTHING_TO_DO` (terminal, exit 0); no `STARTED`, **no `companyfacts` request**.
   - **non-empty** → emit `STARTED`, ingest the affected companies, emit `COMPLETED` (terminal, exit 0).
   - `ALREADY_RUNNING` (the single-flight self-expiring lease, AD-12) is **Story 3.2** — this engine has no lease; 3.2 wraps it. Model the vocabulary as an `enum.Enum` (`CatchUpStatus`) so 3.2 extends it by one member.
   - `STARTED` is a **transient lifecycle emission** (via an injected `on_status` observer, so `core` does no I/O — mirrors backfill's `on_company`); the terminal `CatchUpReport.status` is `NOTHING_TO_DO` | `COMPLETED`.
4. **Pure engine, dumb trigger (AD-2, FR-12).** `fintin/core/catchup.py` imports only stdlib + `fintin.core.*` (`reconcile.WorkList`, `backfill.backfill_universe`/`BackfillReport`/`BackfillStrategy`/`BackfillEvent`/`BackfillFailure`, `ingest.RawFactRow`) — **no `edgar`, no ClickHouse, no `pyarrow`** (AST-guarded, like `core/backfill.py`/`core/coverage.py`). The `catch-up` CLI does the discovery I/O (index fetch + membership query), builds the `CompanyFactsStrategy`, and renders. "Throttle + single-flight live in the engine" (epic AC): throttle-abort is the engine's injected `fatal_errors` (reused from `backfill_universe`); the lease is 3.2.
5. **Throttle / systemic abort is exit 1 — NOT a success outcome.** A throttle exhaustion means the run did **not** catch up (ban-safety stopped it, SM-C1); a `BackfillAborted` means a systemic failure (e.g. the store dropped) — neither is `STARTED/NOTHING_TO_DO/COMPLETED`, so both propagate out of the engine and the CLI exits **1** (consistent with `backfill`/`work-list`). `STARTED` may already have been emitted (the run *did* start) — the CLI logs the abort honestly. The exit-0 vocabulary is for the *normal* outcomes only.
6. **Not offline — discovery hits EDGAR's index; ingest hits `companyfacts`.** Like `work-list` + `backfill`, catch-up builds the **one** rate-limited `EdgarClient` **once** (its gate rejects a blank/placeholder contact email before any request; ban-safety, FR-1) and reuses it for both the index scan and the per-company ingest. (Contrast `status`/`map-canonical`/`universe`, which are offline.)
7. **No new DDL, no table, no config, no cursor (AD-1/AD-18).** Catch-up derives its work every run from the DB high-water-mark hint + the EDGAR index minus store membership (AD-1/AD-16) — no checkpoint, no "last run" marker. The `STARTED`/`COMPLETED` vocabulary is **emitted/returned, never persisted** (no `ingest_run`/`status` table — AD-1 forbids the ledger). `schema.py` untouched (AD-18); `fintin.toml.example` unchanged (reuses `[reconcile].lookback_days`).
8. **One ingest-monotonic `version` base per run (AD-6).** `next_ingest_version(client)` once per run; `backfill_universe` offsets it per affected company (its existing per-company offset — deterministic cross-company tie-break). A later catch-up derives a strictly greater base, so supersession holds across runs (AD-6).

## Tasks / Subtasks

- [x] **Task 1 — Pure catch-up engine** (AC: 1, 2, 3, 4) — `fintin/core/catchup.py` (NEW, pure)
  - [x] Define `class CatchUpStatus(enum.Enum)` with members `STARTED = "STARTED"`, `NOTHING_TO_DO = "NOTHING_TO_DO"`, `COMPLETED = "COMPLETED"` (plain `Enum`, not `str`-mixed — avoids the f-string rendering footgun; render via `.value`). Docstring: `ALREADY_RUNNING` is added in Story 3.2 (the lease).
  - [x] Define `CatchUpReport(NamedTuple)`: `status: CatchUpStatus` (terminal: `NOTHING_TO_DO` | `COMPLETED`), `scanned: int`, `outstanding: int` (= `len(work.items)`), `companies: int` (affected CIK count), `backfill: BackfillReport | None` (`None` for `NOTHING_TO_DO`). None-safe convenience `@property`: `companies_ingested`, `companies_failed`, `rows_landed` (→ `0` when `backfill is None`), `failures` (→ `()` when `None`).
  - [x] Implement `catch_up(work: WorkList, *, strategy: BackfillStrategy, insert_rows, taxonomy_version: str, version: int, fatal_errors=(), max_consecutive_failures=None, on_company=None, on_status=None) -> CatchUpReport`:
    - `affected = tuple(sorted({item.cik for item in work.items}))` — derive distinct affected companies (sorted, deterministic — kboss).
    - If `not affected`: `_emit_status(on_status, NOTHING_TO_DO)`; return `CatchUpReport(NOTHING_TO_DO, scanned=work.scanned, outstanding=len(work.items), companies=0, backfill=None)`. **No strategy call, no insert.**
    - Else: `_emit_status(on_status, STARTED)`; `report = backfill_universe(affected, strategy=strategy, insert_rows=insert_rows, taxonomy_version=taxonomy_version, version=version, already_present=frozenset(), fatal_errors=fatal_errors, max_consecutive_failures=max_consecutive_failures, on_company=on_company)`; `_emit_status(on_status, COMPLETED)`; return `CatchUpReport(COMPLETED, scanned=work.scanned, outstanding=len(work.items), companies=len(affected), backfill=report)`.
    - `_emit_status(on_status, status)` helper mirrors backfill's `_emit`: `try: on_status(status) except Exception: pass` (a passive observer must never sink a run whose companies are already committed; `COMPLETED`-emission errors especially must not lose the committed work).
    - If `backfill_universe` raises (a `fatal_errors` type or `BackfillAborted`), it propagates out of `catch_up` — `COMPLETED` is never emitted, no terminal `CatchUpReport` is returned; the CLI catches and exits 1 (AC-4, ban-safety).
  - [x] Import only: `enum`, `NamedTuple`, `Callable`/`Sequence`; `from fintin.core.reconcile import WorkList`; `from fintin.core.backfill import BackfillEvent, BackfillFailure, BackfillReport, BackfillStrategy, backfill_universe`; `from fintin.core.ingest import RawFactRow` (for the insert type). **No `edgar`/ClickHouse/`pyarrow`.**
- [x] **Task 2 — `catch-up` CLI trigger** (AC: 1, 3, 4, 6) — `fintin/cli/app.py` (MOD)
  - [x] Add `@app.command("catch-up")` with `--config/-c` and `--show-gaps` (enumerate each recorded per-company gap; default shows counts). Update the `_root()` docstring (drop the stale "catch-up … arrive in later stories" — it exists now).
  - [x] `_configure_logging()`; **deferred imports** (heavy `edgar`): `date`; `EdgarClient`, `EdgarConfigError`, `EdgarThrottleError`; `CompanyFactsStrategy`; `edgartools_version`; `fetch_work_candidates`; `resolve_tickers`; `high_water_mark`, `insert_raw_facts`, `next_ingest_version`, `present_accessions`; `compute_work_list`, `resolve_window`; `resolve_universe`; `catch_up`, `CatchUpStatus`; `BackfillAborted`.
  - [x] `load_config` → `ConfigError` **exit 2**. `cfg.universe is None` → **exit 2** (clean "no [universe]" message).
  - [x] `EdgarClient(cfg)` → `EdgarConfigError` **exit 2** (the gate rejects a blank/placeholder email **before any request**; ban-safety). Build it **once**, reuse for discovery + ingest.
  - [x] Wrap `resolve_universe(cfg.universe, resolve_tickers=resolve_tickers)` in `try/except Exception` → "Universe resolution failed" **exit 1** (degraded edgartools install must not traceback — Story 2.3 P1). Empty `resolved.ciks` → **exit 1** (hard misconfiguration for a catch-up scope).
  - [x] `check_connection(cfg.clickhouse)` → `StoreConnectionError` **exit 1**.
  - [x] `client = get_client(...)` in `try/finally` (close via `contextlib.suppress`). Inside: `hwm = high_water_mark(client)`; `window_start, window_end = resolve_window(hwm, cfg.reconcile.lookback_days, date.today())`; `candidates = fetch_work_candidates(edgar_client, filing_date=f"{start}:{end}", ciks=resolved.ciks)`; `present = present_accessions(client, accessions={c.accession for c in candidates})`; `work = compute_work_list(candidates, present)`; `version = next_ingest_version(client)`; then `report = catch_up(work, strategy=CompanyFactsStrategy(edgar_client), insert_rows=lambda rows: insert_raw_facts(client, rows), taxonomy_version=edgartools_version(), version=version, fatal_errors=(EdgarThrottleError,), max_consecutive_failures=10, on_company=_log_company, on_status=_log_status)`.
  - [x] `except EdgarThrottleError` → "EDGAR throttled, catch-up aborted" **exit 1**; `except BackfillAborted` → `str(exc)` **exit 1**; generic `except Exception` → "Catch-up failed" **exit 1**. Never a traceback.
  - [x] `_log_company(event)` reuses backfill's per-company `logger.info("[%d/%d] CIK %s %s", …)`. `_log_status(status)` → `logger.info("catch-up: %s", status.value)`.
  - [x] Render the terminal status (GREEN success): `NOTHING_TO_DO` → `"Nothing to do — the store is already current over the {lookback}-day lookback (NOTHING_TO_DO). [{scanned} scanned]."` **exit 0** (no `STARTED` — an empty work list never starts an ingest run). `COMPLETED` → `"Catch-up complete (STARTED→COMPLETED): {companies_ingested} company(ies) ingested ({rows_landed} facts landed) from {outstanding} outstanding filing(s), {companies_failed} failed, into database '{db}'."` **exit 0**. If `report.failures`: YELLOW `"{companies_failed} company(ies) recorded as explained gaps."` + `--show-gaps` → `  - CIK {gap.cik}: {gap.reason}`. Pluralization consistent with `backfill`.
- [x] **Task 3 — Tests (offline; NFR-7)** (AC: 5)
  - [x] `tests/test_catchup.py` (NEW, pure) — reuse `test_backfill.py`'s `_Fact`/`_facts`/`_FakeStrategy`/`_capturing_insert`/`_Throttle` patterns and build `WorkList`/`WorkItem` inputs directly:
    - **`NOTHING_TO_DO`**: empty `WorkList` → status `NOTHING_TO_DO`, `backfill is None`, `rows_landed == 0`, `failures == ()`, `strat.calls == []` (no fetch), `batches == []` (no insert), `on_status` == `[NOTHING_TO_DO]` (no `STARTED`).
    - **`STARTED`→`COMPLETED`**: `WorkList` with items for CIKs {1,2} → status `COMPLETED`, `companies == 2`, `strat.calls == [1, 2]`, `on_status` == `[STARTED, COMPLETED]`, `rows_landed` matches.
    - **affected-CIK derivation (dedup + sort)**: multiple work items across CIKs `[9, 5, 5, 2]` → affected `[2, 5, 9]`, one ingest per distinct CIK, sorted.
    - **present company IS re-ingested (no `already_present` skip)**: a work item for a CIK is fetched/ingested even though it "exists" (catch-up passes `already_present=frozenset()`) — assert the CIK is in `strat.calls`. This is the restatement/new-filing path (distinct from backfill's skip).
    - **version base + per-company offset** (delegates to `backfill_universe`): affected `[2, 5]`, `version=100` → stamped versions `{2: 100, 5: 101}`.
    - **failure recorded, run still `COMPLETED`** (SM-2): one affected CIK fails → `report.status == COMPLETED`, `companies_failed == 1`, `failures[0].cik` correct, the other CIK still ingested.
    - **throttle propagates, `COMPLETED` not emitted** (AC-4): `fatal_errors=(_Throttle,)`, one affected CIK raises `_Throttle` → `pytest.raises(_Throttle)`, and `on_status` captured == `[STARTED]` only (no `COMPLETED`).
    - **systemic abort propagates** (AC-4): `max_consecutive_failures=2`, ≥2 affected CIKs all fail → `pytest.raises(BackfillAborted)`.
    - **`on_status` observer exception does not sink the run**: an `on_status` that raises still yields a committed `COMPLETED` report.
    - **`CatchUpReport` props None-safe**: a `NOTHING_TO_DO` report → `companies_ingested/companies_failed/rows_landed == 0`, `failures == ()`.
    - **AST purity guard**: `core/catchup.py` imports no `edgar`/`clickhouse`/`pyarrow` (reuse the `_module_imports` helper).
  - [x] `tests/test_cli.py` (MOD) — catch-up error + ban-safety wiring (mirror the `backfill` block; reuse `_CH_ONLY`/`_EDGAR_PLACEHOLDER`/`_EDGAR_VALID`/`_stub_store`/`_raise`):
    - help lists `catch-up`; missing config → 2; missing `[universe]` → 2; missing `[edgar]` → 2 (EdgarClient gate, offline/ban-safe); placeholder email → 2; empty universe → 1. Each asserts no `Traceback`.
    - **`NOTHING_TO_DO` offline** (the one CLI-drivable happy branch): `_stub_store` + monkeypatch `fintin.adapters.edgar.filings_index.fetch_work_candidates` → `[]` so discovery yields an empty work list (no live index request); real `catch_up` returns `NOTHING_TO_DO` → exit 0, "Nothing to do" in output, no `Traceback`.
    - **throttle → exit 1**: `_stub_store` + stub `fetch_work_candidates` → `[]` + monkeypatch `fintin.core.catchup.catch_up` → `_raise(EdgarThrottleError(...))` → exit 1, "throttled" in output.
    - **systemic abort → exit 1**: same, `catch_up` → `_raise(BackfillAborted(...))` → exit 1, "consecutive failures" in output.
- [x] **Task 4 — Validate & document** (AC: all)
  - [x] `uv run pytest` — full suite green; record count + delta.
  - [x] `README.md`: add a **"### Catch up to today"** section (after "Backfill the Universe", before "Check coverage & status"): `fintin catch-up` + `--show-gaps`; reuses the work-list mechanism (index over the lookback window − store membership) then re-ingests the affected companies' `companyfacts`; STARTED→COMPLETED / NOTHING_TO_DO (all exit-0); hits EDGAR (needs a real contact email); restatements absorbed via latest-filed-wins; throttle/systemic abort → exit 1; single-flight lease is a later story. Update the `work-list` section's "catch-up lands in a later stage" forward-reference (it exists now).
  - [x] `fintin.toml.example` needs **no** change (reuses `[reconcile].lookback_days`).
  - [x] Append 3.1 deferred items to `deferred-work.md`: (a) per-company `companyfacts` re-fetch vs a narrower per-accession fetch (v1 uses companyfacts; AD-13); (b) EDGAR index vs `companyfacts` propagation lag — an accession in the index but not yet in `companyfacts` re-appears next run (self-healing); (c) no single-flight lease yet (Story 3.2) — a concurrent catch-up could double the EDGAR rate; the long-backfill cool-down/heartbeat defers (story-1.3/2.3) bite here too until 3.2; (d) **co-filer attribution edge (inherited from the Story 2.2 reconciler, SM-2)** — `compute_work_list` dedups a co-filed accession to its smallest-CIK filer and `present_accessions` tests membership by accession alone, so if two *distinct* Universe CIKs share one accession and the higher-CIK filer's *only* outstanding filing is that co-filing, catch-up derives only the smaller CIK as affected, re-ingests only its `companyfacts`, and — once the accession is present — the higher-CIK filer's own `companyfacts` for it is never fetched. Rare (independent large-caps rarely co-file periodic reports; a non-Universe subsidiary is filtered out pre-dedup) and a property of the *reused* reconciler (3.1 must not re-design it), so recorded, not fixed. Revisit by keying membership/attribution on `(accession, cik)` if a real co-filing gap appears.
  - [x] (Optional) Live smoke: `fintin catch-up` against the local `default` DB on the existing Universe (scratchpad config with the real email, removed after) — expect `NOTHING_TO_DO` if current, or `STARTED→COMPLETED` if a new filing landed.

## Dev Notes

### What this story IS
The **first Epic 3 story**: a single `fintin catch-up` command that brings the store current by *composing primitives that already exist* — the Story 2.2 work-list reconciler (discovery) and the Story 2.3 per-company backfill engine (ingest) — behind a pure `core/catchup.py` that owns only the run-status vocabulary (`STARTED`/`NOTHING_TO_DO`/`COMPLETED`) and the work-list→affected-companies derivation. It introduces one small pure `core` module and one CLI command. It is the "catch up to today is the only behavior the reconciler has" from the architecture brief.

### What this story is NOT (scope fences — do not implement)
- ❌ **No single-flight lease / heartbeat / `ALREADY_RUNNING`** — that is **Story 3.2** (AD-12). This engine has no concurrency guard; 3.2 wraps it. Do model `CatchUpStatus` as an `enum.Enum` so 3.2 adds one member.
- ❌ **No re-implementation of the work-list reconciler or the ingest loop** — reuse `compute_work_list` (at the CLI seam) and `backfill_universe` (inside the engine). A second implementation is a review-blocking failure (epic guardrail).
- ❌ **No new DDL / table / cursor / run-log** — catch-up is stateless-derived (AD-1); the status vocabulary is returned/logged, never persisted. `schema.py` untouched (AD-18).
- ❌ **No new config section** — reuse `[reconcile].lookback_days` and `[universe]`/`[clickhouse]`/`[edgar]`.
- ❌ **No scoped recovery (`recover --cik`)** — that is Story 3.3.
- ❌ **No bulk `companyfacts.zip` strategy** — v1 is per-company (AD-13); catch-up uses the existing `CompanyFactsStrategy`.

### Current substrate (Epic 1 + Epic 2, on `main`) — reuse, do not reinvent (all verified present)
- `fintin/core/reconcile.py` — `WorkItem(accession, cik, form, filed_date)`, `WorkList(items, scanned, already_present)`, `resolve_window(hwm, lookback_days, today) -> (start, end)`, `compute_work_list(candidates, present_accessions) -> WorkList` (pure; dedups candidates by accession, drops present, sorts by `(filed_date, accession)`).
- `fintin/core/backfill.py` — `backfill_universe(ciks, *, strategy, insert_rows, taxonomy_version, version, already_present=frozenset(), fatal_errors=(), max_consecutive_failures=None, on_company=None) -> BackfillReport`; `BackfillStrategy` Protocol (`name`, `company_facts(cik)`); `BackfillReport` (`companies_ingested/companies_skipped/companies_failed/rows_landed/failures/attempted`); `BackfillFailure(cik, reason)`; `BackfillEvent(cik, outcome, index, total)`; `BackfillAborted`. **The engine already sorts+dedups CIKs, offsets `version` per company, records failures/continues, aborts on `fatal_errors`, and circuit-breaks on `max_consecutive_failures` — catch-up gets ALL of this free by calling it with `already_present=frozenset()`.**
- `fintin/core/ingest.py` — `ingest_company`, `RawFactRow`, `IngestResult`, `FactLike`.
- `fintin/adapters/edgar/filings_index.py` — `fetch_work_candidates(edgar_client, *, filing_date, ciks) -> list[WorkItem]` (index over the window, through the rate-limited client; empty `ciks` → `[]`).
- `fintin/adapters/edgar/backfill.py` — `CompanyFactsStrategy(client)` (`name="per-company"`, `company_facts(cik)` → `fetch_company_facts`; raises `NoCompanyFactsError` for an unknown CIK → recorded gap).
- `fintin/adapters/edgar/client.py` — `EdgarClient(cfg)` (ban-safety email gate on construction), `EdgarConfigError`, `EdgarThrottleError`.
- `fintin/adapters/edgar/facts.py` — `fetch_company_facts`, `edgartools_version`.
- `fintin/adapters/edgar/universe.py` — `resolve_tickers` (offline bundled parquet).
- `fintin/adapters/store/raw_fact_repo.py` — `high_water_mark(client) -> date | None` (count-guarded), `present_accessions(client, *, accessions) -> set[str]` (parameterized, no `FINAL`, empty→set() no query), `next_ingest_version(client) -> int`, `insert_raw_facts(client, rows) -> int`.
- `fintin/adapters/store/client.py` — `get_client`, `check_connection`, `StoreConnectionError`.
- `fintin/config.py` — `Config`, `ReconcileConfig(lookback_days)` (always populated; default `DEFAULT_LOOKBACK_DAYS`), `load_config`, `ConfigError`.
- CLI templates to mirror: **`work-list`** (the exact discovery pipeline: hwm→window→candidates→present→compute_work_list) and **`backfill`** (EdgarClient-once, `next_ingest_version`, `fatal_errors=(EdgarThrottleError,)`, `max_consecutive_failures=10`, `BackfillAborted`→exit 1, `_log_company`, `--show-gaps`, `finally`-close). Catch-up = `work-list`'s discovery + `backfill`'s ingest wiring + the status render.

### The composition (the load-bearing logic)
```
# CLI (dumb trigger) — discovery I/O, exactly like `work-list`:
hwm        = high_water_mark(client)                                   # currency hint (AD-16)
start,end  = resolve_window(hwm, cfg.reconcile.lookback_days, today)   # reordering-safe window
candidates = fetch_work_candidates(edgar_client, filing_date=f"{start}:{end}", ciks=resolved.ciks)
present    = present_accessions(client, accessions={c.accession for c in candidates})  # AD-16 membership
work       = compute_work_list(candidates, present)                    # Epic 2 reconciler — REUSED
version    = next_ingest_version(client)                               # AD-6 base

# ENGINE (pure core) — status vocabulary + per-company ingest via backfill_universe:
report     = catch_up(work, strategy=CompanyFactsStrategy(edgar_client), insert_rows=…,
                      taxonomy_version=…, version=version,
                      fatal_errors=(EdgarThrottleError,), max_consecutive_failures=10, …)
# affected = sorted{ item.cik for item in work.items }
#   empty  → NOTHING_TO_DO
#   else   → STARTED → backfill_universe(affected, already_present=∅) → COMPLETED
```
Everything filed since is ingested because each affected company's *full* `companyfacts` (including the new filing) is re-ingested; unchanged history re-lands idempotently (AD-6), the new/restated facts win by higher `version` + latest-filed (AD-6/AD-7).

### Architecture constraints (authoritative)
- **FR-8** — catch-up: bring the store current; ingest everything filed since; report `STARTED`→`COMPLETED`. [epics.md#Story-3.1; prd FR-8]
- **FR-12** — pure engine / decoupled CLI trigger: the engine has zero knowledge of its caller. [prd FR-12; AD-2]
- **AD-1** — derive work every run from the DB + EDGAR index; never persist a cursor/checkpoint/run-log. [SPINE#AD-1]
- **AD-2** — pure engine, dumb trigger. [#AD-2]
- **AD-6** — `ReplacingMergeTree(version)`; a re-ingest with a higher ingest-monotonic `version` supersedes on read (FINAL/argMax). One base per run, offset per company. [#AD-6]
- **AD-11** — per-company idempotent commits (one company = one insert). [#AD-11]
- **AD-13** — backfill/ingest is strategy-pluggable; v1 = per-company `companyfacts`; bulk deferred. Catch-up reuses the per-company strategy. [#AD-13]
- **AD-16** — per-accession membership is the correctness authority; the high-water mark only sizes the scan window. [#AD-16]
- **SM-C1** — fewer/slower requests are always preferred to a ban; a throttle aborts the run (outranks finishing). [prd SM-C1]
- **NFR-7** — tests never hit live EDGAR; the catch-up happy path is exercised via engine + adapter unit tests. [#Testing]
- **Status vocabulary** — `STARTED`/`ALREADY_RUNNING`/`NOTHING_TO_DO`/`COMPLETED` all exit-0; triggers interpret outcomes, the engine emits them. [architecture-brief §"success-not-failure status vocabulary"]

### Previous Story Intelligence (Epic 1 + 2.1–2.4)
- **Reuse `backfill_universe` wholesale (Story 2.3).** It already sorts/dedups CIKs, offsets `version` per company (co-filed-accession determinism), records per-company failures and continues (SM-2), aborts on injected `fatal_errors` (throttle), and circuit-breaks on `max_consecutive_failures` (systemic — Story 2.3 P2). Passing `already_present=frozenset()` gives "re-ingest every affected company" (the opposite of backfill's resume-skip — intentional).
- **Wrap `resolve_universe` + surface no traceback (Story 2.3 P1).** A degraded edgartools install renders a clean exit 1.
- **Build the EdgarClient once (Story 2.3).** A second construction resets process-global edgar rate state; reuse the one client for discovery + ingest.
- **`fatal_errors=(EdgarThrottleError,)` + `max_consecutive_failures=10` + `BackfillAborted`→exit 1 (Story 2.3).** Same ban-safety wiring; the CLI test monkeypatches the engine to raise each and asserts exit 1 (offline).
- **`present_accessions` is parameterized, no `FINAL`, empty→no query (Story 2.2).** Membership by exact accession is the authority (AD-16), decoupled from any date.
- **`max()` on empty ClickHouse returns `1970-01-01`, not NULL (Story 2.2).** `high_water_mark` guards with `count()` → `None` on empty → `resolve_window` anchors at `today` (recent window; full-history-from-empty is backfill's job).
- **Observer exceptions must not sink a committed run (Story 2.3 P5).** `_emit_status` swallows `on_status` errors (as `_emit` does for `on_company`) — critical for the `COMPLETED` emission after the work is already committed.
- **CLI house style:** deferred heavy imports; `typer.secho(..., fg=RED, err=True)` + `raise typer.Exit(code=…)`, never a traceback; close the client in `finally` via `contextlib.suppress`; GREEN success / YELLOW gaps; error paths CLI-tested.
- **Determinism (kboss):** affected CIKs `sorted`; `work.items` already sorted by `(filed_date, accession)`.
- **`enum.Enum`, not `str, Enum` (footgun):** a `str`-mixed enum's f-string/`str()` rendering differs across Python versions (`StrEnum` vs mixin); use a plain `Enum` and render `.value` explicitly.

### Public repo / security (hard constraints)
⚠️ **Public repo:** never write a real email/PII/secret into a tracked file. Catch-up needs an `[edgar]` block (it hits EDGAR), but **tests use `you@example.com` (placeholder — rejected by the gate) and `a@b.co` (valid non-placeholder) only**; the operator's real contact email lives ONLY in the gitignored `fintin.toml`. Any live smoke uses a scratchpad-only untracked config, removed after. Tickers/CIKs are public. **Tests must NEVER hit live EDGAR (NFR-7)** — the throttle/systemic CLI tests monkeypatch the engine; the `NOTHING_TO_DO` CLI test stubs the index fetch.

### Project Structure Notes
- **New:** `fintin/core/catchup.py` (pure engine).
- **Modified:** `fintin/cli/app.py` (`catch-up` command + `_root` docstring), `README.md`, `deferred-work.md`.
- **New tests:** `tests/test_catchup.py`. **Extended:** `tests/test_cli.py` (catch-up error + ban-safety + `NOTHING_TO_DO` offline).
- Hexagonal invariant: `core/catchup.py` imports no `edgar`/ClickHouse/`pyarrow` (AST-guarded); `adapters/edgar/` owns EDGAR access; `adapters/store/` owns ClickHouse; `cli/` is a dumb trigger.

### References
- [Source: _bmad-output/planning-artifacts/epics.md#Story-3.1 — ACs; Epic 3 refinement notes ("reuses, never re-implements"; "story-review guardrail prevents a second implementation")]
- [Source: _bmad-output/planning-artifacts/prds/…/prd.md — FR-8 (catch-up), FR-12 (pure engine/decoupled trigger), SM-C1]
- [Source: _bmad-output/brainstorming/…/architecture-brief.md §4 (the Stateless Reconciler: "catch up to today is the only behavior"; success-not-failure status vocabulary, all exit-0; engine = pure idempotent command, zero knowledge of caller; trigger is a late-bound pluggable parameter)]
- [Source: _bmad-output/implementation-artifacts/2-2-db-derived-work-list.md — the reconciler (`compute_work_list`/`resolve_window`/`fetch_work_candidates`/`present_accessions`) catch-up reuses]
- [Source: _bmad-output/implementation-artifacts/2-3-per-company-resumable-backfill.md — `backfill_universe`/`BackfillStrategy`/`CompanyFactsStrategy`, `fatal_errors`/`max_consecutive_failures`/`BackfillAborted`, P1 traceback-guard + P2 circuit-breaker + P5 observer-guard lessons; the AD-13 defer noting catch-up reuses this reconciler]
- [Source: _bmad-output/implementation-artifacts/deferred-work.md — the story-2.3 defer: "That currency is the job of catch-up (Epic 3, reusing the Story 2.2 index-based reconciler), not backfill"; the "long backfill inherits the uninterruptible cool-down + no single-flight lease" note → Story 3.2]

## Review Findings

Reviewed 2026-07-24 by 3 parallel adversarial layers (Blind Hunter, Edge Case Hunter, Acceptance Auditor), all completed. **All 5 ACs + 8 settled decisions verified genuinely satisfied; no Critical/High/Medium.** Independently confirmed by all three layers: the epic reuse-guardrail (`catch_up` delegates to `backfill_universe`; the CLI reuses `compute_work_list` — no second reconciler or ingest loop), the status vocabulary (`NOTHING_TO_DO` = no fetch/no `STARTED`; `STARTED`→`COMPLETED`; `COMPLETED` never emitted on abort), purity (AST-guarded), exit-code discipline, ban-safety (throttle/systemic → exit 1), NFR-7 (no live EDGAR in tests), monotonic `version` supersession across repeated runs, and the co-filer attribution edge correctly deferred (inherited reconciler property, not a 3.1 bug). Triage: **5 patch, 3 defer, 2 dismissed.**

- [x] [Review][Patch] **Systemic-abort said "backfill aborted" during a `catch-up` run** — `backfill_universe` is now shared by both commands, but its `BackfillAborted` message read "backfill aborted after N consecutive failures", so an operator who ran `fintin catch-up` saw the wrong command name; the CLI test masked it with a fabricated string. Neutralized the engine message to "run aborted after …" (serves both callers correctly), and made the catch-up systemic test assert the real command-neutral wording (+ `"backfill" not in output`). [fintin/core/backfill.py, tests/test_cli.py]
- [x] [Review][Patch] **CLI `COMPLETED`/gaps render branch was untested** — the GREEN `STARTED→COMPLETED` summary + YELLOW gap line + `--show-gaps` loop had zero coverage (only `NOTHING_TO_DO`/throttle/systemic were CLI-exercised), so a render-string typo could ship. Added an offline test that monkeypatches the engine to RETURN a hand-built `COMPLETED` `CatchUpReport` (1 ingested, 1 recorded gap) and asserts the summary + `--show-gaps` gap enumeration + exit 0. [tests/test_cli.py]
- [x] [Review][Patch] **Loose type annotations** — `CatchUpReport.failures` was a bare `tuple`; tightened to `tuple[BackfillFailure, ...]` (matching `BackfillReport.failures`), and typed the CLI callbacks `_log_company(event: BackfillEvent)` / `_log_status(status: CatchUpStatus)`. [fintin/core/catchup.py, fintin/cli/app.py]
- [x] [Review][Patch] **`max_consecutive_failures = 10` duplicated magic literal** — the same ban-safety constant was declared verbatim in both `backfill` and `catch-up` and could drift; hoisted to a single module-level `_MAX_CONSECUTIVE_FAILURES` referenced by both. [fintin/cli/app.py]
- [x] [Review][Patch] **Spec Task-1 import list inaccurate** — it listed `ingest.FactLike` (not imported) and omitted `BackfillEvent`/`BackfillFailure` (imported for the `on_company` type + the `failures` annotation); corrected the import list + decision 4 to match the code. [3-1-pure-catch-up-engine.md]
- [x] [Review][Defer] **No `--debug`/traceback escape hatch on the generic error path** [fintin/cli/app.py] — the "never a traceback" UX (consistent across every command) discards the stack for a genuinely unexpected fault. Deferred as a **cross-command** observability improvement (add `logger.debug(exc_info=True)` or a uniform `--debug` flag), not a 3.1-specific change.
- [x] [Review][Defer] **Purity AST guard is shallow + cwd-dependent** [tests/test_catchup.py] — it denylists three top-level import names and uses a repo-root-relative path (breaks if pytest runs from another directory). Every purity guard in the repo (backfill/coverage/universe/reconcile) shares this exact pattern; a cross-cutting hardening (allowlist + `Path(__file__)`-relative) is a separate change, not catch-up-only. Module under review is genuinely pure.
- [x] [Review][Defer] **`NOTHING_TO_DO` conflates "store current" with a non-raising empty index result** [fintin/cli/app.py] — `fetch_work_candidates` returns `[]` both for a legitimately-empty window and when `edgar.get_filings(...)` returns `None`/empty **without** raising, so a transient non-raising discovery failure would read as GREEN `NOTHING_TO_DO`. Near-unreachable (`resolve_window` guarantees valid in-range dates; a real discovery error raises → exit 1) and inherited from the reconciler adapter; a discovery-level empty-vs-absent signal is beyond 3.1's reuse mandate.

_Dismissed (2, with rationale): (a) **exit 0 when every affected company fails below the consecutive threshold** — the ratified SM-2 policy (per-company failures are explained gaps, all exit-0), identical to `backfill`; a distinct exit code would diverge cross-command and needs a deliberate cross-command decision, not this story. (b) **`NOTHING_TO_DO` render punctuation** differs from the spec literal by one period — purely cosmetic; the CLI tests assert substrings._

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Opus 4.8, 1M context)

### Debug Log References

- `uv run pytest -q` → **282 passed** (+20 over the 262 baseline). New `test_catchup.py` (11, pure incl. AST guard); `test_cli.py` +9 (`catch-up` help + 5 error paths + `NOTHING_TO_DO` offline + throttle-abort + systemic-abort). Python 3.14 in `.venv` (≥ 3.12 floor). No live EDGAR in any test (NFR-7) — the throttle/systemic CLI tests monkeypatch the engine; `NOTHING_TO_DO` stubs the index fetch.
- `uv run fintin --help` → all 9 commands wired (check-connection, schema-init, ingest-company, map-canonical, universe, work-list, backfill, **catch-up**, status).
- Live smoke (1 company, scratchpad config from the gitignored `fintin.toml`, removed + verified gone after; no PII in any tracked file): `fintin catch-up --config <scratchpad> --show-gaps` over `ciks = [320193]` against the local `default` DB → `catch-up: NOTHING_TO_DO` / `Nothing to do — the store is already current over the 7-day lookback (NOTHING_TO_DO) [1 scanned].` Exit 0. Confirms the real end-to-end wiring: the index fetch found Apple's most-recent in-window filing, membership showed it already present, and catch-up returned `NOTHING_TO_DO` with **no** `companyfacts` request (the ban-frugal outcome).

### Completion Notes List

- **Composition story, exactly as designed — reuse, never re-implement (the epic guardrail).** New pure `core/catchup.py` owns only (a) the affected-CIK derivation from the reconciler's `WorkList` and (b) the `STARTED`/`NOTHING_TO_DO`/`COMPLETED` vocabulary. Discovery reuses the Story 2.2 reconciler verbatim at the CLI seam (`resolve_window`/`fetch_work_candidates`/`present_accessions`/`compute_work_list`); ingest reuses `backfill_universe` (Story 2.3) over the affected CIK set with `already_present=frozenset()`. No second reconciler or ingest loop written.
- **Per-accession work list → per-company `companyfacts` ingest (AD-13).** Catch-up re-ingests each affected company's full history; a new filing and any restated period land and win on read (higher ingest-monotonic `version`, AD-6; latest-filed, AD-7). Verified by `test_present_company_is_reingested_for_its_new_filing` (the restatement/new-filing path — the deliberate opposite of backfill's resume-skip).
- **Status vocabulary, all exit-0.** `NOTHING_TO_DO` (empty work list — no fetch, no insert) or `STARTED`→`COMPLETED`, emitted via `on_status` (core does no I/O). `ALREADY_RUNNING`/lease is Story 3.2; `CatchUpStatus` is a plain `enum.Enum` so 3.2 adds one member. A throttle (`fatal_errors`) or systemic `BackfillAborted` propagates out and the CLI exits 1 (ban-safety, SM-C1) — those are NOT in the success vocabulary.
- **Pure engine, dumb trigger (AD-2/FR-12).** `core/catchup.py` is `edgar`/ClickHouse/`pyarrow`-free (AST-guarded, `test_core_catchup_is_pure`). The `catch-up` CLI builds the one rate-limited `EdgarClient` once (reused for discovery + ingest; ban-safety email gate), runs discovery, and renders. No new DDL/table/config/cursor (AD-1/AD-18) — `schema.py` and `fintin.toml.example` untouched; reuses `[reconcile].lookback_days`.
- **Determinism (kboss):** affected CIKs `sorted`; per-company `version` = base + sorted position (delegated to `backfill_universe`).
- **Carried the Story 2.3 review lessons forward:** wrapped `resolve_universe` (P1 traceback guard), `EdgarClient` built once, `fatal_errors=(EdgarThrottleError,)` + `max_consecutive_failures=10` + `BackfillAborted`→exit 1, observer exceptions swallowed (`_emit_status`, P5).
- Exit codes: `ConfigError`→2, missing `[universe]`→2, `EdgarConfigError` (missing/placeholder email)→2, resolution-fail/empty-universe→1, `StoreConnectionError`→1, throttle/systemic→1, generic→1; `NOTHING_TO_DO`/`COMPLETED` (gaps included)→0. Never a traceback.
- Two self-review refinements folded in before implementation: the `NOTHING_TO_DO` render no longer implies a `STARTED→` transition (an empty work list never starts a run); the co-filer attribution edge (inherited from the reused reconciler) is recorded in `deferred-work.md` (SM-2), not code-changed (3.1 must not re-design the reconciler).

### File List

- `fintin/core/catchup.py` (NEW) — pure `catch_up` engine + `CatchUpStatus` + `CatchUpReport`.
- `fintin/cli/app.py` (MOD) — `catch-up` command (discovery + ingest wiring + status render); `_root` docstring updated.
- `tests/test_catchup.py` (NEW) — pure engine tests (11) + AST purity guard.
- `tests/test_cli.py` (MOD) — `catch-up` error paths + ban-safety wiring + `NOTHING_TO_DO` offline (9).
- `README.md` (MOD) — "Catch up to today" section; `work-list` forward-reference updated.
- `_bmad-output/implementation-artifacts/deferred-work.md` (MOD) — Story 3.1 deferred items (companyfacts re-fetch, index↔companyfacts lag, no-lease-yet→3.2, co-filer attribution edge).

## Change Log

- 2026-07-24 — Story 3.1 drafted via create-story (exhaustive substrate analysis: reconciler + backfill engine + CLI templates read in full). Design settled: `fintin catch-up` = the Epic 2 work-list reconciler (discovery, reused at the CLI seam) + `backfill_universe` over the affected CIK set (`already_present=∅`, reused inside a new pure `core/catchup.py`) + the `STARTED`/`NOTHING_TO_DO`/`COMPLETED` status vocabulary (all exit-0; `ALREADY_RUNNING`/lease is Story 3.2). Per-accession work list → per-company `companyfacts` re-ingest (AD-13; restatements absorbed via AD-6/AD-7). Pure engine/dumb trigger (AD-2/FR-12); no new DDL/table/config/cursor (AD-1/AD-18); throttle/systemic abort → exit 1 (SM-C1). Status → ready-for-dev.
- 2026-07-24 — Code review (3 parallel adversarial layers: Blind Hunter, Edge Case Hunter, Acceptance Auditor). All 5 ACs + 8 settled decisions verified satisfied; **no Critical/High/Medium**. Applied all **5 patch findings**: (P1) neutralized the shared `backfill_universe` abort message ("backfill aborted" → "run aborted") so a `catch-up` systemic abort doesn't name the wrong command, and made the CLI test assert the real wording; (P2) added the missing offline `COMPLETED`-render CLI test (GREEN summary + gap line + `--show-gaps`); (P3) tightened `CatchUpReport.failures` → `tuple[BackfillFailure, ...]` + typed the CLI callbacks; (P4) hoisted `max_consecutive_failures=10` to a shared `_MAX_CONSECUTIVE_FAILURES`; (P5) corrected the spec import list. **3 deferred** (cross-command `--debug`/traceback hatch; cross-cutting purity-guard hardening; non-raising empty-index masking as `NOTHING_TO_DO`) and **2 dismissed** (exit-0-on-all-fail = ratified SM-2, matches backfill; `NOTHING_TO_DO` punctuation), logged. **283 tests pass (+1)**. Secret-scan clean. Status → done.
- 2026-07-24 — Story 3.1 implemented (red-green-refactor through all 4 tasks). Pure `catch_up` engine + `CatchUpStatus`/`CatchUpReport` (`core/catchup.py`) — derives affected CIKs from the reused `WorkList` and delegates ingest to `backfill_universe` (`already_present=∅`); owns only the run-status vocabulary. `catch-up` dumb-trigger CLI reuses `work-list`'s discovery + `backfill`'s ban-safety wiring. Reuses the Epic 2 reconciler + Story 2.3 backfill engine, re-implements neither (epic guardrail). Pure core AST-guarded; no DDL/table/config/cursor (AD-1/AD-18). **282 tests pass (+20)**; the `NOTHING_TO_DO` branch is CLI-drivable offline, throttle/systemic abort→exit 1 asserted offline (NFR-7: zero live-EDGAR in tests). Live 1-company smoke returned `NOTHING_TO_DO` with no `companyfacts` request (scratchpad config removed after; no PII in any tracked file). Deferred items logged (companyfacts re-fetch vs per-accession, index↔companyfacts lag, no-lease-yet→3.2, co-filer attribution edge). Status → review.
