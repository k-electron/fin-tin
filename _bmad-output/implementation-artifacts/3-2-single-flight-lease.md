---
baseline_commit: 47688f1326444a1bfea911e2d156152ffba0669a
---

# Story 3.2: Single-flight self-expiring lease

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the builder,
I want at most one ingestion run at a time, guarded by a self-expiring lease,
so that concurrent triggers can't double the EDGAR rate (a ban) or deadlock the tool after a crash.

## Acceptance Criteria

1. **Given** a run is active and heartbeating **When** a second trigger fires **Then** it returns **`ALREADY_RUNNING` (exit-0)** and **issues no EDGAR requests** — coalesce, don't queue (FR-11, AD-12, NFR-7). The lease is a **filesystem** lock file (path from config), **not** ClickHouse (AD-1/AD-18 untouched — no new table).
2. **Given** a run crashes holding the lease **When** a later trigger sees the **expired** lease (heartbeat older than TTL) **Then** it **reclaims** it and resumes the partial work — resumption is the existing DB-derived work list (AD-16), not lease-stored state, so "resume" falls out for free.
3. **Given** a run is inside an EDGAR cool-down (AD-3) **Then** it **keeps heartbeating** so its lease is not reclaimed mid-cool-down — a **background heartbeat thread** beats while the main thread is blocked in the cool-down sleep, so this needs no change to the EdgarClient's cool-down policy.
4. **Given** the run's status vocabulary **Then** `STARTED` / `ALREADY_RUNNING` / `NOTHING_TO_DO` / `COMPLETED` are **all exit-0**. `ALREADY_RUNNING` is the 4th `CatchUpStatus` member (3.1 built the enum for exactly this).
5. **Given** the hexagonal architecture **Then** the single-flight guard lives **in the engine, not the trigger** (AD-2): `core/` defines a `Lease` **port** (Protocol) + a pure `run_single_flight` combinator; `adapters/lease/` implements the **filesystem** lease. The CLI is a dumb trigger that builds the concrete lease and hands it in.
6. **Given** ban-safety is the whole point **Then** **both** ingestion runs — `catch-up` **and** `backfill` — acquire the **same** lease (shared path → mutual exclusion), so a backfill and a catch-up can't run concurrently and double the rate either (SM-C1). This also lands the deferred story-1.3/2.3 "no lease / uninterruptible cool-down" items.
7. **Given** the offline test suite **Then** it covers the filesystem lease (real temp file, fast TTL/heartbeat — no EDGAR, no ClickHouse), the pure `run_single_flight` guard (fake lease), `[lease]` config parsing, and the CLI `ALREADY_RUNNING` wiring for both commands **proving no EDGAR request on coalesce** (NFR-7).

## 🔑 Key design decisions (settled)

1. **Filesystem lease, not ClickHouse (AD-12; epics.md §"Single-flight lease").** The lease is a local lock file (path from config, default `fintin.lease` in the CWD) — the architecture is explicit: *"a filesystem lease file (path from config) with heartbeat ≪ TTL; a run in EDGAR cool-down keeps heartbeating; NOT stored in ClickHouse."* AD-1 explicitly carves this out: *"The only permitted operational state is the single-flight lease (AD-12)."* So **no `schema.py` change, no new table** (AD-18 untouched). The lease file is gitignored (runtime state).
2. **Hexagonal: `core` lease PORT + `adapters/lease` filesystem impl (SPINE source tree; AD-2/AD-12).** `fintin/core/lease.py` (NEW, pure) defines a `Lease` Protocol (`acquire() -> bool`, `release() -> None`) + a pure `run_single_flight(lease, run) -> T | None` combinator. `fintin/adapters/lease/file_lease.py` (NEW) implements the filesystem lease. The single-flight guard lives **in the engine** (AD-2: "throttle and single-flight live inside the engine, never in a trigger"); the CLI just constructs the concrete `FileLease` and passes it in.
3. **Acquire the lease BEFORE any EDGAR request — coalesce with zero EDGAR (AC-1).** Discovery (`fetch_work_candidates`) hits EDGAR's index, so the lease must be acquired *before* discovery. `run_single_flight(lease, run)` acquires first; if a live lease is held it returns `None` **without ever invoking `run`** — so a coalesced trigger issues **no** EDGAR request (not the index, not `companyfacts`). The cheap offline pre-flight (config, Universe resolve, EdgarClient construction which makes no request, ClickHouse ping) may run before coalescing — none of it touches EDGAR.
4. **Background heartbeat thread — beats through the EDGAR cool-down (AC-3).** On `acquire()`, `FileLease` starts a **daemon thread** that atomically rewrites the lease file's `heartbeat_at` every `heartbeat_seconds`. Because it's a *separate* thread, it keeps beating while the main thread is blocked in the EdgarClient's `>=10-min` cool-down sleep — so the lease never goes stale mid-cool-down (AC-3) **without touching the cool-down policy** (the story-1.3 forward-hook comment stays; the thread is the mechanism). `release()` stops the thread and deletes the file (only if still ours). This also resolves the story-1.3 "uninterruptible blocking cool-down" and story-2.3 "long backfill has no lease" defers.
5. **Atomic acquire + stale reclaim (AC-1, AC-2).** `acquire()`: create the lease file with `os.open(..., O_CREAT|O_EXCL)` (atomic — only one racer wins a free lease). On `FileExistsError`, read the record: if `now - heartbeat_at > ttl` (or the file is unparseable/corrupt) → **stale** → `os.unlink` it and retry the exclusive create (a crashed holder is reclaimed); else → a **live** lease is held → return `False` (coalesce). The record is JSON `{token, pid, host, acquired_at, heartbeat_at, ttl_seconds}`; `token` = a per-acquire uuid so `release()`/heartbeat only act while we still own it. Writes are atomic (temp + `os.replace`) so a reader never sees a partial file.
6. **Reclaim "resumes partial work" for free (AC-2, AD-1/AD-16).** The lease stores **no work state** (AD-1). After reclaiming a crashed run's lease, `run` re-derives the outstanding work from the DB + EDGAR index (the Story 2.2 reconciler): the crashed run's already-committed accessions are now present → dropped; the remainder is re-fetched. So "resume the partial work" is just the existing resumability — the lease only governs *who may run*, never *what's left*.
7. **`ALREADY_RUNNING` = the 4th `CatchUpStatus` member; all exit-0 (AC-4).** 3.1 built `CatchUpStatus` as a plain `enum.Enum` for exactly this. `catch_up_single_flight(lease, run) -> CatchUpReport` wraps `run_single_flight`, mapping the `None` (coalesced) case to `CatchUpReport(status=ALREADY_RUNNING, …)`. The CLI renders `ALREADY_RUNNING` GREEN, exit 0.
8. **Both `catch-up` and `backfill` share the one lease (AC-6, SM-C1).** Both are EDGAR-heavy ingestion runs; guarding both with the *same* `cfg.lease.path` makes them mutually exclusive, closing the backfill×catch-up concurrency window (the real ban risk the lease exists to prevent). `catch-up` renders `ALREADY_RUNNING` via `CatchUpStatus`; `backfill` renders its own `ALREADY_RUNNING` line (it has a `BackfillReport`, no status enum) — both exit-0. `run_single_flight` is generic (returns `None` on coalesce) so both reuse it.
9. **Config `[lease]` with a safe default; heartbeat ≪ TTL enforced.** `LeaseConfig(path: str = "fintin.lease", ttl_seconds: int = 120, heartbeat_seconds: int = 15)`, always populated (default when `[lease]` absent, like `[reconcile]`). Validation: `path` a non-empty string; `ttl_seconds >= 2`; `heartbeat_seconds >= 1`; **`2 * heartbeat_seconds <= ttl_seconds`** (≥2 beats per TTL window — enforces "≪", rejects a foot-gun config where a single missed beat expires the lease). TTL need not exceed the cool-down: the heartbeat thread beats *through* it.

## Tasks / Subtasks

- [x] **Task 1 — Pure lease port + single-flight combinator** (AC: 4, 5) — `fintin/core/lease.py` (NEW, pure)
  - [x] `@runtime_checkable class Lease(Protocol)`: `def acquire(self) -> bool: ...` (True = acquired/reclaimed; False = a live lease is held), `def release(self) -> None: ...`.
  - [x] `def run_single_flight(lease: Lease, run: Callable[[], T]) -> T | None`: `if not lease.acquire(): return None`; then `try: return run() finally: lease.release()`. **`run` is only invoked after a successful acquire** (so a coalesced trigger runs no side effects — the AC-1 no-EDGAR guarantee). Generic (`T`) so both catch-up and backfill reuse it.
  - [x] Import only stdlib (`typing`: `Protocol`, `runtime_checkable`, `TypeVar`, `Callable`). No `edgar`/ClickHouse/`pyarrow`.
- [x] **Task 2 — `CatchUpStatus.ALREADY_RUNNING` + `catch_up_single_flight`** (AC: 4, 7) — `fintin/core/catchup.py` (MOD)
  - [x] Add `ALREADY_RUNNING = "ALREADY_RUNNING"` to `CatchUpStatus` (between `STARTED` and `NOTHING_TO_DO`). Update the module docstring (it no longer says "Story 3.2 adds it" — it's here now).
  - [x] `def catch_up_single_flight(lease: Lease, run: Callable[[], CatchUpReport]) -> CatchUpReport`: `result = run_single_flight(lease, run)`; `return result if result is not None else CatchUpReport(status=CatchUpStatus.ALREADY_RUNNING, scanned=0, outstanding=0, companies=0, backfill=None)`. Import `Lease`/`run_single_flight` from `fintin.core.lease` (stays pure).
- [x] **Task 3 — Filesystem lease adapter** (AC: 1, 2, 3) — `fintin/adapters/lease/file_lease.py` (NEW)
  - [x] `class FileLease` (satisfies `Lease` structurally). `__init__(self, path: str | Path, *, ttl_seconds: float, heartbeat_seconds: float)`. Holds a per-instance `self._token = uuid4().hex`, a `threading.Event` stop flag, and the heartbeat thread handle.
  - [x] `acquire() -> bool`: `os.makedirs(parent, exist_ok=True)`; loop (bounded ~3): `try: fd = os.open(path, O_CREAT|O_EXCL|O_WRONLY, 0o600)` → write our record, close, start the heartbeat thread, return `True`. `except FileExistsError:` read+parse the record; if `now - heartbeat_at > ttl` **or** unparseable → `os.unlink(path)` (suppress `FileNotFoundError`) and retry; else (fresh) → return `False`. Return `False` if the loop exhausts (someone reclaimed between our unlink and create — coalesce).
  - [x] Record = JSON `{token, pid, host, acquired_at, heartbeat_at, ttl_seconds}`; all writes atomic (write a `<path>.<token>.tmp` then `os.replace` onto `path`) so a concurrent reader never sees a partial file.
  - [x] Heartbeat thread: loop until the stop event is set, sleeping `heartbeat_seconds`, then rewriting the record with a fresh `heartbeat_at` — **only if we still own it** (re-read; token matches). Daemon thread (dies with the process on a hard crash → the file goes stale → reclaimable).
  - [x] `release() -> None`: set the stop event, `join` the thread (short timeout), then `os.unlink(path)` **only if the on-disk token is still ours** (don't delete a lease another run reclaimed after we lost it). Suppress `FileNotFoundError`. Idempotent.
  - [x] Optional context-manager sugar (`__enter__`/`__exit__`) is NOT required — `run_single_flight` calls `acquire`/`release` explicitly.
- [x] **Task 4 — `[lease]` config** (AC: 1, 9) — `fintin/config.py` (MOD)
  - [x] `DEFAULT_LEASE_PATH = "fintin.lease"`, `DEFAULT_LEASE_TTL_SECONDS = 120`, `DEFAULT_LEASE_HEARTBEAT_SECONDS = 15`. `@dataclass(frozen=True) class LeaseConfig`: `path: str = DEFAULT_LEASE_PATH`, `ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS`, `heartbeat_seconds: int = DEFAULT_LEASE_HEARTBEAT_SECONDS`.
  - [x] Add `lease: LeaseConfig = LeaseConfig()` to `Config` (always populated, like `reconcile`). Parse `[lease]` in `load_config` (`_parse_lease`): reject a non-table section; `path` must be a non-empty string; `ttl_seconds`/`heartbeat_seconds` non-bool ints (`ttl >= 2`, `heartbeat >= 1`); enforce `2 * heartbeat_seconds <= ttl_seconds` with a clear error. Absent `[lease]` → the default `LeaseConfig()`.
- [x] **Task 5 — Wire single-flight into both CLI triggers** (AC: 1, 4, 6) — `fintin/cli/app.py` (MOD)
  - [x] `catch-up`: build `lease = FileLease(cfg.lease.path, ttl_seconds=cfg.lease.ttl_seconds, heartbeat_seconds=cfg.lease.heartbeat_seconds)` after the offline pre-flight (config/universe/EdgarClient/resolve/`check_connection`). Move the `get_client` + discovery + `catch_up` block into a local `_run() -> CatchUpReport` (client closed in its own `finally`). Call `report = catch_up_single_flight(lease, _run)` inside the existing `try/except` (throttle/BackfillAborted/generic → exit 1). Render `report.status`: **`ALREADY_RUNNING`** → GREEN `"Another run is already active — ALREADY_RUNNING (nothing to do; no EDGAR request issued)."` exit 0; `NOTHING_TO_DO`/`COMPLETED` as before.
  - [x] `backfill`: same pattern — build the lease, move `get_client` + `present_ciks` + `backfill_universe` into `_run() -> BackfillReport`, call `result = run_single_flight(lease, _run)`; `if result is None:` GREEN `"Another run is already active — ALREADY_RUNNING (nothing to do; no EDGAR request issued)."` exit 0; else render the `BackfillReport` as before. Both commands' `_run` closes the client in `finally`.
  - [x] Deferred imports: add `FileLease` (`fintin.adapters.lease.file_lease`), `run_single_flight` (`fintin.core.lease`), and (catch-up) `catch_up_single_flight`.
- [x] **Task 6 — Tests (offline; NFR-7)** (AC: 7) — no live EDGAR, no ClickHouse
  - [x] `tests/test_lease.py` (NEW): **FileLease** (real `tmp_path` file, fast durations): free acquire → True + file exists; second `FileLease.acquire()` on the same fresh lease → False (coalesce); **stale reclaim** — a holder with `heartbeat_seconds` ≫ `ttl_seconds` (won't beat) goes stale after `> ttl`, a second `acquire()` reclaims → True (simulates a crashed run); **heartbeat keeps fresh** — a holder with `heartbeat_seconds ≪ ttl_seconds`, after sleeping `> ttl`, a second `acquire()` still sees it fresh → False (AC-3 mechanism); `release()` deletes the file → a later `acquire()` → True; corrupt/empty lease file → reclaimed; `release()` is idempotent and does not delete a lease reclaimed by another token. **Pure `run_single_flight`** (fake `Lease`): acquire True → `run` invoked once, `release` called (even if `run` raises — assert release on exception); acquire False → returns `None`, `run` **never invoked**, `release` **not** called. **AST purity guard**: `core/lease.py` imports no `edgar`/`clickhouse`/`pyarrow`.
  - [x] `tests/test_catchup.py` (MOD): `catch_up_single_flight` with a fake lease — busy → `CatchUpReport(status=ALREADY_RUNNING, backfill=None)`, `run` not called; free → returns `run()`'s report, `release` called. `ALREADY_RUNNING` is a `CatchUpStatus` member.
  - [x] `tests/test_config.py` (MOD): `[lease]` absent → default `LeaseConfig()`; a valid `[lease]` parses; `2*heartbeat > ttl` → `ConfigError`; non-int ttl/heartbeat, blank path, non-table section → `ConfigError`.
  - [x] `tests/test_cli.py` (MOD): **`catch-up` coalesce** — pre-acquire a real `FileLease` on `cfg.lease.path`, monkeypatch `fetch_work_candidates` to **raise if called** (proving no EDGAR/discovery), invoke `catch-up` → exit 0, "ALREADY_RUNNING" in output, discovery never called; release the holder. **`backfill` coalesce** — same, monkeypatch `backfill_universe`/discovery to raise-if-called, hold the lease → exit 0, "ALREADY_RUNNING". Reuse `_stub_store`/`_EDGAR_VALID`; the config toml sets `[lease] path` under `tmp_path`.
- [x] **Task 7 — Validate & document** (AC: all)
  - [x] `uv run pytest` — full suite green; record count + delta.
  - [x] `fintin.toml.example`: add a `[lease]` section (path/ttl/heartbeat) with comments (filesystem single-flight; NOT ClickHouse; heartbeat ≪ TTL; a crashed run's lease self-expires).
  - [x] `.gitignore`: ignore `fintin.lease` (and any configured lease path is runtime state — note it).
  - [x] `README.md`: a "Single-flight (one run at a time)" note under catch-up/backfill — a second trigger returns `ALREADY_RUNNING` (exit-0, no EDGAR); a crashed run's lease self-expires (TTL) and is reclaimed; a run in EDGAR cool-down keeps heartbeating; the lease is a local file, not ClickHouse.
  - [x] `deferred-work.md`: **resolve** the story-1.3 (uninterruptible cool-down / no lease heartbeat) + story-2.3 (long backfill no lease) + story-3.1 (no-lease-yet) defers — now landed. **Add** the concurrent-reclaim micro-race (two triggers reclaiming the same stale lease at once on a single machine — negligible on a laptop; a fully-robust reclaim needs `fcntl`/`flock`).
  - [x] (Optional) Live smoke: two overlapping `fintin catch-up` runs against the local DB (scratchpad config, real email, removed after) — the second returns `ALREADY_RUNNING` with no EDGAR request.

## Dev Notes

### What this story IS
The **second Epic 3 story**: the AD-12 single-flight self-expiring **filesystem** lease that makes concurrent triggers safe. It introduces a pure `Lease` port + `run_single_flight` combinator in `core`, a `FileLease` adapter (atomic acquire, stale reclaim, background heartbeat), a `[lease]` config block, and wires the guard into **both** ingestion CLIs (`catch-up` + `backfill`) so at most one runs at a time. It adds `ALREADY_RUNNING` (the 4th status). It lands the deferred lease/heartbeat items from stories 1.3, 2.3, and 3.1.

### What this story is NOT (scope fences — do not implement)
- ❌ **No ClickHouse lease table / DDL** — the lease is a **filesystem** file (AD-12); `schema.py` and `adapters/store` are untouched (AD-1/AD-18).
- ❌ **No distributed-consensus / cross-machine locking** — v1 is a single laptop; a filesystem lease is the ratified mechanism. The concurrent-reclaim micro-race is an accepted v1 limitation (documented).
- ❌ **No change to the EdgarClient cool-down policy** — the background heartbeat thread beats *through* the existing blocking sleep (AC-3); the `run()` cool-down/retry logic is unchanged.
- ❌ **No queueing** — coalesce (`ALREADY_RUNNING`), don't queue (AD-12).
- ❌ **No scoped recovery (`recover --cik`)** — that's Story 3.3 (it will reuse this same lease).
- ❌ **No `EdgarClient` singleton enforcement** — the deferred story-1.3 singleton item is separate; this story adds the lease, not client-construction guards.

### Current substrate — reuse, do not reinvent (all verified present)
- `fintin/core/catchup.py` — `CatchUpStatus(enum.Enum)` (STARTED/NOTHING_TO_DO/COMPLETED — **add ALREADY_RUNNING**), `CatchUpReport(status, scanned, outstanding, companies, backfill)`, `catch_up(...)`. The docstring already anticipates this story.
- `fintin/core/backfill.py` — `backfill_universe(...) -> BackfillReport` (`companies_ingested`/`rows_landed`/`failures`); `BackfillAborted`. Unchanged; just wrapped by the lease at the CLI.
- `fintin/adapters/edgar/client.py` — `EdgarClient(config, *, sleep=time.sleep)`; the cool-down `self._sleep(wait)` runs on the **main** thread with an explicit AD-12 forward-hook comment (lines ~223-227). The heartbeat thread is the separate mechanism — no change here.
- `fintin/adapters/lease/__init__.py` — a **placeholder** ("the self-expiring lease lands in Story 3.2"). Put `file_lease.py` alongside it.
- `fintin/config.py` — `Config` (add `lease: LeaseConfig`), `ReconcileConfig`/`_parse_reconcile` are the exact template for `LeaseConfig`/`_parse_lease` (default-when-absent, non-bool-int + range validation). The module docstring already lists `[lease]` as a coming section.
- `fintin/cli/app.py` — `catch-up` (discovery + `catch_up`) and `backfill` (discovery + `backfill_universe`) commands; `_MAX_CONSECUTIVE_FAILURES` (story-3.1 review) is the module constant style to mirror. Both build the `EdgarClient` once, `check_connection`, close the client in `finally`.
- Test patterns: `tests/test_config.py::test_reconcile_absent_uses_default` (config default template); `tests/test_cli.py::_stub_store`/`_EDGAR_VALID`/`_raise` (CLI harness); the `_module_imports` AST-guard helper (in test_backfill/test_catchup/test_coverage).

### The composition (the load-bearing logic)
```
# core (pure): the single-flight policy lives in the engine (AD-2)
def run_single_flight(lease, run):        # generic
    if not lease.acquire(): return None   # coalesce — run NEVER invoked (no EDGAR)
    try: return run()
    finally: lease.release()

# catch-up CLI (dumb trigger): acquire BEFORE discovery
lease = FileLease(cfg.lease.path, ttl_seconds=…, heartbeat_seconds=…)
def _run() -> CatchUpReport:
    client = get_client(...)
    try:  … discovery (hits EDGAR index) + catch_up(...) …
    finally: client.close()
report = catch_up_single_flight(lease, _run)   # ALREADY_RUNNING report if busy
```
A second trigger's `acquire()` returns `False` → `_run` (and thus discovery) is never called → **zero EDGAR requests** (AC-1). A crashed holder's lease goes stale after TTL → the next trigger reclaims and `_run` re-derives the remaining work (AC-2). The heartbeat thread beats through the cool-down sleep (AC-3).

### Architecture constraints (authoritative)
- **FR-11 / AD-12** — single-flight via a self-expiring **filesystem** lease (path from config, default under the data dir); heartbeat ≪ TTL; coalesce (`ALREADY_RUNNING` exit-0), don't queue; expired lease reclaimed + partial work resumed; a run in EDGAR cool-down keeps heartbeating; **NOT stored in ClickHouse**. [SPINE#AD-12; epics.md §Epic-3 refinement; prd FR-11]
- **AD-1** — no persisted cursor/checkpoint/ledger; **the single-flight lease is the one permitted operational state** (explicit carve-out). [SPINE#AD-1]
- **AD-2** — pure engine, dumb trigger; throttle + single-flight live **inside the engine**, never a trigger. [SPINE#AD-2]
- **AD-3** — the EDGAR cool-down (≥10 min) is on the main thread; the lease heartbeat must survive it (→ background thread). [SPINE#AD-3]
- **AD-16** — outstanding work is DB-derived every run; the lease governs *who runs*, not *what's left* (so reclaim resumes for free). [SPINE#AD-16]
- **AD-18** — `adapters/store` owns all DDL; the lease adds none (it's a file). [SPINE#AD-18]
- **NFR-7 / SM-C1** — ban-safety: concurrent runs must never double the EDGAR rate; a coalesced trigger issues **no** EDGAR request; fewer/slower always beats a ban. Tests never hit live EDGAR. [prd; SPINE Errors&status]
- **Source tree** — `core/` defines the lease **port**; `adapters/lease/` implements the **filesystem** lease. [SPINE source tree + capability map "FR-11 single-flight → core + adapters/lease → AD-11, AD-12, AD-16"]

### Previous Story Intelligence (Epic 1–3.1)
- **`CatchUpStatus` was built as a plain `enum.Enum` for exactly this (Story 3.1).** Add `ALREADY_RUNNING` as one member — no `str`-mixed footgun.
- **The EdgarClient cool-down forward-hook (Story 1.3).** `self._sleep` is on the main thread; the deferred "uninterruptible cool-down / no heartbeat" concern is resolved here by the **background** heartbeat thread (no change to `run()`), not by making the sleep interruptible.
- **Long backfill has no lease (Story 2.3 defer).** Resolved: `backfill` now shares the lease (AC-6).
- **No-lease-yet (Story 3.1 defer).** Resolved: `catch-up` acquires the lease.
- **CLI house style (Epic 1–3.1):** deferred heavy imports; `typer.secho(fg=RED, err=True)` + `raise typer.Exit(code=…)`, never a traceback; close the client in `finally` via `contextlib.suppress`; GREEN success / YELLOW gaps; module-level constants for tunables (`_MAX_CONSECUTIVE_FAILURES`); error paths CLI-tested.
- **Config style (Epic 1–2):** default-when-absent sections (`ReconcileConfig`); reject `bool` before `int` (`isinstance(x, bool)`); clear `ConfigError` messages with the path.
- **Determinism/atomicity (kboss):** atomic file writes (temp + `os.replace`); a per-acquire `token` so release/heartbeat never touch a lease we no longer own.

### Public repo / security (hard constraints)
⚠️ **Public repo:** never write a real email/PII/secret into a tracked file. The lease file holds only `{token, pid, host, timestamps}` — **no** email/PII — but it is **runtime state**, so gitignore it. Tests use `you@example.com` (placeholder) / `a@b.co` (valid non-placeholder) only; the real contact email lives ONLY in the gitignored `fintin.toml`. Any live smoke uses a scratchpad-only untracked config, removed after. **Tests must NEVER hit live EDGAR (NFR-7)** — the coalesce CLI tests monkeypatch discovery/engine to *raise if called*, proving no request.

### Project Structure Notes
- **New:** `fintin/core/lease.py` (port + guard), `fintin/adapters/lease/file_lease.py` (filesystem impl), `tests/test_lease.py`.
- **Modified:** `fintin/core/catchup.py` (ALREADY_RUNNING + `catch_up_single_flight`), `fintin/config.py` (`[lease]`), `fintin/cli/app.py` (both triggers), `fintin.toml.example`, `.gitignore`, `README.md`, `deferred-work.md`, `tests/test_catchup.py`, `tests/test_config.py`, `tests/test_cli.py`.
- Hexagonal invariant: `core/lease.py` imports no `edgar`/ClickHouse/`pyarrow` (AST-guarded); `adapters/lease/` owns the filesystem lease; `cli/` is a dumb trigger.

### References
- [Source: _bmad-output/planning-artifacts/epics.md#Story-3.2 — ACs; Epic-3 refinement (filesystem lease, heartbeat during cool-down, NOT ClickHouse)]
- [Source: _bmad-output/planning-artifacts/architecture/architecture-fin-tin-2026-07-23/ARCHITECTURE-SPINE.md#AD-12 (self-expiring filesystem lease; coalesce), #AD-1 (lease = the one permitted operational state), #AD-2, source tree (`adapters/lease`), capability map (FR-11 → core + adapters/lease)]
- [Source: _bmad-output/planning-artifacts/prds/…/prd.md#FR-11 (single-flight; ALREADY_RUNNING exit-0; self-expiring lease; reclaim after crash)]
- [Source: _bmad-output/implementation-artifacts/3-1-pure-catch-up-engine.md — `CatchUpStatus`/`CatchUpReport`/`catch_up`; the "CatchUpStatus is an enum so 3.2 adds one member" setup; the no-lease-yet defer]
- [Source: _bmad-output/implementation-artifacts/deferred-work.md — story-1.3 (uninterruptible cool-down / lease heartbeat), story-2.3 (long backfill no lease), story-3.1 (no-lease-yet) — all resolved here]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Opus 4.8, 1M context)

### Debug Log References

- `uv run pytest -q` → **309 passed** (+26 over the 283 baseline). `test_lease.py` (11: FileLease acquire/coalesce/reclaim/heartbeat/release/idempotent + pure `run_single_flight` + AST guard); `test_config.py` +10 (`[lease]` default/parse + 8 validation rejects); `test_catchup.py` +3 (`catch_up_single_flight` coalesce/run + `ALREADY_RUNNING` member); `test_cli.py` +2 (catch-up + backfill coalesce → `ALREADY_RUNNING`, exit 0, discovery/engine never reached). Python 3.14 in `.venv`. No live EDGAR in any test (NFR-7) — the coalesce CLI tests monkeypatch discovery/`backfill_universe` to **raise if called**, proving no request on coalesce.
- Fixed an architecture-guard failure: `test_no_edgar_or_raw_http_imports_outside_edgar_adapter` bans `socket` outside `adapters/edgar/`; switched the lease's hostname source `socket.gethostname()` → `platform.node()` (diagnostic only; `platform` is not a banned network root).
- Added an autouse `_isolate_cwd` fixture to `test_cli.py` — the ingestion commands now create a default `fintin.lease` in the CWD, so every CLI test runs in a throwaway CWD (no lease file lands in the repo).
- No live smoke: the coalesce path is fully offline-proven (it returns before any EDGAR request), and a live `catch-up` constructs the EdgarClient which logs the contact email (edgartools identity line) — a PII risk not worth it for behavior already covered.

### Completion Notes List

- **Filesystem self-expiring lease, not ClickHouse (AD-12).** New pure `core/lease.py` (`Lease` port + generic `run_single_flight` combinator) + `adapters/lease/file_lease.py` (`FileLease`). No `schema.py`/DDL change (AD-1/AD-18 untouched) — the lease is "the one permitted operational state" (AD-1 carve-out). Lease file is gitignored runtime state holding only `{token, pid, host, timestamps}` (no PII).
- **Single-flight lives in the engine (AD-2).** `run_single_flight(lease, run)` acquires **before** invoking `run` — a coalesced trigger returns `None` (→ `ALREADY_RUNNING`) **without running discovery**, so **zero EDGAR requests** on coalesce (AC-1). The CLI is a dumb trigger that builds the concrete `FileLease` and hands it in.
- **Atomic acquire + stale reclaim (AC-1/AC-2).** Acquire claims the lock by atomically hard-linking a fully-written record (`os.link` — exclusive AND never a half-written file). A pre-existing lease is read: stale (heartbeat > TTL) or corrupt → reclaimed; live → coalesce. Reclaim "resumes partial work" for free because work is DB-derived (AD-16) — the lease governs *who* runs, never *what's left*.
- **Background heartbeat thread beats through the EDGAR cool-down (AC-3).** A daemon thread refreshes `heartbeat_at` every `heartbeat_seconds`; because it's a separate thread it keeps beating while the main thread is blocked in the `>=10-min` cool-down sleep — so the lease isn't reclaimed mid-cool-down, with **no change to the EdgarClient**. A hard crash kills the daemon thread → the lease goes stale → reclaimable. A per-acquire `token` stops heartbeat/release from touching a lease we no longer own.
- **`ALREADY_RUNNING` = the 4th `CatchUpStatus` (AC-4).** 3.1 built the enum for exactly this. `catch_up_single_flight` maps the coalesced `None` to an `ALREADY_RUNNING` `CatchUpReport`; the CLI renders it GREEN, exit 0. All of `STARTED`/`ALREADY_RUNNING`/`NOTHING_TO_DO`/`COMPLETED` exit-0.
- **Both `catch-up` and `backfill` share the one lease (AC-6, SM-C1).** They're the two EDGAR-heavy runs; the shared `cfg.lease.path` makes them mutually exclusive, closing the backfill×catch-up concurrency window (the real ban risk). `backfill` renders its own `ALREADY_RUNNING` line (it returns a `BackfillReport`, no status enum); both exit-0.
- **`[lease]` config (AC-9).** `LeaseConfig(path="fintin.lease", ttl_seconds=120, heartbeat_seconds=15)`, always populated (default when absent, like `[reconcile]`). Validation enforces `2 * heartbeat <= ttl` (heartbeat ≪ TTL) so a live run — even a paused one — is never falsely reclaimed.
- **Landed the deferred lease items:** story-1.3 (uninterruptible cool-down / no heartbeat) and story-2.3 (long backfill no lease) and story-3.1 (no-lease-yet) are all resolved (see deferred-work.md); the reclaim micro-race is the one new (single-machine, negligible) defer.
- Exit codes unchanged except the new coalesce path: `ALREADY_RUNNING` → 0; throttle/systemic/generic still → 1; config/universe/edgar → 2.

### File List

- `fintin/core/lease.py` (NEW) — pure `Lease` port + `run_single_flight` combinator.
- `fintin/adapters/lease/file_lease.py` (NEW) — filesystem `FileLease` (atomic acquire, stale reclaim, background heartbeat).
- `fintin/core/catchup.py` (MOD) — `CatchUpStatus.ALREADY_RUNNING` + `catch_up_single_flight`.
- `fintin/config.py` (MOD) — `LeaseConfig` + `_parse_lease` + `Config.lease`.
- `fintin/cli/app.py` (MOD) — single-flight wired into `catch-up` and `backfill` (shared lease; `ALREADY_RUNNING` render).
- `fintin.toml.example` (MOD) — `[lease]` section.
- `.gitignore` (MOD) — ignore `fintin.lease`.
- `tests/test_lease.py` (NEW) — FileLease + `run_single_flight` + AST guard.
- `tests/test_config.py` (MOD) — `[lease]` parsing/validation.
- `tests/test_catchup.py` (MOD) — `catch_up_single_flight` coalesce/run.
- `tests/test_cli.py` (MOD) — catch-up + backfill coalesce (`ALREADY_RUNNING`); autouse `_isolate_cwd`.
- `README.md` (MOD) — "Single-flight (one run at a time)" section.
- `_bmad-output/implementation-artifacts/deferred-work.md` (MOD) — resolved story-1.3/2.3/3.1 lease defers; added the reclaim micro-race.

## Change Log

- 2026-07-24 — Story 3.2 implemented (red-green-refactor through all 7 tasks). Pure `Lease` port + `run_single_flight` (`core/lease.py`); filesystem `FileLease` (atomic `os.link` acquire, stale reclaim, background heartbeat thread — beats through the EDGAR cool-down, AC-3); `CatchUpStatus.ALREADY_RUNNING` + `catch_up_single_flight`; `[lease]` config; single-flight wired into **both** `catch-up` and `backfill` (shared lease → mutual exclusion, AC-6). No ClickHouse/DDL (AD-1/AD-18); pure core AST-guarded. Lands the deferred story-1.3/2.3/3.1 lease/heartbeat items. **309 tests pass (+26)**; the coalesce CLI tests prove no EDGAR request when held (NFR-7). Switched the lease hostname to `platform.node()` (the arch-guard bans `socket`); added an autouse CWD-isolation fixture so no lease file lands in the repo. Status → review.
- 2026-07-24 — Story 3.2 drafted (exhaustive substrate analysis: AD-12 spine, EdgarClient cool-down, config, catch-up engine, adapters/lease placeholder all read in full). Design settled: a **filesystem** self-expiring lease (AD-12; NOT ClickHouse) — a pure `Lease` port + `run_single_flight` combinator in `core/lease.py`, a `FileLease` adapter (atomic O_EXCL acquire, stale reclaim, background heartbeat thread), a `[lease]` config block, `ALREADY_RUNNING` as the 4th `CatchUpStatus`, wired into **both** `catch-up` and `backfill` (shared lease → mutual exclusion, AC-6). Heartbeat thread beats through the EDGAR cool-down (AC-3) with no change to the client. Lands the deferred story-1.3/2.3/3.1 lease items. Status → ready-for-dev.
