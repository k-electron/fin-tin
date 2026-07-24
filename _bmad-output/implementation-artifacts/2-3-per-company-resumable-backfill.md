# Story 2.3: Per-company resumable backfill

Status: ready-for-dev

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the builder,
I want to backfill the whole Universe resumably via the per-company `companyfacts` strategy behind a pluggable interface,
so that I can populate the market and survive interruptions without re-fetching companies I already have.

## Acceptance Criteria

1. **Given** the resolved Universe **When** I run `fintin backfill` **Then** each in-scope company's full available history is ingested via the per-company `companyfacts` strategy selected behind a pluggable `BackfillStrategy` interface (FR-7, AD-13), **committing per company** (AD-11) — every EDGAR fetch routed through the one rate-limited client (AD-3).
2. **Given** a backfill killed mid-run **When** I re-run it **Then** it resumes without re-ingesting (or re-fetching) already-committed companies — resumption is re-derived from **store membership** (per-company presence in `raw_fact`), not a checkpoint file (FR-10, AD-1, AD-11, AD-16). Re-running a completed backfill is a no-op.
3. **Given** a per-company fetch fails (no company facts, malformed response, or any non-throttle error) **Then** it is **recorded as an explained gap `(cik, reason)` — not fatal** — **And** the run continues to the next company (SM-2). A company that yields zero rows is likewise recorded, never silently dropped.
4. **Given** EDGAR throttles and the client exhausts its cool-down retries **When** backfill is running **Then** the run **aborts loudly** (`EdgarThrottleError`) rather than continuing to hammer EDGAR — ban-safety (SM-C1) outranks completeness (NFR-3).
5. **Given** a much larger Universe later **Then** the strategy can switch to a bulk `companyfacts.zip` implementation **without redesign** — the engine depends only on the `BackfillStrategy` interface; the bulk impl is deferred (AD-13, interface-level only).
6. **Given** the offline test suite **When** it runs **Then** it exercises the engine, the strategy, the membership query, and the CLI error paths with **zero live EDGAR calls** (NFR-7) — pure-data engine tests, a fake `EdgarClient` + monkeypatched `edgar.get_company_facts`, and a throwaway-DB integration test for the new repo query.

## 🔑 Key design decisions (settled)

1. **The pluggable interface is a `BackfillStrategy` Protocol in `core/` (AD-13, AC-1/AC-5).** Define `BackfillStrategy` in the new `fintin/core/backfill.py` with a single method `company_facts(cik: int) -> Iterable[FactLike]` (and a `name: str` for reporting). `FactLike` already lives in `core/ingest.py`, so the Protocol keeps `core` `edgar`-free. The **per-company** implementation `CompanyFactsStrategy` lives in `fintin/adapters/edgar/backfill.py` and wraps the existing `fetch_company_facts(edgar_client, cik)`. The **bulk `companyfacts.zip`** strategy is a *second future implementation of the same Protocol* — explicitly **not built** here (AC-5 is interface-level: prove the engine takes any `BackfillStrategy`). This resolves the "switch without redesign" AC by construction.

2. **The engine is pure `core`; the CLI is a dumb trigger (AD-2).** `backfill_universe(...)` in `core/backfill.py` owns the loop, the per-company skip, the failure-record-and-continue policy, and the throttle-abort policy. It calls the existing pure `ingest_company(...)` per company (fetch→transform→insert via injected ports). `fintin backfill` in `cli/app.py` only wires concrete adapters and renders — no ingestion logic in the CLI.

3. **Resume = per-company store membership, no checkpoint (AD-1/AD-11/AD-16).** Add `present_ciks(client, *, ciks) -> set[int]` to `raw_fact_repo` (`SELECT DISTINCT cik FROM raw_fact WHERE cik IN %(ciks)s`, parameterized, no `FINAL`). The engine **skips CIKs already present before fetching** — skipping the *fetch*, not just the insert, is what honors SM-C1 (a restart must not re-download hundreds of companies). This is a **per-company extension of the same AD-16 membership authority** Story 2.2 established (`present_accessions`), **not a second reconciler** — backfill does **not** re-implement `compute_work_list`. Per-company idempotency (`ReplacingMergeTree(version)` + `next_ingest_version`) makes a re-touched company a read no-op, so the skip is a request-minimizing optimization on top of an already-idempotent commit.

4. **Backfill is the full-history-from-empty path; the 2.2 reconciler is the catch-up path (AD-13 division of labour).** Story 2.2's index-based work-list is a *bounded-lookback incremental* mechanism (its own docstring defers full-history-from-empty to this story). Backfill uses `companyfacts` (one request returns a company's entire history) — **not** `fetch_work_candidates`/`compute_work_list`. Restatements to an already-present company are caught later by Epic 3 catch-up (which reuses the 2.2 reconciler), **not** by re-backfilling.

5. **Throttle aborts; everything else is a recorded gap (SM-2 vs SM-C1).** Core stays `edgar`-free yet still enforces "throttle = stop" via an injected `fatal_errors: tuple[type[BaseException], ...]` — the CLI passes `(EdgarThrottleError,)`. The engine re-raises `fatal_errors`; it catches every other per-company `Exception` (including `NoCompanyFactsError`) into a `BackfillFailure(cik, reason)` and continues.

6. **One `EdgarClient`, one ingest `version`, per run.** Construct `EdgarClient(cfg)` **once** and reuse it across all companies — a second construction mutates process-global edgar state (last-writer-wins; deferred item). Compute `version = next_ingest_version(client)` **once per run** and pass it to every `ingest_company` call; CIK identity keys are disjoint across companies (accession embeds the filer), so a shared per-run version cannot collide, and a later resume run gets a higher `max(version)+1` that supersedes correctly.

7. **No new DDL, no new config section.** Backfill only *inserts* through the existing `raw_fact_repo`; `schema.py` remains the sole DDL owner (AD-18) and is unchanged. It is a **precondition** that `fintin schema-init` has already created Tier 0/Tier 1/MV/mart (AD-18: MVs do not backfill pre-existing rows). No `[backfill]` config is added — there is only one strategy in v1, so a selector would be dead config; strategy-selection-by-Universe-size arrives with the bulk strategy.

## Tasks / Subtasks

- [ ] **Task 1 — Pure backfill engine + strategy interface** (AC: 1, 2, 3, 4, 5) — `fintin/core/backfill.py` (NEW, pure)
  - [ ] Define `BackfillStrategy` Protocol (`@runtime_checkable`): attr `name: str`, method `company_facts(cik: int) -> Iterable[FactLike]`. Import `FactLike` from `fintin.core.ingest` (no `edgar`).
  - [ ] Define `BackfillFailure(NamedTuple)`: `cik: int`, `reason: str` (mirrors `UniverseGap`).
  - [ ] Define `BackfillEvent(NamedTuple)`: `cik: int`, `outcome: str` (`"ingested"|"skipped"|"failed"`), `index: int` (1-based), `total: int` — for the injected progress callback.
  - [ ] Define `BackfillReport(NamedTuple)`: `ingested: tuple[IngestResult, ...]`, `skipped: tuple[int, ...]`, `failures: tuple[BackfillFailure, ...]`, `version: int`; convenience properties `attempted`, `companies_ingested`, `companies_skipped`, `companies_failed`, `rows_landed` (sum of `r.rows_landed`).
  - [ ] Implement `backfill_universe(ciks, *, strategy, insert_rows, taxonomy_version, version, already_present=frozenset(), fatal_errors=(), on_company=None) -> BackfillReport`.
    - [ ] Iterate `sorted(set(ciks))` (deterministic, dedup — kboss determinism).
    - [ ] For a CIK in `already_present`: record in `skipped`, emit a `"skipped"` event, **do not call the strategy** (assert-testable: no fetch for skipped).
    - [ ] Else call `ingest_company(cik, fetch_facts=strategy.company_facts, insert_rows=insert_rows, taxonomy_version=taxonomy_version, version=version)`; record the `IngestResult` in `ingested`; emit `"ingested"`.
    - [ ] `except fatal_errors: raise` (throttle → abort whole run; AC-4).
    - [ ] `except Exception as exc:` append `BackfillFailure(cik, reason=f"{type(exc).__name__}: {exc}")`; emit `"failed"`; **continue** (AC-3).
    - [ ] Call `on_company(event)` if provided (pure — core does no I/O itself).
  - [ ] Reuse the existing `ingest_company` — do **not** duplicate fetch/transform/insert logic.
- [ ] **Task 2 — Per-company `companyfacts` strategy adapter** (AC: 1, 6) — `fintin/adapters/edgar/backfill.py` (NEW)
  - [ ] `class CompanyFactsStrategy`: `name = "per-company"`; `__init__(self, client: EdgarClient)`; `company_facts(self, cik) -> Iterable[FactLike]` returns `fetch_company_facts(self._client, int(cik))`.
  - [ ] `edgar` import stays confined to `adapters/edgar/`; reuse `fetch_company_facts` (do NOT call `edgar.get_company_facts` here directly).
  - [ ] `NoCompanyFactsError` (raised by `fetch_company_facts` on EDGAR's `None`) propagates to the engine, which records it as a gap.
- [ ] **Task 3 — Per-company membership query** (AC: 2) — `fintin/adapters/store/raw_fact_repo.py` (MOD)
  - [ ] `present_ciks(client, *, ciks: Collection[int]) -> set[int]`: `SELECT DISTINCT cik FROM raw_fact WHERE cik IN %(ciks)s` (parameterized — never string-interpolated), **no `FINAL`** (membership = existence). Empty `ciks` → `set()` with **no query**. Return a set of ints.
- [ ] **Task 4 — `backfill` CLI trigger** (AC: 1, 2, 3, 4) — `fintin/cli/app.py` (MOD)
  - [ ] Add `@app.command("backfill")` with `--config/-c`, `--refresh` (re-ingest present companies too), `--show-gaps` (list each recorded failure).
  - [ ] Lazy-import `edgar`-touching modules inside the body (keep `--help`/config paths fast), as `ingest-company`/`work-list` do.
  - [ ] `load_config` → `ConfigError` **exit 2**. Missing `[universe]` → **exit 2**.
  - [ ] Construct `EdgarClient(cfg)` **once** → `EdgarConfigError` **exit 2**.
  - [ ] `resolve_universe(cfg.universe, resolve_tickers=resolve_tickers)`; empty `.ciks` → **exit 1**.
  - [ ] `check_connection(cfg.clickhouse)` → `StoreConnectionError` **exit 1**.
  - [ ] `client = get_client(...)` in a `try/finally` (close in `finally` via `contextlib.suppress`); `version = next_ingest_version(client)`; `present = set()` if `--refresh` else `present_ciks(client, ciks=resolved.ciks)`.
  - [ ] `strategy = CompanyFactsStrategy(edgar_client)`; call `backfill_universe(resolved.ciks, strategy=strategy, insert_rows=lambda rows: insert_raw_facts(client, rows), taxonomy_version=edgartools_version(), version=version, already_present=present, fatal_errors=(EdgarThrottleError,), on_company=<log line>)`.
  - [ ] `EdgarThrottleError` → red "EDGAR throttled, backfill aborted" **exit 1**. Generic `Exception` → "Backfill failed: …" **exit 1**. **Never** print a traceback.
  - [ ] On completion → **exit 0** even with recorded gaps (SM-2: gaps are explained, not run failure). Green summary: companies ingested + rows, skipped-already-present, failed count; if `--show-gaps`, print each `(cik, reason)`. Note that `EdgarClient` holds no closeable resource (only the ClickHouse client is closed).
- [ ] **Task 5 — Tests (offline; NFR-7)** (AC: 6)
  - [ ] `tests/test_backfill.py` (NEW, pure): fake `BackfillStrategy` (returns constructed `FactLike` stubs / `[]` / raises); fake `insert_rows` capturing rows. Assert: deterministic `sorted` iteration; **skipped CIKs never call the strategy**; a raising company is recorded as a `BackfillFailure` and the loop continues; `fatal_errors` (a stub throttle type) **aborts** the whole run; `version` is threaded to `ingest_company`; report counts (`ingested`/`skipped`/`failed`/`rows_landed`); `on_company` events fire with correct `index`/`total`/`outcome`. **AST purity guard**: `core/backfill.py` imports no `edgar`/`clickhouse`/`pyarrow` (reuse the `tests/test_reconcile.py`/`test_universe.py` pattern).
  - [ ] `tests/test_edgar_backfill.py` (NEW, offline): `CompanyFactsStrategy.company_facts` via a fake `EdgarClient` (`.run(op)` → `op()`) with `edgar.get_company_facts` monkeypatched to return a parsed `EntityFacts` (or directly-constructed `FinancialFact`s); assert facts flow through and `NoCompanyFactsError` propagates when the monkeypatch returns `None`. **No network.**
  - [ ] `tests/test_raw_fact_repo.py` (MOD, `@pytest.mark.integration`, throwaway DB): `present_ciks` — empty input → `set()` no query; returns the present subset; distinct; absent CIKs excluded.
  - [ ] `tests/test_cli.py` (MOD): `backfill` error paths assert exit codes (config→2, edgar-config→2, missing-universe→2, empty-universe→1, connection→1) and **no `Traceback`** in output. The network happy-path is **not** CLI-tested (NFR-7), matching `ingest-company`/`work-list`.
- [ ] **Task 6 — Validate & document** (AC: all)
  - [ ] `uv run pytest` — full suite green; record the count + delta.
  - [ ] Update `README.md`: a "Backfill the Universe" section (`fintin schema-init` precondition → `fintin backfill` → `--refresh`/`--show-gaps`; resumable, ban-safe).
  - [ ] `fintin.toml.example` needs **no** change (no new config).
  - [ ] Append 2.3 deferred items to `_bmad-output/implementation-artifacts/deferred-work.md`.
  - [ ] (Optional) One manual live smoke on a 2–3 CIK Universe using a scratchpad-only untracked config with a real email; removed after. **Never commit it.**

## Dev Notes

### What this story IS
The multi-company **backfill** that populates the store from empty: loop the resolved Universe, fetch each company's full history via one `companyfacts` request, commit per company, skip companies already present (resumable), record per-company failures as explained gaps, and abort only on throttle. It composes primitives that already exist (`ingest_company`, `fetch_company_facts`, `resolve_universe`, `next_ingest_version`, `insert_raw_facts`) behind a new pluggable `BackfillStrategy` interface.

### What this story is NOT (scope fences — do not implement)
- ❌ **No bulk `companyfacts.zip` strategy** — AC-5 is *interface-level only*; per-company API is the sole v1 impl (AD-13; deferred).
- ❌ **No single-flight lease / heartbeat (AD-12)** — Epic 3 (Story 3.2). A long backfill will block on uninterruptible cool-downs until the lease lands; that is accepted for v1 (deferred).
- ❌ **No catch-up / work-list / reconciler changes** — backfill does not touch `core/reconcile.py` or `filings_index.py`; those are the Epic 3 catch-up path.
- ❌ **No DDL / schema / config changes** — insert-only through the existing repo; `schema-init` is a precondition.
- ❌ **No coverage/status report** — that is Story 2.4 (`fintin status`). This story only *produces* the substrate 2.4 reads (rows in `raw_fact` + a run summary of gaps).

### Current substrate (Epic 1 + Stories 2.1/2.2, on `main`)
Reuse — do not reinvent (all verified present):
- `fintin/core/ingest.py` — `ingest_company(cik, *, fetch_facts, insert_rows, taxonomy_version, version=None) -> IngestResult` (pure, injected ports); `to_raw_fact_rows(...)`; `RawFactRow` (14 fields, schema order); `IngestResult(cik, facts_seen, rows_landed, dropped_*, deduped, version)` + `.dropped`; `FactLike` Protocol; `normalize_accession`, `_ACCESSION_RE`, `content_hash`, `STANDARD_TAXONOMIES`.
- `fintin/adapters/edgar/facts.py` — `fetch_company_facts(client, cik) -> EntityFacts` (through `EdgarClient.run`, the ONLY `edgar.get_company_facts` caller); `NoCompanyFactsError` when EDGAR returns `None`; `edgartools_version()`.
- `fintin/adapters/edgar/client.py` — `EdgarClient(cfg)` (constructs once; ban-safety email gate); `.run(op, *, description=...)` (cool-down/retry, raises `EdgarThrottleError` when exhausted); `EdgarConfigError`. No `.close()` (holds no resource).
- `fintin/adapters/store/raw_fact_repo.py` — `insert_raw_facts(client, rows) -> int` (owns all writes; empty → 0); `next_ingest_version(client) -> int` (`max(version)+1`); `high_water_mark`; `present_accessions(*, accessions)`. **Add `present_ciks(*, ciks)` here.**
- `fintin/core/universe.py` — `resolve_universe(universe, *, resolve_tickers) -> ResolvedUniverse` (pure; `.ciks` sorted/deduped, `.gaps`). Its docstring already names Story 2.3 as its caller.
- `fintin/adapters/edgar/universe.py` — `resolve_tickers(tickers)` (offline bundled parquet; no network).
- `fintin/adapters/store/client.py` — `get_client(cfg)`, `check_connection(cfg) -> str`, `StoreConnectionError`.
- CLI (`fintin/cli/app.py`) — existing `ingest-company` (single-CIK) and `work-list` commands are the exact wiring/exit-code templates to mirror.

### edgartools 5.43.0 — verified companyfacts facts (do not re-investigate)
- `edgar.get_company_facts(cik: int) -> EntityFacts | None` downloads `https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010}.json` through edgar's throttled client (so `TooManyRequestsError`/throttle is possible → already wrapped by `EdgarClient.run` inside `fetch_company_facts`). **Returns `None` (does not raise)** on unknown-CIK / no-XBRL / unparseable → surfaces as `NoCompanyFactsError`.
- One call returns **a company's entire standard-taxonomy history** across all filings → backfill is "one request per company," the request-minimizing per-company strategy (SM-C1). companyfacts returns **consolidated** values only — it never emits dimensioned facts (the dimensioned-drop branch is already covered in `to_raw_fact_rows` tests).
- `EntityFacts` is iterable (`__iter__` yields `FinancialFact`), satisfying `FactLike` structurally — `core` needs no `edgar` import.
- Offline test parse (no network): `edgar.entity.parser.EntityFactsParser.parse_company_facts(json_dict) -> EntityFacts`; companyfacts JSON shape `{"cik":…, "entityName":…, "facts": {"us-gaap": {"<Tag>": {"label":…, "units": {"USD": [{"start":…, "end":…, "val":…, "accn":"0000…-..-……", "form":"10-K", "filed":"YYYY-MM-DD"}]}}}}}`.

### Architecture constraints (authoritative)
- **AD-13** — backfill is one interface with swappable strategies selected by Universe size; v1 = per-company `companyfacts` API; bulk `companyfacts.zip` deferred. [ARCHITECTURE-SPINE.md#AD-13]
- **AD-11 / FR-10** — commit at **per-company** grain (never one final write); resume-after-crash re-derives the gap from the DB (membership); **no checkpoint file**. [#AD-11]
- **AD-16** — per-*entity* membership is the correctness authority; here it is per-company presence in `raw_fact` (does this CIK already have rows?). [#AD-16]
- **AD-1** — derive state at run time, never persist a cursor/checkpoint/ledger. [#AD-1]
- **AD-3 / FR-1** — all EDGAR access through the one rate-limited client; every `companyfacts` fetch runs through `EdgarClient.run`. [#AD-3]
- **AD-2 / FR-12** — pure engine, dumb trigger: the backfill engine is pure `core`; `fintin backfill` is a dumb CLI trigger; throttle policy lives in the engine/adapter, never the trigger. [#AD-2]
- **AD-6** — Tier 0 is `ReplacingMergeTree(version)` on the identity key; `version` is ingest-monotonic (`next_ingest_version`); all writes are inserts; readers use `FINAL`/`argMax`. Idempotent re-ingest supersedes on read. [#AD-6]
- **AD-18** — schema-init creates Tier 0/Tier 1/MV/mart **before** any backfill insert (MVs don't backfill pre-existing rows). Backfill assumes it has run. [#AD-18]
- **SM-2 / Errors & status** — a per-company problem is a **recorded explained gap, never a silent omission**; the run continues; run vocabulary is exit-0. [#Errors]
- **SM-C1 vs NFR-3** — fewer/slower requests are always preferred to a ban; the "single unattended session" target is **soft** and never traded against rate-limit compliance. Throttle-give-up aborts the run. [prd.md#SM-C1]
- **NFR-7** — EDGAR-touching code tested via fixtures/injection; **never** live EDGAR. [#Testing]

### Resumability: how AD-11 + AD-16 + AD-1 combine (the load-bearing design)
No checkpoint exists anywhere (no `failures`/`ingest_run`/`backfill_state` table — confirmed absent from `schema.py`). Each run:
1. Derives scope from config (`resolve_universe`).
2. Derives "already done" from the store (`present_ciks` over the scope) — the only source of resume truth.
3. Ingests each **not-yet-present** company in one atomic per-company insert (a single company's rows fit one ClickHouse block → all-or-nothing; a CIK is therefore either fully present or absent, so skipping a present CIK is safe).
4. On restart, steps 1–2 re-derive the remaining gap — nothing was persisted to drift (AD-1).

This is a **per-company** extension of Story 2.2's membership authority (`present_accessions` was per-accession for the incremental work-list). It is **not** a second resumability mechanism and **not** a re-implementation of `compute_work_list` — backfill's full-history-from-empty path is deliberately distinct from the reconciler's bounded-lookback catch-up path (AD-13). Idempotency (AD-6) already makes re-ingest a read no-op; the `present_ciks` skip adds request-minimization (SM-C1) so a restart doesn't re-download completed companies. `--refresh` bypasses the skip to re-ingest (idempotently) when a company must be refreshed.

### Failure handling → explained gaps (feeds Story 2.4)
Per SM-2, the engine collects `BackfillFailure(cik, reason)` in-run (mirroring Story 2.1's `UniverseGap(identifier, reason)`) and surfaces them in the run summary — **no failures table** (AD-1 forbids a persisted ledger). A company that returns no facts (`NoCompanyFactsError`) or errors mid-transform is recorded, not fatal. Story 2.4's `fintin status` reconstructs "zero-fact" gaps directly from the DB (in-scope CIKs absent from `raw_fact`); this story's contribution to coverage is (a) the ingested rows and (b) a run summary of recorded failures. Only `EdgarThrottleError` (throttle retries exhausted) is fatal — it aborts to protect against a ban (SM-C1).

### Project Structure Notes
- **New:** `fintin/core/backfill.py` (pure engine + `BackfillStrategy` Protocol), `fintin/adapters/edgar/backfill.py` (`CompanyFactsStrategy`).
- **Modified:** `fintin/adapters/store/raw_fact_repo.py` (`present_ciks`), `fintin/cli/app.py` (`backfill` command), `README.md`.
- **New tests:** `tests/test_backfill.py`, `tests/test_edgar_backfill.py`. **Extended:** `tests/test_raw_fact_repo.py`, `tests/test_cli.py`.
- Hexagonal invariant holds: `core/backfill.py` imports no `edgar`/ClickHouse/`pyarrow` (AST-guarded); `edgar` stays inside `adapters/edgar/`; `adapters/store/` owns all ClickHouse; `cli/` is a dumb trigger. `snake_case` modules; one adapter package per port.

### Previous Story Intelligence (Epic 1 + 2.1/2.2)
- **`version` from the store, not the clock (1.4 review):** always thread `next_ingest_version(client)` — never `time.time_ns()` — so a resume supersedes correctly (AD-6). Compute once per run.
- **`max()` on empty ClickHouse returns `1970-01-01`, not NULL (2.2):** `present_ciks` uses existence (`SELECT DISTINCT`), so this pitfall doesn't apply — but do not switch it to a `max()`-style guard.
- **Parameterize IN-clauses (2.2):** `WHERE cik IN %(ciks)s` via clickhouse-connect params (it supports lists) — never string-interpolate.
- **Construct `EdgarClient` once (1.3 deferred #2):** per-company construction would reset process-global edgar rate state (last-writer-wins). The backfill loop builds it once and reuses it.
- **CLI house style (2.2):** lazy imports in the body; `typer.secho(..., fg=RED, err=True)` + `raise typer.Exit(code=…)`, never a traceback; close the ClickHouse client in `finally` via `contextlib.suppress`; error paths CLI-tested, network happy-path not (NFR-7).
- **Offline-proof socket blocking (1.4/1.5):** if asserting a path is offline, block `socket.socket.connect`/`create_connection` **after** SSL-needing imports; never `socket.socket = None`; never block sockets around ClickHouse-touching code (localhost is a socket). Backfill's engine test needs no sockets at all (pure data + fakes).
- **Determinism (kboss):** iterate `sorted` CIKs; report tuples in stable order.

### Public repo / security (hard constraints)
⚠️ **Public repo:** never write a real contact email or any PII/secret into a tracked file (story, code, tests, `README.md`, `fintin.toml.example`). Examples use `you@example.com`; the operator's real contact email lives only in the gitignored `fintin.toml` and is the ban-safety gate `EdgarClient` enforces. Tickers/CIKs are public and safe. Any live smoke uses a scratchpad-only, untracked config, removed after — never committed.

### References
- [Source: _bmad-output/planning-artifacts/epics.md#Story-2.3 — ACs; Epic 2 "build resumability once" refinement; FR-2/FR-7/FR-10/NFR-3]
- [Source: _bmad-output/planning-artifacts/prds/prd-fin-tin-2026-07-23/prd.md — FR-7, FR-10, FR-14, NFR-3, SM-2, SM-3, SM-C1]
- [Source: _bmad-output/planning-artifacts/architecture/architecture-fin-tin-2026-07-23/ARCHITECTURE-SPINE.md#AD-1,2,3,6,11,13,16,18; source tree (cli: catch-up/backfill/status); Errors & status (per-company failure recorded, not fatal)]
- [Source: _bmad-output/planning-artifacts/architecture/architecture-fin-tin-2026-07-23/BUILD-SPLIT.md#Epic-D — per-company backfill strategy behind the pluggable interface]
- [Source: _bmad-output/implementation-artifacts/1-4-land-raw-facts.md — `ingest_company`, `to_raw_fact_rows`, `next_ingest_version`, `NoCompanyFactsError`, offline test patterns]
- [Source: _bmad-output/implementation-artifacts/2-1-resolve-universe-from-config.md — `resolve_universe`/`UniverseGap`, AST guard, offline-proof sockets, public-repo rule]
- [Source: _bmad-output/implementation-artifacts/2-2-db-derived-work-list.md — membership authority (AD-16), `present_accessions`, CLI/exit conventions, the explicit hand-off "full history from empty is Story 2.3's per-company backfill"]
- [Source: _bmad-output/implementation-artifacts/deferred-work.md — #2 EdgarClient singleton, #3 sync-only run(), #4 uninterruptible cool-down, #10 next_ingest_version non-atomic, #17 cik UInt32, #18 20-F/40-F]

## Dev Agent Record

### Agent Model Used

### Debug Log References

### Completion Notes List

### File List

## Change Log

- 2026-07-24 — Story 2.3 drafted via create-story (3 parallel research agents: architecture spine + PRD, code inventory, prior-story learnings). Design settled: pure `backfill_universe` engine + `BackfillStrategy` Protocol in `core/backfill.py`; `CompanyFactsStrategy` in `adapters/edgar/backfill.py`; `present_ciks` per-company membership for checkpoint-free resume (AD-1/AD-11/AD-16); throttle-abort vs recorded-gap policy (SM-C1/SM-2); one `EdgarClient` + one `version` per run; no DDL/config change. Status → ready-for-dev.
