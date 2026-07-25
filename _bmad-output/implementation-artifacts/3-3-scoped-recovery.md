---
baseline_commit: 26a049c78abaf112c5cb1436e9ce41e777b6db8d
---

# Story 3.3: Scoped recovery

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the builder,
I want to re-fetch and rebuild one company from EDGAR with `fintin recover --cik X`,
so that I can repair Tier 0 corruption or loss without a new subsystem.

## Acceptance Criteria

1. **Given** a target CIK **When** I run `fintin recover --cik X` **Then** it re-ingests that company from EDGAR via the **normal throttled path** (a scoped re-ingest), **superseding** the prior Tier 0 copy with a higher ingest-monotonic `version` (AD-6), **and re-derives Tier 1 → resolution → mart** (FR-6, AD-6, AD-14).
2. **Given** recovery **Then** it **reuses the existing ingest machinery — no new subsystem** (a thin flag): the pure `ingest_company` (Tier 0) + `map_company` (Tier 1) composed behind one small pure `recover_company`; the resolution MV + wide mart auto-derive from the Tier 1 insert (Story 1.6), so re-deriving Tier 1 completes the chain.
3. **Given** automated corruption *detection* **Then** it is **out of v1 scope** (scrub is Should; ad-hoc reactive repair is Won't) — recovery here is **manually invoked** with an explicit `--cik`.
4. **Given** ban-safety **Then** recovery goes through the **one rate-limited EDGAR client** (AD-3) and acquires the **shared single-flight lease** (Story 3.2, AD-12) — a `recover` while a catch-up/backfill is active returns `ALREADY_RUNNING` (exit-0, no EDGAR request); a recover holds the lease so nothing else double-hits EDGAR.
5. **Given** the offline test suite **Then** it covers the pure `recover_company` (fakes — re-ingest + re-map, version passthrough, AST purity) and the CLI error + ban-safety paths (invalid CIK, missing/placeholder email, no-companyfacts, throttle, `ALREADY_RUNNING`), with **zero live EDGAR** (NFR-7). The recover *happy path* hits `companyfacts`, so — like `ingest-company` — it is exercised offline through the engine, never live.

## 🔑 Key design decisions (settled)

1. **Recovery is a *scoped re-ingest*, not a new engine (AD-14, epic "thin story").** The epic is explicit: *"Recovery (FR-6) is a thin story: `fintin recover --cik X` = catch-up scoped to one accession/company, riding Epic 2's machinery — not a new subsystem."* AD-14: *"recovery is a scoped re-ingest."* So `recover_company` **composes two primitives that already exist**: `ingest_company(cik, …)` (re-land Tier 0 from `companyfacts`, superseding the corrupt copy by a higher `version`, AD-6) then `map_company(cik, …)` (re-derive Tier 1 from the fresh Tier 0). No new subsystem, no new fetch path, no new DDL.
2. **Superseding by ingest-monotonic version (AD-6/AD-14) — same-taxonomy re-ingest.** The re-ingest stamps `next_ingest_version(client)` (strictly greater than the corrupt prior copy), so `FINAL`/`argMax` reads return the fresh Tier 0; the re-map stamps `next_canonical_version(client)` for Tier 1. Because recovery re-ingests the *same* company under the *same* taxonomy, every fact's `canonical_concept` is unchanged, so version-supersession resolves cleanly (no concept churn — the story-1.5 "cross-taxonomy re-map needs a mart rebuild" caveat does not apply to a same-version recovery).
3. **Tier 1 → resolution → mart is automatic after the re-map (Story 1.6).** The wide mart resolves concepts **on read** over `canonical_fact` (Approach B), and the `resolved_fact` MV fires **on insert** into `canonical_fact`. So once `map_company` re-inserts the recovered company's Tier 1 rows (higher version), both the MV and the read-time mart reflect the recovered values — "re-derives Tier 1 → resolution → mart" is satisfied by the re-map alone. No explicit mart rebuild.
4. **Pure engine, dumb trigger (AD-2).** `fintin/core/recover.py` (NEW, pure) = `recover_company(cik, *, fetch_facts, insert_raw_rows, read_raw_facts, insert_canonical_rows, taxonomy_version, raw_version, canonical_version) -> RecoverReport`, importing only `ingest_company`/`map_company`/their result types from `core` — no `edgar`/ClickHouse/`pyarrow` (AST-guarded). The `recover` CLI wires the concrete EDGAR fetch + store read/insert and the lease.
5. **Ban-safety: throttled client + shared single-flight lease (AD-3, AD-12).** Recovery hits `companyfacts`, so it runs through the one `EdgarClient` (its gate rejects a blank/placeholder email before any request) and acquires the **same** `cfg.lease.path` lease as catch-up/backfill (via `run_single_flight`) — mutual exclusion across all EDGAR runs. A coalesced recover returns `ALREADY_RUNNING` (exit-0) **without any EDGAR request** (the re-ingest is inside the guarded `_run`).
6. **Targeted, not Universe/index-driven.** Unlike catch-up (index-derived) and backfill (Universe-wide), `recover --cik X` targets **one explicit CIK** and **always re-fetches it** (no membership/present skip — the whole point is to repair a possibly-corrupt present copy). It needs **no `[universe]` section** (you can recover any CIK, in-scope or not). Just a valid CIK.
7. **`--cik` only in v1; `--accession` deferred.** The AC mentions "CIK/accession", and the epic says "one accession/company". v1's only fetch is per-company `companyfacts` (AD-13; the per-accession fetch is the same deferred item as Story 3.1's), so recovery is **per-company** (`--cik`). Accession-scoped recovery is deferred with the per-accession fetch strategy.
8. **No detection/scrub (AC-3).** No corruption *detection*, no scheduled scrub — `recover` is a manual, explicit-target repair. (The at-rest scrub is a deferred "Should".)

## Tasks / Subtasks

- [x] **Task 1 — Pure recovery engine** (AC: 1, 2, 4) — `fintin/core/recover.py` (NEW, pure)
  - [x] `RecoverReport(NamedTuple)`: `cik: int`, `ingest: IngestResult` (Tier 0), `project: ProjectResult` (Tier 1). Convenience `@property`: `rows_landed` (= `ingest.rows_landed`), `projected` (= `project.projected`), `raw_seen` (= `project.raw_seen`).
  - [x] `recover_company(cik, *, fetch_facts, insert_raw_rows, read_raw_facts, insert_canonical_rows, taxonomy_version, raw_version, canonical_version) -> RecoverReport`: call `ingest = ingest_company(cik, fetch_facts=fetch_facts, insert_rows=insert_raw_rows, taxonomy_version=taxonomy_version, version=raw_version)` then `project = map_company(cik, read_raw_facts=read_raw_facts, insert_rows=insert_canonical_rows, version=canonical_version)`; return `RecoverReport(int(cik), ingest, project)`. Docstring: scoped re-ingest (Tier 0, superseding by version) → re-derive Tier 1 (flows to resolution + mart); reuses the existing machinery.
  - [x] Import only `from fintin.core.ingest import IngestResult, ingest_company` + `from fintin.core.canonical import ProjectResult, map_company` + stdlib. **No `edgar`/ClickHouse/`pyarrow`.**
- [x] **Task 2 — `recover` CLI trigger** (AC: 1, 3, 4, 6, 7) — `fintin/cli/app.py` (MOD). Update the `_root()` docstring (recover exists now — remove the "recover arrives in a later story" note).
  - [x] `@app.command("recover")` with `cik: int = typer.Argument(..., help="SEC CIK to re-fetch and rebuild (e.g. 320193).")` and `--config/-c`. (No `--universe`, no `--show-gaps`.)
  - [x] `_configure_logging()`; **deferred imports**: `EdgarClient`, `EdgarConfigError`, `EdgarThrottleError`; `NoCompanyFactsError`, `edgartools_version`, `fetch_company_facts`; `insert_raw_facts`, `next_ingest_version`, `read_raw_facts`; `insert_canonical_facts`, `next_canonical_version`; `FileLease`; `run_single_flight`; `recover_company`.
  - [x] Validate `1 <= cik <= 4_294_967_295` → **exit 2** (mirror `ingest-company`). `load_config` → `ConfigError` **exit 2**. `EdgarClient(cfg)` → `EdgarConfigError` **exit 2** (ban-safety email gate, before any request). `check_connection` → `StoreConnectionError` **exit 1**.
  - [x] Build `lease = FileLease(cfg.lease.path, ttl_seconds=…, heartbeat_seconds=…)`. A local `_run()` does: `client = get_client(...)` in a `try/finally` (close); `raw_version = next_ingest_version(client)`; `canonical_version = next_canonical_version(client)`; `return recover_company(cik, fetch_facts=lambda c: fetch_company_facts(edgar_client, c), insert_raw_rows=lambda rows: insert_raw_facts(client, rows), read_raw_facts=lambda c: read_raw_facts(client, c), insert_canonical_rows=lambda rows: insert_canonical_facts(client, rows), taxonomy_version=edgartools_version(), raw_version=raw_version, canonical_version=canonical_version)`.
  - [x] `report = run_single_flight(lease, _run)` inside a `try/except`: `NoCompanyFactsError` → YELLOW "EDGAR has no companyfacts for CIK X" **exit 1**; `EdgarThrottleError` → "EDGAR throttled, recovery aborted" **exit 1**; generic `Exception` → "Recovery failed" **exit 1**. `if report is None:` → GREEN "Another run is already active — ALREADY_RUNNING (nothing to do; no EDGAR request issued)." **exit 0**. Never a traceback.
  - [x] Success render (GREEN): `"Recovered CIK {cik}: {rows_landed} facts re-ingested into Tier 0, {projected} projected to canonical Tier 1 (resolution + mart re-derived) in database '{db}'."` **exit 0**.
- [x] **Task 3 — Tests (offline; NFR-7)** (AC: 5)
  - [x] `tests/test_recover.py` (NEW, pure) — reuse the `_Fact`/`_facts` stub pattern; wire fakes so `read_raw_facts` returns what `insert_raw_rows` captured (simulating the store round-trip): re-ingest lands Tier 0 rows (stamped `raw_version`), re-map projects them to Tier 1 (stamped `canonical_version`, `canonical_concept` = element local name); `RecoverReport.rows_landed`/`projected`/`raw_seen` correct; `ingest.version == raw_version`, `project.version == canonical_version`; a company yielding zero facts still runs both stages cleanly (0 landed, 0 projected). **AST purity guard**: `core/recover.py` imports no `edgar`/`clickhouse`/`pyarrow`.
  - [x] `tests/test_cli.py` (MOD): `recover` error/ban-safety paths (mirror the `ingest-company`/coalesce patterns): help lists `recover`; missing config → 2; invalid CIK (0 and > 2³²) → 2; missing `[edgar]` → 2; placeholder email → 2; `NoCompanyFactsError` (monkeypatch the engine) → 1; `EdgarThrottleError` → 1; **`ALREADY_RUNNING`** — hold a real `FileLease` on `cfg.lease.path`, monkeypatch `fetch_company_facts`/`recover_company` to **raise if called** → exit 0, "ALREADY_RUNNING", no EDGAR. Each asserts no `Traceback`. (No `[universe]` needed — recover doesn't read it.)
- [x] **Task 4 — Validate & document** (AC: all)
  - [x] `uv run pytest` — full suite green; record count + delta.
  - [x] `README.md`: a "Recover a company (repair Tier 0)" section — `fintin recover --cik X`; re-fetches one company's `companyfacts` through the throttled client + shared lease, supersedes the prior copy by version, and re-derives Tier 1 → resolution → mart; manual/targeted (no auto-detection); needs no `[universe]`.
  - [x] `fintin.toml.example` needs **no** change (recover reuses `[clickhouse]`/`[edgar]`/`[lease]`).
  - [x] `deferred-work.md`: note `--accession`-scoped recovery deferred (per-accession fetch, tied to the Story 3.1/AD-13 defer); note automated corruption detection / at-rest scrub is out of v1 scope (Should/Won't); note recovery re-fetches full `companyfacts` (same per-company granularity as catch-up).
  - [~] (Optional) Live smoke — **SKIPPED by choice**: `recover` = `ingest-company` + `map-canonical` composed (both already live-smoked in earlier stories), and a live `recover` logs the EDGAR contact-email identity line (a PII risk into any transcript), so the offline-covered composition was not re-smoked live.

## Dev Notes

### What this story IS
The **final Epic 3 story** — and deliberately **thin**. `fintin recover --cik X` repairs a corrupt/lost Tier 0 by re-fetching one company from EDGAR and rebuilding it: a scoped re-ingest (`ingest_company`) that supersedes the prior copy by a higher `version` (AD-6), followed by a Tier 1 re-map (`map_company`) that flows to resolution + the mart (Story 1.6). It rides the existing machinery + the single-flight lease + the throttled client — **no new subsystem** (AD-14). It closes Epic 3.

### What this story is NOT (scope fences — do not implement)
- ❌ **No new ingest/fetch engine, no new DDL** — reuse `ingest_company` + `map_company`; `schema.py` untouched (AD-18).
- ❌ **No corruption detection / scheduled scrub** — manual `--cik` only (AC-3; the at-rest scrub is a deferred Should).
- ❌ **No `--accession` scope** — v1 fetch is per-company `companyfacts` (AD-13); accession-scope is deferred with the per-accession fetch.
- ❌ **No new lease / concurrency mechanism** — reuse the Story 3.2 `FileLease` + `run_single_flight` (shared `cfg.lease.path`).
- ❌ **No `[universe]` dependency** — recover targets an explicit CIK, in-scope or not.
- ❌ **No new config** — reuse `[clickhouse]`/`[edgar]`/`[lease]`.

### Current substrate — reuse, do not reinvent (all verified present)
- `fintin/core/ingest.py` — `ingest_company(cik, *, fetch_facts, insert_rows, taxonomy_version, version) -> IngestResult` (`rows_landed`, `version`). Tier 0 re-ingest, superseding by version (AD-6).
- `fintin/core/canonical.py` — `map_company(cik, *, read_raw_facts, insert_rows, version) -> ProjectResult` (`raw_seen`, `projected`, `version`). Tier 0 → Tier 1, zero network.
- `fintin/adapters/edgar/facts.py` — `fetch_company_facts(client, cik)` (raises `NoCompanyFactsError` when EDGAR has none), `edgartools_version()`.
- `fintin/adapters/edgar/client.py` — `EdgarClient(cfg)` (ban-safety gate), `EdgarConfigError`, `EdgarThrottleError`.
- `fintin/adapters/store/raw_fact_repo.py` — `insert_raw_facts`, `next_ingest_version`, `read_raw_facts(client, cik)` (Tier 0 `FINAL`).
- `fintin/adapters/store/canonical_fact_repo.py` — `insert_canonical_facts`, `next_canonical_version`.
- `fintin/adapters/lease/file_lease.py` — `FileLease(path, *, ttl_seconds, heartbeat_seconds)`; `fintin/core/lease.py` — `run_single_flight(lease, run) -> T | None` (Story 3.2). Recover reuses both (coalesce → `None` → `ALREADY_RUNNING`).
- CLI templates: `ingest-company` (CIK validation + EdgarClient + Tier 0 fetch/insert + `NoCompanyFactsError`/throttle handling) and `map-canonical` (Tier 1 read/project/insert). `recover` = both, under the lease. Mirror `backfill`/`catch-up` for the `_run` + `run_single_flight` + `ALREADY_RUNNING` wiring.

### The composition (the load-bearing logic)
```
# core (pure): recover = re-ingest Tier 0 (supersede) → re-map Tier 1 (→ resolution/mart)
def recover_company(cik, *, fetch_facts, insert_raw_rows, read_raw_facts,
                    insert_canonical_rows, taxonomy_version, raw_version, canonical_version):
    ingest  = ingest_company(cik, fetch_facts=fetch_facts, insert_rows=insert_raw_rows,
                             taxonomy_version=taxonomy_version, version=raw_version)  # Tier 0, AD-6
    project = map_company(cik, read_raw_facts=read_raw_facts, insert_rows=insert_canonical_rows,
                          version=canonical_version)                                  # Tier 1 → mart
    return RecoverReport(cik, ingest, project)

# recover CLI (dumb trigger): throttled client + shared single-flight lease
report = run_single_flight(lease, _run)   # ALREADY_RUNNING (None) if a run is active
```
The re-ingest lands the fresh Tier 0 at a strictly higher version → supersedes the corrupt copy on `FINAL`/`argMax`. The re-map reads that Tier 0 (`FINAL`) and re-inserts Tier 1 at a higher version → the resolution MV + read-time mart reflect the recovered values (Story 1.6). One targeted company, one EDGAR request, under the lease.

### Architecture constraints (authoritative)
- **FR-6 / AD-14** — Tier 0 recovery is a scoped re-ingest from EDGAR, preserving provenance, superseding by version; re-derive Tier 1 → resolution → mart. [SPINE#AD-14; prd FR-6]
- **AD-2** — pure engine (`recover_company`), dumb CLI trigger. [#AD-2]
- **AD-3** — through the one rate-limited client (no direct HTTP). [#AD-3]
- **AD-6** — `ReplacingMergeTree(version)`; a higher ingest-monotonic version supersedes on read (FINAL/argMax). [#AD-6]
- **AD-12** — single-flight lease guards concurrent EDGAR runs; recover shares it (coalesce → `ALREADY_RUNNING`). [#AD-12]
- **AD-13** — per-company `companyfacts` is the v1 fetch; per-accession deferred (so recover is `--cik`). [#AD-13]
- **AD-18** — `adapters/store` owns all DDL; recover adds none. [#AD-18]
- **NFR-7 / SM-C1** — tests never hit live EDGAR; the recover happy path is engine-tested with fakes; a coalesced recover issues no EDGAR request. [#Testing]
- **Scope (v1 Won't):** *"ad-hoc reactive repair is Won't"* refers to **automated detection**; a **manually-invoked** scoped recovery is exactly FR-6 (in scope). [SPINE Deferred; epics.md]

### Previous Story Intelligence (Epic 1–3.2)
- **Reuse `ingest_company` + `map_company` wholesale** — they are already pure, injected-port orchestrators; recover just sequences them (Tier 0 then Tier 1) at fresh versions.
- **Single-flight is already generic (Story 3.2)** — `run_single_flight(lease, _run)` returns `None` on coalesce → render `ALREADY_RUNNING` (exit 0). Recover reuses it verbatim (shared `cfg.lease.path`); the coalesce CLI test holds a real `FileLease` and asserts no EDGAR (monkeypatch `fetch_company_facts` to raise-if-called).
- **EdgarClient once + ban-safety gate (Epic 1–3.1)** — construct once; its gate rejects a blank/placeholder email before any request (exit 2). `NoCompanyFactsError` → exit 1 (like `ingest-company`); `EdgarThrottleError` → exit 1.
- **CLI house style** — CIK range validation (exit 2) before work; deferred heavy imports; `typer.secho(fg=RED, err=True)` + `raise typer.Exit(code=…)`, never a traceback; close the client in `_run`'s `finally`; GREEN success. Error paths CLI-tested; the EDGAR happy path is not (NFR-7).
- **Same-taxonomy re-ingest is clean (Story 1.5 caveat)** — recovery re-ingests the same company/taxonomy, so `canonical_concept` doesn't change and version-supersession resolves without the cross-taxonomy mart-rebuild the story-1.5 defer describes.
- **Determinism (kboss)** — `ingest_company` de-dups/stamps deterministically; the re-map is 1:1; versions are store-derived monotonic.

### Public repo / security (hard constraints)
⚠️ **Public repo:** never write a real email/PII/secret into a tracked file. Recover needs `[edgar]` (it hits EDGAR); tests use `you@example.com` (placeholder) / `a@b.co` (valid non-placeholder) only; the real contact email lives ONLY in the gitignored `fintin.toml`. Any live smoke uses a scratchpad-only untracked config, removed after (and note: a live `recover` logs the EDGAR identity line — keep the real email out of every tracked file). **Tests must NEVER hit live EDGAR (NFR-7)** — the coalesce/no-facts/throttle CLI tests monkeypatch the engine/fetch.

### Project Structure Notes
- **New:** `fintin/core/recover.py` (pure engine), `tests/test_recover.py`.
- **Modified:** `fintin/cli/app.py` (`recover` command + `_root` docstring), `README.md`, `deferred-work.md`, `tests/test_cli.py`.
- Hexagonal invariant: `core/recover.py` imports no `edgar`/ClickHouse/`pyarrow` (AST-guarded); `adapters/edgar` owns the fetch; `adapters/store` owns the reads/writes; `cli/` is a dumb trigger.

### References
- [Source: _bmad-output/planning-artifacts/epics.md#Story-3.3 — ACs; Epic-3 refinement ("Recovery is a thin story … riding Epic 2's machinery — not a new subsystem")]
- [Source: _bmad-output/planning-artifacts/architecture/architecture-fin-tin-2026-07-23/ARCHITECTURE-SPINE.md#AD-14 (recovery is a scoped re-ingest), #AD-2, #AD-3, #AD-6, #AD-12, #AD-13; Deferred (detection/scrub Should/Won't)]
- [Source: _bmad-output/planning-artifacts/prds/…/prd.md#FR-6 (Tier 0 recovery)]
- [Source: _bmad-output/implementation-artifacts/1-4-land-raw-facts.md + 1-5-map-canonical-tier1.md — `ingest_company`/`map_company` recover reuses]
- [Source: _bmad-output/implementation-artifacts/3-2-single-flight-lease.md — `FileLease`/`run_single_flight`, the shared-lease + `ALREADY_RUNNING` wiring recover reuses]

## Review Findings

Reviewed 2026-07-25 by 3 parallel adversarial layers (Blind Hunter, Edge Case Hunter, Acceptance Auditor), all completed. **All 5 ACs + 8 settled decisions verified genuinely satisfied; no Critical/High.** Independently confirmed: the thin reuse (`recover_company` composes `ingest_company` + `map_company`, no re-implementation), purity (AST-guarded), the re-ingest→re-map ordering + read-after-write (synchronous inserts), store-derived monotonic versions (AD-6), the shared-lease coalesce with no EDGAR request, correct exit codes / handler ordering, `schema.py` + `fintin.toml.example` untouched (no new DDL/config), and no `[universe]` read. Triage: **5 patch, 2 defer, 3 dismissed.**

- [x] [Review][Patch] **`--cik` was documented as a flag but implemented positional — the README's command errored** — the AC, epic, PRD, and README all say `fintin recover --cik X`, but the code used `typer.Argument` (positional), so the shipped copy-paste example `fintin recover --cik 320193` failed with "No such option: --cik". Changed the CLI to `typer.Option(..., "--cik")` (matching the AC/docs) and updated the recover CLI tests to `["recover", "--cik", …]`. [fintin/cli/app.py, tests/test_cli.py]
- [x] [Review][Patch] **Zero-landed run overclaimed "N re-ingested … resolution + mart re-derived"** — when EDGAR returns a non-`None` but empty/all-filtered fetch, `rows_landed == 0` yet the GREEN line claimed a re-ingest + re-derivation that didn't happen. Added an honest `rows_landed == 0` branch: YELLOW "EDGAR returned no ingestable facts — Tier 0 left unchanged (M re-projected from existing data)", exit 0. Added CLI render tests for both the GREEN (facts landed) and YELLOW (zero-landed) branches. [fintin/cli/app.py, tests/test_cli.py]
- [x] [Review][Patch] **README overstated "corrupted or lost" / "idempotent"** — insert-only supersession (AD-6) only overrides *matching* identity keys; a key-field-mangled or vanished fact isn't retracted. Scoped the wording to "supersedes values on matching identity keys" and noted (like `backfill --refresh`) it cannot retract rows absent from the fresh fetch. [README.md]
- [x] [Review][Patch] **`recover --help` ate `[universe]` (Rich markup)** — the docstring's literal `[universe]` was consumed as console markup, rendering "Needs no ; targets any CIK". Reworded to "Needs no universe config; targets any CIK." (and de-marked the arrows). [fintin/cli/app.py]
- [x] [Review][Patch] **Optional-smoke subtask marked `[x]` but skipped** — corrected the checkbox to `[~] SKIPPED by choice` with the rationale (recover = ingest-company + map-canonical, both live-smoked; a live recover logs the contact email). [3-3-scoped-recovery.md]
- [x] [Review][Defer] **Partial-recovery inconsistency window** [fintin/cli/app.py] — Tier 0 re-landed but the Tier 1 re-map raises → a transient Tier0/mart split; self-healing on re-run, and ClickHouse offers no cross-statement transaction to wrap it. Logged (optionally surface a "re-run to finish" hint).
- [x] [Review][Defer] **Read-after-write rests on synchronous inserts** [fintin/adapters/store/client.py] — the re-map's `raw_fact FINAL` read sees the just-inserted Tier 0 only because no `async_insert` is configured; if async inserts are ever adopted, add a sync barrier before the re-map. Logged.

_Dismissed (3, with rationale): (a) **the shared lease doesn't cover `ingest-company`/`map-canonical`'s version-sequence race** — pre-existing and already deferred (story-1.5 `next_canonical_version` read-then-increment); recover correctly takes the lease. (b) **`EdgarClient(cfg)` construction guards only `EdgarConfigError`** — the established cross-command pattern; `__init__` only raises `EdgarConfigError` by design. (c) **a store drop between `check_connection` and `get_client` renders generic "Recovery failed" not "Connection failed"** — exit code still 1, no traceback; label specificity is cosmetic and matches the other commands._

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Opus 4.8, 1M context)

### Debug Log References

- `uv run pytest -q` → **323 passed** (+12 over the 311 baseline). `test_recover.py` (4, pure: re-ingest→re-map, supersede-by-version, zero-fact, AST guard); `test_cli.py` +8 (`recover` help + invalid-CIK + missing-config + missing/placeholder-email + no-companyfacts→1 + throttle→1 + `ALREADY_RUNNING` coalesce→0). Python 3.14. No live EDGAR in any test (NFR-7) — the no-facts/throttle/coalesce CLI tests monkeypatch the engine; the coalesce test holds a real `FileLease` and proves `recover_company` is never reached.
- `uv run fintin --help` → all 10 commands wired (…, catch-up, status, **recover**).
- No live smoke: `recover` = `ingest-company` + `map-canonical` composed (both already live-smoked in earlier stories), and a live `recover` logs the EDGAR contact-email identity line — a PII risk not worth re-proving an offline-covered composition.

### Completion Notes List

- **Thin scoped re-ingest, as designed (AD-14, epic "thin story").** New pure `core/recover.py` — `recover_company` composes the existing `ingest_company` (Tier 0 re-land, superseding the corrupt copy by `next_ingest_version`, AD-6) + `map_company` (Tier 1 re-derive at `next_canonical_version`). No new engine, no new fetch path, no new DDL/table/config (`schema.py`/`fintin.toml.example` untouched).
- **Tier 1 → resolution → mart is automatic.** The wide mart resolves on read over `canonical_fact` and the resolution MV fires on its insert (Story 1.6), so the re-map alone re-derives resolution + mart — the AC's "re-derives Tier 1 → resolution → mart" needs no explicit rebuild. Same-taxonomy re-ingest ⇒ `canonical_concept` unchanged ⇒ clean version-supersession (the story-1.5 cross-taxonomy caveat doesn't apply).
- **Ban-safety reused (AD-3, AD-12).** Recovery runs through the one rate-limited `EdgarClient` (email gate → exit 2) and acquires the **shared** `cfg.lease.path` single-flight lease via `run_single_flight` — a recover while a catch-up/backfill is active coalesces to `ALREADY_RUNNING` (exit 0, **no EDGAR request**; the re-ingest is inside the guarded `_run`). Verified by the coalesce CLI test (real `FileLease` held; `recover_company` raise-if-called never fires).
- **Targeted, not Universe/index-driven.** `recover --cik X` needs no `[universe]`, always re-fetches the named CIK (no membership skip — the point is to repair a possibly-corrupt present copy). `--cik` only in v1; `--accession` deferred (per-accession fetch, AD-13). No corruption detection/scrub (AC-3, manual repair).
- **Pure engine/dumb trigger (AD-2).** `core/recover.py` is `edgar`/ClickHouse/`pyarrow`-free (AST-guarded); the CLI wires the concrete EDGAR fetch + store read/insert + lease. Exit codes: invalid CIK/config/email → 2; connection/no-companyfacts/throttle/generic → 1; `ALREADY_RUNNING`/success → 0. Never a traceback.
- **Closes Epic 3.** All 10 CLI commands wired; the full v1 capability set (FR-1…FR-14) is delivered.

### File List

- `fintin/core/recover.py` (NEW) — pure `recover_company` + `RecoverReport`.
- `fintin/cli/app.py` (MOD) — `recover` command; `_root` docstring finalized.
- `tests/test_recover.py` (NEW) — pure engine tests + AST purity guard.
- `tests/test_cli.py` (MOD) — `recover` error + ban-safety wiring (8).
- `README.md` (MOD) — "Recover a company (repair Tier 0)" section.
- `_bmad-output/implementation-artifacts/deferred-work.md` (MOD) — Story 3.3 defers (`--accession`, detection/scrub, full-companyfacts granularity).

## Change Log

- 2026-07-25 — Code review (3 parallel adversarial layers). All 5 ACs + 8 decisions verified; no Critical/High. Applied all **5 patch findings**: (P1) `--cik` made a `typer.Option` so the documented `recover --cik X` works (was positional — the README example errored); (P2) honest zero-landed render (YELLOW "Tier 0 left unchanged", no overclaimed re-derivation) + both render branches now CLI-tested; (P3) scoped the README supersede-by-key wording (can't retract vanished/key-mangled rows, like `--refresh`); (P4) fixed the `--help` `[universe]` Rich-markup garble; (P5) honest optional-smoke checkbox. **2 deferred** (partial-recovery self-healing split; sync-insert read-after-write dependency) and **3 dismissed** (pre-existing version race; EdgarClient-construction guard pattern; connection-drop label), logged. **325 tests pass (+2)**. Secret-scan clean. Status → done.
- 2026-07-24 — Story 3.3 implemented (red-green-refactor through all 4 tasks). Pure `recover_company` (`core/recover.py`) composes `ingest_company` (Tier 0, supersede by version) + `map_company` (Tier 1 → resolution/mart); `recover --cik X` dumb-trigger CLI reuses the throttled client + the shared single-flight lease (coalesce → `ALREADY_RUNNING`). No new subsystem/DDL/config (AD-14/AD-18); `--cik` only; no `[universe]`. **323 tests pass (+12)**; the coalesce CLI test proves no EDGAR request when the lease is held (NFR-7). Closes Epic 3 (10 CLI commands wired). Status → review.
- 2026-07-24 — Story 3.3 drafted (substrate analysis: `ingest_company`/`map_company`, the canonical repo, the Story 3.2 lease all read in full). Design settled: `fintin recover --cik X` = a **scoped re-ingest** — a small pure `recover_company` composing `ingest_company` (Tier 0, superseding by version, AD-6) + `map_company` (Tier 1, flows to resolution + mart, Story 1.6), through the one throttled client (AD-3) and the shared single-flight lease (AD-12; coalesce → `ALREADY_RUNNING`). Thin flag, no new subsystem (AD-14); `--cik` only (per-accession deferred, AD-13); no detection/scrub (AC-3); no `[universe]`/DDL/config change. Status → ready-for-dev.
