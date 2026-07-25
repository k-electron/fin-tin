# Deferred Work

Open deferred items only, newest story first. Anything closed moves to
[Resolved](#resolved) at the bottom with a note on what closed it — this ledger is
the pre-retro checklist, so the open set has to stay readable at a glance.

**35 open items across 17 stories/reviews.** None blocks v1. Three are worth pulling
out because their own stated revisit trigger has already fired, so they are live
rather than hypothetical:

- **Schema migrations** (medium, code review of story-1.2) — `CREATE … IF NOT EXISTS`
  is create-only, so a changed definition needs a manual drop/recreate. The dropped
  `resolved_fact` pair is a live instance: gone from the DDL, still sitting in any
  already-created database.
- **`EdgarClient` singleton enforcement** (medium, code review of story-1.3) —
  deferred until "the lease/heartbeat lands". It landed in Story 3.2.
- **`next_canonical_version` read-then-increment is not atomic** (low, code review of
  story-1.5) — deferred until "the lease lands". Same trigger, also fired.

## Deferred from: code review of story-3.3 (2026-07-25)

- **Partial-recovery inconsistency window (Tier 0 re-landed, Tier 1 re-map raised)** (low, self-healing) — `recover` re-ingests Tier 0 then re-maps Tier 1 as two sequential store operations (not one transaction). If `ingest_company` commits the fresh Tier 0 but `map_company` then raises (a read/project/insert error), the run exits 1 with Tier 0 advanced but Tier 1 (and thus the mart) NOT re-derived — a transient split. Not silent corruption: a re-run re-ingests (strictly higher version) and re-maps, so it self-heals; and ClickHouse has no cross-statement transaction to wrap this in anyway. Revisit (surface a "Tier 0 advanced; re-run to finish re-deriving Tier 1" hint on the failure) only if operators hit it.
- **Read-after-write correctness rests on synchronous inserts** (low, latent) — `recover` (and `ingest-company`→`map-canonical` run back-to-back) relies on the just-inserted Tier 0 rows being visible to the subsequent `raw_fact FINAL` read on the same client. `adapters/store/client.py` configures no `async_insert`, so ClickHouse's default synchronous insert guarantees this today. If `async_insert=1` is ever adopted, the re-map could read stale Tier 0 and re-project the corrupt copy — add a `wait_for_async_insert` / sync barrier between the ingest and the re-map at that point.

## Deferred from: story-3.3 (2026-07-24, scoped recovery)

- **`--accession`-scoped recovery** (deferred, AD-13) — `fintin recover --cik X` recovers a whole company (re-fetches its full `companyfacts`); recovering a *single accession* would need a per-accession fetch strategy, which is the same deferred item as Story 3.1's "narrower per-accession fetch vs full `companyfacts`" (v1's only fetch is per-company `companyfacts`). The pure `recover_company` engine and the CLI are already shaped so an `--accession` flag + a per-accession fetch drop in without a redesign. Revisit with the per-accession fetch strategy.
- **Automated corruption *detection* / at-rest scrub is out of v1 scope** (Should/Won't, by design — AC-3) — recovery is **manually invoked** with an explicit `--cik`; there is no scheduled scrub or "this number looks wrong" detector. Per the architecture (Deferred): the periodic at-rest integrity scrub is a "Should" and ad-hoc reactive-repair *detection* is a "Won't" for v1. The **repair** path itself (a scoped re-ingest superseding by version, AD-6/AD-14) is FR-6 and is delivered here. Revisit the scrub if at-rest corruption is ever observed; recovery is already the fix once a target is known.
- **Recovery re-fetches the full `companyfacts` and re-maps the whole company** — like catch-up, a targeted recover re-downloads the company's entire history (one request) and re-projects all of its Tier 0 → Tier 1, even to repair one bad fact (idempotent, bounded — one company). The narrower per-accession/per-fact repair rides on the deferred per-accession fetch above.

## Deferred from: story-3.2 (2026-07-24, single-flight lease)

- **The in-process heartbeat protects the lease *file*, not the *run* — a process frozen past the TTL can be displaced mid-run** (low→medium, single-machine, inherent) — the lease is held by an in-process background heartbeat thread and `run_single_flight` acquires once and never re-verifies ownership during the run. So if the *whole* holder process is paused longer than `ttl_seconds` — a laptop suspend/lid-close (routine for the "catch up before I query" trigger), `SIGSTOP`, severe scheduler starvation, or a **forward wall-clock jump** (`FileLease` uses `time.time()`, not `monotonic`, because staleness must be comparable across processes) — its heartbeat can't refresh, a concurrent trigger sees the lease stale and reclaims it, and the resumed original run keeps issuing EDGAR requests unaware it lost the lease → two concurrent runs (the double-rate ban this feature prevents). A related manifestation: a heartbeat `os.replace` that interleaves *after* a reclaimer's `os.link` can stamp the old owner's token back over the reclaimer's fresh record ("flapping"). All are inherent to a filesystem lease with an in-process heartbeat on a single machine. Mitigations if ever needed: pass a fencing token to the EDGAR operations and re-verify ownership at commit boundaries, or move to `fcntl`/`flock` advisory locks. **Accepted for v1** (single-user laptop; the default 120s TTL bounds the exposure and a displaced run's writes are still idempotent by version, AD-6, so it degrades to wasted requests, never corruption). The acquire-*instant* concurrent-reclaim race below is the microsecond variant of the same class.
- **Concurrent-reclaim micro-race on a stale lease** (low, single-machine) — `FileLease.acquire` reclaims an expired lease by `os.unlink` + atomic `os.link`; two triggers that both observe the *same* stale lease at the same instant could both unlink+relink and both believe they acquired (the classic filesystem-lease reclaim race). Negligible on a single-user laptop (v1's target): it requires two triggers firing within microseconds of each other exactly as a lease crosses its TTL. The common paths are race-free — a free acquire and a live-lease coalesce are guarded by `os.link`'s atomic exclusivity; reclaim now happens only on *positively-confirmed* death (stale heartbeat, corrupt content, or vanished — an unreadable-but-present file is presumed live and coalesced, so a transient I/O error never steals a live lease); and a per-acquire `token` stops heartbeat/release from touching a lease we no longer own. A fully-robust reclaim would need `fcntl`/`flock` (POSIX advisory locks). Revisit if fin-tin ever runs multi-process on a shared filesystem.
- **`.tmp` record files leak on a hard crash** (low) — `FileLease.acquire`/heartbeat write a `<path>.<token>.<pid>.tmp` before the atomic `os.link`/`os.replace` and remove it in a `finally`; a `SIGKILL`/power-loss between the write and the removal orphans one uniquely-named temp file per crash. Bounded (one per hard-kill) but never garbage-collected. Revisit by sweeping stale `<lease>.*.tmp` siblings on acquire if litter accumulates.
- **Lease is not held across the offline pre-flight** (low, by design) — `check_connection` (a ClickHouse ping) runs before the lease is acquired, so a coalesced trigger does one local ClickHouse round-trip before returning `ALREADY_RUNNING`. AC-1 forbids only **EDGAR** requests on coalesce (satisfied — discovery is inside the guarded `_run`); the ClickHouse ping is local and cheap. Moving it inside the guard would slightly reduce coalesced-trigger work; not worth the restructure.

## Deferred from: code review of story-1.2 (2026-07-23)

- **Schema migrations** (medium) — `fintin/adapters/store/schema.py` uses `CREATE … IF NOT EXISTS`, which silently keeps a stale table/MV definition if its DDL later changes; only the `screening_mart` view (`CREATE OR REPLACE`) is refreshed. There is no migration/versioning story, so a DDL change (e.g. the F1 resolution-rank fix) won't apply to an already-created deployment without a manual drop/recreate. Deferred because the v1 schema is still stabilizing and this is a solo local tool; a create-only limitation note is documented in `schema.py` now. Revisit with a proper migration mechanism when the schema settles or a second environment appears. **Live instance (2026-07-25):** the superseded `resolved_fact` + `resolved_fact_mv` were removed from the DDL but persist in any already-created database until dropped by hand (`DROP VIEW IF EXISTS resolved_fact_mv; DROP TABLE IF EXISTS resolved_fact;`).

## Deferred from: code review of story-1.3 (2026-07-23)

- **No singleton enforcement on `EdgarClient`** (medium) — every `EdgarClient(...)` mutates process-global edgar state (`EDGAR_IDENTITY`, `EDGAR_RATE_LIMIT_PER_SEC`, `httpclient.HTTP_MGR`); a second construction at a different rate silently replaces the first (last-writer-wins), and a construction landing mid-request could close the prior client's transport. Deferred: v1 is single-process and ingestion is single-flight (AD-12, Epic 3), so concurrent clients aren't a real scenario yet. Revisit when the lease/heartbeat lands or if a second concurrent EDGAR consumer appears — enforce a module-level `configure_edgar()`-once or a singleton. **Trigger fired:** the lease/heartbeat landed in Story 3.2.
- **`run()` is sync-only** (medium) — it can wrap edgartools' synchronous per-company path (v1, AD-13) but not the async fetchers or the bulk `companyfacts.zip` download, which raise `TooManyRequestsError` on their own coroutine path and would throttle outside the cool-down policy. Deferred: the bulk strategy is explicitly deferred (AD-13) and v1 uses only the sync per-company API. Add an async `run` variant (or a shared cool-down helper both call) when the bulk/async path is built.

## Deferred from: code review of story-1.4 (2026-07-24)

- **ClickHouse `Date` range guard** (low) — `raw_fact.period_start`/`period_end`/`filed_date` are `Date` (1970-01-01 … 2149-06-06). An out-of-range or corrupt date (typo, malformed XBRL) would be clamped/rejected by clickhouse-connect, desyncing it from the pre-insert `content_hash` and corrupting the identity key. Deferred: unreachable for real SEC/XBRL data (EDGAR XBRL is post-~2009, well inside the window). Revisit by widening to `Date32` or range-checking dates in the transform when at-rest integrity validation (the deferred AD-14 scrub) is built.

## Deferred from: story-1.5 (2026-07-24)

- **Mart view refresh on schema change is manual** (low, ties to the Story 1.2 migration defer) — the mart is `CREATE OR REPLACE VIEW`, but `schema-init` must be re-run to apply a label/column change to an existing database (Story 1.5's live `default` DB kept the stale view until re-run; throwaway-DB tests build fresh so they miss this). Folds into the deferred schema-migration mechanism.

## Deferred from: code review of story-1.5 (2026-07-24)

- **`next_canonical_version` read-then-increment is not atomic** (low) — `SELECT max(version)+1` then insert: two overlapping `map-canonical` runs (or a map racing a manual insert) could read the same max and stamp the same `version`, so two rows with the same identity key + same version would dedup nondeterministically under `ReplacingMergeTree`, breaking AD-6's "higher version always supersedes". Same pattern as `raw_fact_repo.next_ingest_version`. Deferred: v1 is single-process/single-writer and ingestion is single-flight (AD-12, deferred). Revisit when the lease lands — derive the version from a serialized source or guard concurrent writers. **Trigger fired:** the AD-12 lease landed in Story 3.2, but it wraps `backfill`/`catch-up`/`recover` — *not* `map-canonical` — so two concurrent `map-canonical` runs are still unguarded and this stands on its own terms. (The ambiguous-tag and manual-mart-refresh code-review findings are already recorded under the story-1.5 dev section above.)

## Deferred from: code review of story-1.6 (2026-07-24)

- **Drop the redundant `FROM canonical_fact FINAL` in the mart** (low, perf) — `version` is already the 4th `argMaxIf` rank term, so the winning value is the highest-version row with or without `FINAL`, and the `countIf(cond) > 0` presence guard only needs a nonzero count. `FINAL` therefore changes no result but forces a full merge-on-read of `canonical_fact` on every screen — a market-scale cost (NFR-3). Deferred as a validated optimization (drop `FINAL`, confirm results identical) once the Universe is backfilled and screen latency is measurable. AD-6-compliant either way (argMax-over-version is the supersession path).
- **Observability / test coverage** (low) — `DICTIONARY_VERSION` is defined but not surfaced (logged/asserted), so a bump has no observable effect; the NFR-3 tripwire test asserts correctness but measures no elapsed time (can't catch a latency regression); the `resolved_fact` MV this flagged as untested has since been dropped outright (2026-07-25), removing that sub-point rather than fixing it. Address when the concept dictionary gains a management/versioning surface or when NFR-3 needs a real latency guard.
- **Projection does no defensive scope/format re-validation** (low) — `to_canonical_fact_rows`/`local_name` trust Story 1.4's Tier-0 precondition (us-gaap/dei/srt, valid namespaced tags). A malformed tag (`us-gaap:` → `''`) or a leaked out-of-scope tag would project a garbled/empty `canonical_concept` silently. Unreachable today; add a defense-in-depth guard (assert/drop-with-count) if Tier 0's guarantees ever weaken.

## Deferred from: code review of story-2.1 (2026-07-24)

- **`cik` is a `UInt32` in the store, but SEC CIKs are nominally up to 10 digits (> 2³²)** (low) — `raw_fact`/`canonical_fact` key `cik` as `UInt32` (Story 1.2 schema; the "Identity" convention in ARCHITECTURE-SPINE.md). A CIK is a nominal 10-digit value (max 9,999,999,999 > 4,294,967,295), so a hypothetical future SEC assignment above 2³² would overflow the column. Currently unreachable — the highest CIK in edgartools' bundled reference table is ≈ 2.1M, and Story 2.1's resolve path now range-checks resolved CIKs to `[1, 4_294_967_295]` (out-of-range → gap) to match the config-CIK guard, so nothing out of range reaches the store. Pre-existing schema decision, not caused by this change. Revisit the column width (`UInt64`) if SEC CIK assignments ever approach 2³².

## Deferred from: code review of story-2.2 (2026-07-24)

- **Foreign-issuer annual forms (20-F / 40-F)** (medium, out of v1 scope) — the work-list form family is `10-K*`/`10-Q*` (matching the mart). Foreign private issuers (20-F) and Canadian filers (40-F) file XBRL financial statements under those forms, so a Universe CIK whose only in-window filings are 20-F/40-F would be silently skipped by `work-list`. Out of v1 scope per ARCHITECTURE-SPINE.md Deferred ("Foreign/IFRS filers — Won't (v1)"); v1's S&P 500 Universe is US-domestic. Revisit the discovery form family + the mart's periodic-form filter together if the Universe ever includes foreign issuers.
- **`date.today()` is process-local, not US-Eastern** (low) — the scan window's `today` uses the local clock; near midnight or a quarter boundary in a timezone ahead of ET it can name a day EDGAR hasn't reached, briefly missing a just-filed straggler. Self-healing: per-run re-derivation + the lookback window catch it next run. Not worth a `zoneinfo` dependency in v1; revisit if runs are scheduled at boundary times.
- **A 429 mid-multi-quarter fetch re-downloads earlier quarters** (low) — `EdgarClient.run` retries the whole `get_filings` operation on a throttle, re-fetching quarters already retrieved during a cool-down. Rate-safe (still under the limiter), just redundant work. Revisit if per-quarter fetch checkpointing is ever wanted.
- **FR-2 "one index fetch per calendar quarter" is not unit-asserted** (low) — the per-quarter request count is delegated to edgartools' `get_filings` internals and confirmed only by the manual live smoke (NFR-7 bans live-EDGAR tests). Revisit if a request-counting harness (e.g. a recorded-transport fixture) is added.

## Deferred from: story-2.3 (2026-07-24)

- **`present_ciks` skip assumes a per-company insert is atomic** (low) — backfill's resume test is per-company presence ("does this CIK have ≥ 1 row?"), which treats a present CIK as fully committed. This relies on one company's `insert_raw_facts` call landing as a single atomic ClickHouse block (all-or-nothing). A pathologically large single company whose rows spanned multiple insert blocks and was killed mid-insert could leave a partially-ingested company that a restart then skips as "present." Unreachable in practice (a company's filtered standard-taxonomy facts fit one block; v1 is single-writer/single-flight) and `--refresh` heals it (idempotent re-ingest). Revisit with strict per-accession completeness only if very large single-company inserts or multi-block splits appear.
- **Backfill does not pick up restatements/new filings for an already-present company** (low, by design — AD-13 division of labour) — because backfill skips present CIKs, re-running it won't fetch a newer filing (or restatement) for a company already in the store. That currency is the job of **catch-up** (Epic 3, reusing the Story 2.2 index-based reconciler), not backfill. `--refresh` re-ingests a present company's full history on demand. Recorded for clarity; not a gap to fix.
- **Bulk `companyfacts.zip` strategy** (deferred, AD-13) — v1 ships only the per-company `CompanyFactsStrategy`. The pluggable `BackfillStrategy` interface (`core/backfill.py`) is now in place, so the bulk/full-market strategy is a drop-in second implementation with no engine redesign. Ties to the deferred sync-only `EdgarClient.run` (story-1.3 defer) which cannot yet wrap the bulk/async download.

## Deferred from: code review of story-2.3 (2026-07-24)

- **Zero-fact / permanently-unknown-CIK companies are re-checked on every backfill run** (low) — `present_ciks` defines "done" as ≥ 1 row in `raw_fact`, so an in-scope company that lands zero rows (all facts filtered out, genuinely factless, or a `NoCompanyFactsError` from an unknown/delisted CIK) is never "present" and is re-fetched (one live `companyfacts` request) on each subsequent `backfill`. Distinct from the partial-multi-block case above (this is the *zero-legitimate-rows* case). Deferred: the cost is bounded (≈ 0 factless companies among the S&P 500), re-checking is arguably desirable (a company that starts filing gets picked up next run), and recording a "tried-but-empty" marker would require a persisted attempt-ledger — forbidden by AD-1. The README/spec no-op claim was corrected to "skips companies already holding facts." Revisit if a large Universe accrues many permanently-empty CIKs and the wasted requests become material (then reconcile against the Story 2.4 coverage surface, which already derives zero-fact gaps from DB absence).
- **`--refresh` cannot retract a fact removed between ingests** (low) — the insert-only model (AD-6) supersedes still-present identity keys with a higher `version`, but a fact that was in a prior ingest and is absent from the current `companyfacts` has no higher-version row to supersede it, so it lingers on `FINAL` reads. `--refresh` is therefore idempotent for still-present facts, not for deletions/withdrawals. Same class as the deferred re-map/rebuild (story-1.5): a true retract needs a scoped delete/rebuild. The `--refresh` wording was softened. Revisit with a first-class re-map/scrub command.
- **AC-2 full resume CLI wiring + `--refresh` happy-path are not CLI-tested** (low) — the engine skip is tested with a hand-passed `already_present`, and `present_ciks` has integration tests, but the CLI seam that feeds `present_ciks(...)` into `already_present` (and `--refresh` forcing `present = set()`) is not asserted at any level. NFR-7 keeps the backfill network happy-path out of the CLI tests (it would hit live EDGAR). Revisit if a fully-injected CLI harness (fake strategy + fake store) is added so the happy-path wiring can be exercised offline.

## Deferred from: story-2.4 (2026-07-24)

- **`fintin status` reports DB-derived absence, not the specific per-run backfill failure reason** (low, by design — AD-1) — a company that failed during backfill is surfaced as a zero-fact explained gap with the uniform reason "no facts in store" (derived from `raw_fact` absence), not the specific run-time cause (`NoCompanyFactsError`, a mid-transform error, etc.). That specific reason was ephemeral run output (`BackfillFailure(cik, reason)`, shown by `backfill --show-gaps`) and is intentionally not persisted — AD-1 forbids a failures ledger. This is the correct v1 design (ratified in the story-2.3 defer above and the 2.3 hand-off), recorded here for clarity. A durable per-company reason would need a persisted status/failures table (an AD-1 exception), out of v1 scope.

## Deferred from: story-3.1 (2026-07-24)

- **Per-company `companyfacts` re-fetch, not a narrower per-accession fetch** (low, by design — AD-13) — catch-up derives the *distinct affected companies* from the outstanding accessions and re-ingests each company's **full** `companyfacts` (the only v1 fetch strategy; bulk `.zip` deferred). So a company with one new 10-Q re-downloads its whole history (idempotent on read, AD-6). Bounded and correct for a normal catch-up delta (a handful of companies); a narrower per-accession fetch would need a new fetch strategy behind the existing `BackfillStrategy` seam. Revisit only if catch-up deltas become large and the redundant history re-fetch is measurably costly.
- **EDGAR index vs `companyfacts` propagation lag** (low, self-healing) — discovery reads the multi-filer index; ingest reads `companyfacts`. If an accession is already in the index but EDGAR hasn't yet added it to the company's `companyfacts`, catch-up re-ingests the company but that specific filing's facts don't land yet — it reappears in the next run's work list (membership by accession) and lands then. Self-healing via per-run re-derivation (AD-1); no action.
- **Co-filer attribution edge (inherited from the Story 2.2 reconciler, SM-2)** (low) — `compute_work_list` dedups a co-filed accession to its **smallest-CIK** filer and `present_accessions` tests membership by accession alone, so if two *distinct in-scope* CIKs share one accession and the higher-CIK filer's **only** outstanding filing is that co-filing, catch-up derives only the smaller CIK as affected, re-ingests only its `companyfacts`, and — once the accession is present — never fetches the higher-CIK filer's own `companyfacts` for it. Rare (independent large-caps seldom co-file periodic reports; a non-Universe subsidiary is filtered out before dedup) and a property of the **reused** reconciler (Story 3.1 must not re-design it — the epic guardrail). Recorded, not fixed. Revisit by keying membership/attribution on `(accession, cik)` if a real co-filing gap surfaces.

## Deferred from: code review of story-3.1 (2026-07-24)

- **`catch-up` `NOTHING_TO_DO` conflates "store already current" with a non-raising empty index result** (low, near-unreachable) — `fetch_work_candidates` returns `[]` both when the EDGAR index legitimately has no in-window filings AND when `edgar.get_filings(...)` returns `None`/empty *without raising* (its documented behavior for an invalid/out-of-range date). `resolve_window` guarantees `window_start <= window_end <= today` (valid ISO dates), so the `None` path is near-unreachable, and a real discovery error *raises* → exit 1. But a transient non-raising `None`/empty from EDGAR would read as GREEN `NOTHING_TO_DO` (store current) rather than a failed discovery. Inherited from the reconciler adapter (same for `work-list`); a fix would have `fetch_work_candidates`/discovery signal an explicit empty-vs-absent result (beyond 3.1's reuse mandate). Revisit if EDGAR server-side empties are observed masking as "current".

## Deferred from: code review of story-2.4 (2026-07-24)

- **`fintin status`'s high-water mark is store-wide, not Universe-scoped** (low) — `status` renders `high_water_mark(client)` = `max(filed_date)` over all of `raw_fact`, with no CIK filter. If the store holds facts only for CIKs *outside* the current Universe scope (e.g. after a config edit removed a previously-ingested CIK, or a manual `ingest-company` of a non-Universe CIK), the report can print `Coverage: 0 of N in-scope companies present. High-water mark: <a real date>.` — a concrete currency date alongside zero in-scope presence, which can be misread as "in-scope data exists through that date." Not a bug: FR-14 explicitly specifies "the **store's** High-water mark" (so store-wide is what's asked for), the shared `high_water_mark` primitive is deliberately store-wide (the reconciler's scan-sizing hint, AD-16), and the mismatch only arises under Universe-config churn. Revisit by either scoping the HWM to `resolved.ciks` (a new `max(filed_date) WHERE cik IN (...)` repo query — the story deliberately added none) or annotating the line as "(store-wide)" if a Universe-scoped currency figure is wanted.

## Resolved

### Closed by the deferred-work cleanup pass (2026-07-25)
- **No `--debug`/traceback escape hatch on the generic error path** (was: code review of
  story-3.1) — all 13 generic CLI handlers now funnel through `_fail_unexpected`, which
  logs the stack at DEBUG; a root `--debug` flag (or `FINTIN_DEBUG=1`) recovers it. The
  default friendly one-liner UX is unchanged.
- **Purity AST guard is shallow and cwd-dependent** (was: code review of story-3.1) — the
  8 copy-pasted `_module_imports` helpers became `tests/purity.py`: repo-root-relative
  paths, an allowlist-*subset* assertion in place of the 3-name denylist, and a new
  `fintin.adapters.*` rejection asserting the real invariant. (The original note also
  claimed the guard missed function-level imports — it did not; `ast.walk` already
  descends into function bodies.)
- **Drop the now-unused `resolved_fact` + `resolved_fact_mv`** (was: code review of
  story-1.6) — removed from the DDL, with the schema test now asserting they are *not*
  re-created. Existing databases keep the orphans until dropped by hand (see Schema
  migrations, still open).

### Closed by Story 3.2 (the AD-12 filesystem lease + background heartbeat thread)
- **Cool-down is an uninterruptible blocking sleep** (was: code review of story-1.3) — the
  background heartbeat thread beats *through* the main-thread cool-down sleep, so a run in
  cool-down is not reclaimed (AC-3), without making the sleep interruptible.
- **A long backfill inherits the uninterruptible cool-down + no single-flight lease** (was:
  story-2.3) — `backfill` now acquires the shared lease on the same path as `catch-up`.
- **No single-flight lease yet** (was: story-3.1) — `catch-up` acquires the lease and
  returns `ALREADY_RUNNING` (exit 0, no EDGAR request) when a run is already active.

### Closed by Story 1.6 (Approach B — concept resolution derived on read)
- **Concept-dictionary resolution must be recency-aware, not position-first** (was:
  re-review of story-1.5) — the mart resolves each concept as one `argMaxIf` over
  `(filed_date, /A, accession, version, element_position, raw_tag)`, so element position
  is only a same-filing tiebreak.
- **Same-local-name facts across namespaces could collide in one concept** (was: re-review
  of story-1.5) — `raw_tag` is the final deterministic term of that rank (verified present
  in `schema.py`), so a cross-namespace tie cannot occur.

### Moot after the AD-9 pivot
Story 1.5 was reworked so `canonical_concept` = the standard element (1:1 lossless) rather
than an edgartools statistical mapping. With no statistical mapping left to re-map,
version, or disambiguate, these three no longer describe the system:
- **Cross-taxonomy re-map requires a mart rebuild** (was: story-1.5)
- **Standardization mapping version not separately recorded** (was: story-1.5)
- **Ambiguous-tag disambiguation** (was: story-1.5)
