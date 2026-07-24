# Deferred Work

## Deferred from: code review of story-1.2 (2026-07-23)

- **Schema migrations** (medium) — `fintin/adapters/store/schema.py` uses `CREATE … IF NOT EXISTS`, which silently keeps a stale table/MV definition if its DDL later changes; only the `screening_mart` view (`CREATE OR REPLACE`) is refreshed. There is no migration/versioning story, so a DDL change (e.g. the F1 resolution-rank fix) won't apply to an already-created deployment without a manual drop/recreate. Deferred because the v1 schema is still stabilizing and this is a solo local tool; a create-only limitation note is documented in `schema.py` now. Revisit with a proper migration mechanism when the schema settles or a second environment appears.

## Deferred from: code review of story-1.3 (2026-07-23)

- **No singleton enforcement on `EdgarClient`** (medium) — every `EdgarClient(...)` mutates process-global edgar state (`EDGAR_IDENTITY`, `EDGAR_RATE_LIMIT_PER_SEC`, `httpclient.HTTP_MGR`); a second construction at a different rate silently replaces the first (last-writer-wins), and a construction landing mid-request could close the prior client's transport. Deferred: v1 is single-process and ingestion is single-flight (AD-12, Epic 3), so concurrent clients aren't a real scenario yet. Revisit when the lease/heartbeat lands or if a second concurrent EDGAR consumer appears — enforce a module-level `configure_edgar()`-once or a singleton.
- **`run()` is sync-only** (medium) — it can wrap edgartools' synchronous per-company path (v1, AD-13) but not the async fetchers or the bulk `companyfacts.zip` download, which raise `TooManyRequestsError` on their own coroutine path and would throttle outside the cool-down policy. Deferred: the bulk strategy is explicitly deferred (AD-13) and v1 uses only the sync per-company API. Add an async `run` variant (or a shared cool-down helper both call) when the bulk/async path is built.
- **Cool-down is an uninterruptible blocking sleep** (low) — a single `self._sleep(wait)` (up to ≥10 min) with no countdown/progress and no clean mid-wait Ctrl-C. Deferred: ties directly to the AD-12 single-flight lease, which needs the run to keep heartbeating *during* an EDGAR cool-down (Epic 3). The sleep is already injectable so a future heartbeating/interruptible sleeper drops in. Revisit with the lease.

## Deferred from: code review of story-1.4 (2026-07-24)

- **ClickHouse `Date` range guard** (low) — `raw_fact.period_start`/`period_end`/`filed_date` are `Date` (1970-01-01 … 2149-06-06). An out-of-range or corrupt date (typo, malformed XBRL) would be clamped/rejected by clickhouse-connect, desyncing it from the pre-insert `content_hash` and corrupting the identity key. Deferred: unreachable for real SEC/XBRL data (EDGAR XBRL is post-~2009, well inside the window). Revisit by widening to `Date32` or range-checking dates in the transform when at-rest integrity validation (the deferred AD-14 scrub) is built.
