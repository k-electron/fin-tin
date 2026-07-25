# Story 2.4: Coverage & status report

Status: ready-for-dev

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the builder,
I want to see coverage and currency for the Universe with `fintin status`,
so that I know what's ingested and what's an explained gap — with no silent omissions.

## Acceptance Criteria

1. **Given** an ingested store **When** I run `fintin status` **Then** it reports the **count of in-scope companies present**, the **high-water mark** (latest `filed_date`, or a clear "empty store" note), and **any in-scope company with zero facts as an explained gap** — no silent omissions (FR-14, SM-2). Unresolvable-ticker gaps from Universe resolution are surfaced too (the second class of "in scope but absent").
2. **Given** a company that failed during backfill **Then** it appears as an **explained gap with a reason** — because a failed/factless company is exactly one absent from `raw_fact`, `status` derives it (in-scope CIK − present CIKs) and reports the reason "no facts in store" (the durable DB-derived state; the ephemeral per-run failure message is not persisted — AD-1).
3. **Given** `status` runs **Then** it is **fully offline** — ClickHouse reads + bundled-parquet Universe resolution only, **no `EdgarClient`, no contact email, no EDGAR request** (like `universe`/`map-canonical`).
4. **Given** the coverage math **Then** it is a **pure `core` function** fed already-fetched store data (present CIKs + high-water mark) and the resolved Universe — a dumb CLI trigger does the queries and renders (AD-2). No new DDL, no new config (AD-18/AD-1).
5. **Given** the offline test suite **Then** it covers the pure coverage engine (plain data), the CLI error paths, **and** an end-to-end CLI happy path against a throwaway ClickHouse DB (`status` is the one command whose happy path is offline-verifiable) — zero live EDGAR (NFR-7).

## 🔑 Key design decisions (settled)

1. **`status` DERIVES coverage from the DB; it never reads a failures/status table (AD-1).** There is no `failures`/`ingest_run`/`coverage` table anywhere (AD-1 forbids a persisted ledger; the schema owns only the 6 tier/mart objects). Coverage = pure set arithmetic: in-scope = `resolve_universe(...).ciks`; present = `present_ciks(client, ciks=in_scope)`; **zero-fact gaps = in-scope − present**; currency = `high_water_mark(client)`. This is explicitly the design the Story 2.3 hand-off and `deferred-work.md` already ratified ("the Story 2.4 coverage surface … derives zero-fact gaps from DB absence").
2. **A backfill-failed company's "reason" is the derived DB state, not the run's exception message (AC-2, AD-1).** A `NoCompanyFactsError`, a mid-transform error, and a genuinely-factless company all manifest identically: **absent from `raw_fact`**. `status` reports them uniformly as `CIK <n>: no facts in store`. The specific per-run reason was ephemeral (`BackfillFailure(cik, reason)`, surfaced by `fintin backfill --show-gaps` at run time) and is intentionally not persisted (AD-1). `status` reads only the DB's ground truth (AD-1: "computed at run time from the DB").
3. **Two classes of explained gap, both surfaced (SM-2 — no silent omissions).** (a) **Unresolvable-ticker gaps** — `resolved.gaps` (`UniverseGap(identifier, reason)`) from offline resolution, rendered exactly like `fintin universe` does. (b) **Zero-fact gaps** — in-scope CIKs absent from `raw_fact`, rendered `CIK <n>: no facts in store`. Counts are always shown; `--show-gaps` enumerates both classes (mirrors `backfill --show-gaps` so a large incomplete-backfill gap list doesn't flood the default output — the count is still exact, so nothing is *silently* omitted).
4. **Pure engine, dumb trigger (AD-2).** A new pure `fintin/core/coverage.py` (`CoverageReport` + `compute_coverage(resolved, present, hwm)`) does the set math on plain values; the `status` CLI does the two store queries and renders. Mirrors `resolve_universe`/`compute_work_list` (pure core fed by injected/adapter-fetched data). AST-guarded `edgar`/ClickHouse/`pyarrow`-free.
5. **Offline — no `EdgarClient`, no email (AC-3).** Both inputs are offline: Universe resolution reads edgartools' bundled parquet (Story 2.1 — no request), and coverage reads ClickHouse. Constructing an `EdgarClient` would demand a real contact email for a read-only offline op — the exact anti-pattern Story 2.1 rejected. `status` = `universe`'s offline resolution + `map-canonical`'s ClickHouse-only structure.
6. **Gaps do not change the exit code.** A successful report exits **0** even with gaps (they're explained states, not errors — the "all exit-0" run vocabulary). An empty store is a valid report (0 present, all in-scope as gaps, exit 0). Only misconfiguration/infrastructure fails loudly: `ConfigError`→2, missing `[universe]`→2, empty resolved Universe→1, `StoreConnectionError`→1, generic→1. Never a traceback.
7. **No new repo query, no DDL, no config.** `present_ciks` + `high_water_mark` already exist and suffice (present count = `len(present_ciks(...))`; gaps = set difference). `schema.py` stays untouched (AD-18); `fintin.toml.example` unchanged.

## Tasks / Subtasks

- [ ] **Task 1 — Pure coverage engine** (AC: 1, 2, 4) — `fintin/core/coverage.py` (NEW, pure)
  - [ ] Define `CoverageReport(NamedTuple)`: `in_scope: int`, `present: int`, `zero_fact_ciks: tuple[int, ...]` (sorted), `resolution_gaps: tuple[UniverseGap, ...]`, `hwm: date | None`. Convenience `@property`: `missing` (= `len(zero_fact_ciks)`), `total_gaps` (= `missing + len(resolution_gaps)`), `is_complete` (= `missing == 0 and not resolution_gaps`).
  - [ ] Implement `compute_coverage(resolved: ResolvedUniverse, present: Collection[int], hwm: date | None) -> CoverageReport`. Pure: `in_scope_set = set(resolved.ciks)`; `present_in_scope = in_scope_set & {int(c) for c in present}` (intersect defensively); `zero_fact = tuple(sorted(in_scope_set - present_in_scope))`; carry `resolved.gaps` and `hwm` through. Deterministic (sorted).
  - [ ] Import only `from fintin.core.universe import ResolvedUniverse, UniverseGap` + stdlib (`date`, `NamedTuple`, `Collection`). No `edgar`/ClickHouse/`pyarrow`.
- [ ] **Task 2 — `status` CLI trigger** (AC: 1, 2, 3, 6) — `fintin/cli/app.py` (MOD)
  - [ ] Add `@app.command("status")` with `--config/-c` and `--show-gaps` (enumerate every explained gap; default shows counts).
  - [ ] `_configure_logging()`; **deferred imports** (all pure `fintin.*` + the offline `resolve_tickers` — **no `edgar` client import**): `resolve_tickers`, `resolve_universe`, `present_ciks`, `high_water_mark`, `compute_coverage`.
  - [ ] `load_config` → `ConfigError` **exit 2**. `cfg.universe is None` → **exit 2** (clean "no [universe]" message).
  - [ ] Wrap `resolve_universe(cfg.universe, resolve_tickers=resolve_tickers)` in `try/except Exception` → "Universe resolution failed" **exit 1** (a degraded edgartools install must not print a traceback — the Story 2.3 P1 lesson). Empty `resolved.ciks` → **exit 1**.
  - [ ] `check_connection(cfg.clickhouse)` → `StoreConnectionError` **exit 1**.
  - [ ] `client = get_client(...)` in `try/finally` (close via `contextlib.suppress`); `present = present_ciks(client, ciks=resolved.ciks)`; `hwm = high_water_mark(client)`; `report = compute_coverage(resolved, present, hwm)`. Generic `Exception` → "Status failed" **exit 1**.
  - [ ] Render (GREEN summary): `Coverage: {present} of {in_scope} in-scope company(ies) present. High-water mark: {hwm.isoformat() or 'none (store empty)'}.` Then if `report.total_gaps`: YELLOW `"{total_gaps} explained gap(s): {G} unresolvable ticker(s), {H} zero-fact company(ies)."` If `--show-gaps`: list resolution gaps (`  - {gap.identifier}: {gap.reason}`) then zero-fact CIKs (`  - CIK {cik}: no facts in store`). **exit 0** on a successful report (gaps included).
  - [ ] **No `EdgarClient` constructed anywhere** in this command (AC-3). Pluralization helper consistent with `universe`/`backfill`.
- [ ] **Task 3 — Tests (offline; NFR-7)** (AC: 5)
  - [ ] `tests/test_coverage.py` (NEW, pure): `compute_coverage` with plain sets/dates — present count; zero-fact = sorted set difference; resolution-gap passthrough; `hwm` passthrough; empty store (0 present, all in-scope as zero-fact); fully-covered (`is_complete`); partial; `total_gaps`/`missing` properties; determinism (unsorted input → sorted output). **AST purity guard**: `core/coverage.py` imports no `edgar`/`clickhouse`/`pyarrow` (reuse the `test_universe.py`/`test_reconcile.py` helper).
  - [ ] `tests/test_cli.py` (MOD): `status` error paths — help lists `status`; missing config → 2; missing `[universe]` → 2; empty-universe (unresolvable-only tickers, offline) → 1; each asserts no `Traceback`.
  - [ ] `tests/test_cli.py` (MOD, `@pytest.mark.integration`): **end-to-end happy path** — create a throwaway DB (reuse the `schema_client`/`local_clickhouse_config` pattern), seed `raw_fact` rows for one in-scope CIK, write a config toml naming that DB + a `[universe]` with two CIKs (one seeded, one absent), invoke `fintin status --config <toml> --show-gaps`, assert: present count = 1 of 2, the HWM date shows, the absent CIK is listed as `no facts in store`, exit 0, no `Traceback`. (This is offline — ClickHouse only, no EDGAR — so NFR-7 holds trivially.)
- [ ] **Task 4 — Validate & document** (AC: all)
  - [ ] `uv run pytest` — full suite green; record count + delta.
  - [ ] Update `README.md`: a "Check coverage & status" section (`fintin status`, `--show-gaps`; offline; explained gaps = unresolvable tickers + zero-fact companies; a failed-backfill company shows as `no facts in store`).
  - [ ] `fintin.toml.example` needs **no** change.
  - [ ] Append any 2.4 deferred item to `deferred-work.md` (e.g. `status` shows DB-derived absence, not the ephemeral per-run failure reason — the AD-1 trade).
  - [ ] (Optional) Live smoke: `fintin status` against the local `default` DB (offline — needs no email) on the existing Apple data + a deliberately-absent CIK.

## Dev Notes

### What this story IS
The **final Epic 2 story**: an offline, read-only `fintin status` that reports coverage (how many in-scope companies are present), currency (the high-water mark), and every explained gap (unresolvable tickers + in-scope companies with zero facts) — no silent omissions (FR-14/SM-2). It is a pure composition of primitives that already exist; it introduces one small pure `core` module and one CLI command.

### What this story is NOT (scope fences — do not implement)
- ❌ **No new DDL / table** — there is no failures/status/coverage table and this story adds none (AD-1/AD-18). Coverage is derived, never stored.
- ❌ **No EDGAR access / EdgarClient / contact email** — fully offline (AC-3).
- ❌ **No persistence of backfill failure reasons** — AD-1 forbids the ledger; the run-time reason lives in `backfill --show-gaps`, not here.
- ❌ **No catch-up "run outcome" reporting** — FR-14's "after a Catch-up, reflects the run's outcome" consequence is an Epic 3 (catch-up) concern; Story 2.4 is the standalone report only.
- ❌ **No new config section** — `status` reads `[clickhouse]` + `[universe]` only.

### Current substrate (Epic 1 + Epic 2 stories 2.1–2.3, on `main`)
Reuse — do not reinvent (all verified present):
- `fintin/core/universe.py` — `resolve_universe(universe, *, resolve_tickers) -> ResolvedUniverse`; `ResolvedUniverse(ciks: tuple[int,...], gaps: tuple[UniverseGap,...], tickers_resolved: int, explicit_ciks: int)`; `UniverseGap(identifier: str, reason: str)`. `.ciks` is sorted/deduped; `.gaps` in config order.
- `fintin/adapters/edgar/universe.py` — `resolve_tickers(tickers) -> dict[str, int | None]` (offline bundled parquet; no network/email). Deferred-import it; it's the offline resolver port.
- `fintin/adapters/store/raw_fact_repo.py` — `present_ciks(client, *, ciks) -> set[int]` (`SELECT DISTINCT cik FROM raw_fact WHERE cik IN %(ciks)s`, no `FINAL`, empty→set() no query); `high_water_mark(client) -> date | None` (`count()`-guarded `max(filed_date)`; `None` on empty). **No count helper exists and none is needed** — `len(present_ciks(...))` is the present count.
- `fintin/adapters/store/client.py` — `get_client(cfg, *, database=None)`, `check_connection(cfg) -> str`, `StoreConnectionError`.
- `fintin/config.py` — `Config`, `UniverseConfig`, `load_config`, `ConfigError`. `cfg.universe is None` when the section is absent.
- CLI templates: `map-canonical` (the offline, ClickHouse-only, **no-EdgarClient** structure) and `universe` (offline Universe resolve + the explained-gap render loop). Mirror both.

### Architecture constraints (authoritative)
- **FR-14** — the report shows: count of in-scope companies present, the store's high-water mark, and any in-scope company with zero facts (as explained gaps). [prd.md#FR-14]
- **SM-2** — every in-scope company is either successfully ingested or listed as an explained gap; **no silent omissions**. [prd.md#SM-2]
- **AD-1** — derive state at run time from the DB; never persist a cursor/checkpoint/ledger. A failures table is forbidden — coverage is derived. [ARCHITECTURE-SPINE.md#AD-1]
- **AD-2** — pure engine, dumb trigger: coverage math is a pure `core` function; `status` is a dumb CLI trigger. [#AD-2]
- **AD-16** — membership is the correctness authority; the HWM is a currency hint only (presence is decided by `present_ciks`, not the HWM). [#AD-16]
- **AD-18** — `adapters/store` owns all DDL; `status` is read-only and adds none. [#AD-18]
- **Errors & status convention** — a per-company failure is recorded (not fatal) and the coverage report lists zero-fact/failed companies as explained gaps; run vocabulary is exit-0. [SPINE Consistency Conventions]
- **NFR-3** — the status query is a cheap membership/aggregate scan (`present_ciks` no-`FINAL` + one `count()/max()`), far lighter than a mart screen; comfortably interactive. Do not add per-company `count()` loops. [epics.md#NFR-3]
- **NFR-7** — offline; the happy path is integration-tested against a throwaway DB, never live EDGAR. [#Testing]

### The derivation (the load-bearing logic)
```
resolved = resolve_universe(cfg.universe, resolve_tickers)     # offline scope + ticker gaps
present  = present_ciks(client, ciks=resolved.ciks)            # AD-16 membership (per company)
hwm      = high_water_mark(client)                             # currency hint (None if empty)
report   = compute_coverage(resolved, present, hwm)            # pure: in-scope − present = gaps
```
`present ⊆ resolved.ciks` (we only query in-scope CIKs), so `zero_fact = sorted(set(resolved.ciks) − present)`. A company that failed backfill is absent from `raw_fact` → it lands in `zero_fact` → reported as `CIK <n>: no facts in store`. Two queries total; no `FINAL`; no mart.

### Project Structure Notes
- **New:** `fintin/core/coverage.py` (pure engine).
- **Modified:** `fintin/cli/app.py` (`status` command), `README.md`.
- **New tests:** `tests/test_coverage.py`. **Extended:** `tests/test_cli.py` (error paths + one integration happy-path).
- Hexagonal invariant: `core/coverage.py` imports no `edgar`/ClickHouse/`pyarrow` (AST-guarded); `adapters/store/` owns all ClickHouse; `cli/` is a dumb trigger. `status` constructs **no `EdgarClient`**.

### Previous Story Intelligence (Epic 1 + 2.1/2.2/2.3)
- **Wrap `resolve_universe` (Story 2.3 P1 review fix):** a degraded edgartools install must render a clean exit 1, never a traceback — `status` wraps it from the start.
- **`resolved.gaps` must be surfaced (Story 2.3 P5 review):** `backfill` was flagged for dropping resolution gaps; `status` is the command that surfaces them — render them (like `universe`).
- **`max()` on empty ClickHouse returns `1970-01-01`, not NULL (Story 2.2):** `high_water_mark` already guards this with `count()`; render `None` as "store empty".
- **Parameterized IN-clauses, no `FINAL` for membership (Story 2.2/2.3):** `present_ciks` already does both.
- **CLI house style:** deferred imports; `typer.secho(..., fg=RED, err=True)` + `raise typer.Exit(code=…)`, never a traceback; close the ClickHouse client in `finally` via `contextlib.suppress`; GREEN success / YELLOW gaps. Error paths CLI-tested.
- **Determinism (kboss):** `compute_coverage` returns sorted `zero_fact_ciks`; gaps in config order.
- **`status` is offline → its happy path IS integration-testable** (unlike `ingest-company`/`work-list`/`backfill`, which hit EDGAR). Seize this: add the end-to-end CLI integration test the EDGAR commands couldn't have.

### Public repo / security (hard constraints)
⚠️ **Public repo:** never write a real email/PII/secret into a tracked file. `status` needs **no** `[edgar]` block at all; test configs use `you@example.com` (placeholder) or `a@b.co` (valid non-placeholder) only if an `[edgar]` block is present for some reason — but `status` doesn't require one. Tickers/CIKs are public. Any live smoke needs no email (offline).

### References
- [Source: _bmad-output/planning-artifacts/epics.md#Story-2.4 — ACs; Epic 2 coverage/gaps; FR-14/NFR-3]
- [Source: _bmad-output/planning-artifacts/prds/prd-fin-tin-2026-07-23/prd.md — FR-14 (§4.6), SM-2, SM-C2]
- [Source: _bmad-output/planning-artifacts/architecture/architecture-fin-tin-2026-07-23/ARCHITECTURE-SPINE.md#AD-1,2,16,18; Errors & status convention; source tree (cli: catch-up/backfill/status); capability map FR-14 = cli + adapters/store]
- [Source: _bmad-output/implementation-artifacts/2-1-resolve-universe-from-config.md — `resolve_universe`/`ResolvedUniverse`/`UniverseGap`, offline-resolution decision, gap-render pattern]
- [Source: _bmad-output/implementation-artifacts/2-3-per-company-resumable-backfill.md — the hand-off ("Story 2.4's `fintin status` reconstructs zero-fact gaps directly from the DB"), `present_ciks`/`high_water_mark`, P1 traceback-guard + P5 resolved.gaps review lessons]
- [Source: _bmad-output/implementation-artifacts/deferred-work.md — line ratifying "the Story 2.4 coverage surface … derives zero-fact gaps from DB absence"]

## Dev Agent Record

### Agent Model Used

### Debug Log References

### Completion Notes List

### File List

## Change Log

- 2026-07-24 — Story 2.4 drafted via create-story (2 parallel research agents: architecture/AC crux + code inventory). Design settled: offline read-only `fintin status`; pure `compute_coverage` in `core/coverage.py`; composes `resolve_universe` + `present_ciks` + `high_water_mark`; explained gaps = unresolvable tickers + zero-fact companies (derived, AD-1 — a failed-backfill company = DB absence, reason "no facts in store"); no EdgarClient/email (AC-3); no DDL/config. Status → ready-for-dev.
