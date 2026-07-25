# fin-tin

A locally-hosted query tool that turns SEC EDGAR financial disclosures into a
clean, normalized, always-current-enough local corpus you can screen across
companies with plain SQL against ClickHouse.

> **Status:** early development. Runnable skeleton + ClickHouse store schema, and
> the single compliant, rate-limited EDGAR client (identity/User-Agent, rate cap,
> cool-down on throttle). Ingestion lands next.

## Prerequisites

- Python **≥ 3.12**
- [`uv`](https://docs.astral.sh/uv/) (project + dependency management)
- Docker (for the local ClickHouse container)

## Quickstart

```bash
# 1. Install dependencies into a local .venv
uv sync

# 2. Create your local config from the template (fintin.toml is gitignored)
cp fintin.toml.example fintin.toml

# 3. Start ClickHouse 26.3 (single node, persistent named volume)
docker compose up -d

# 4. Verify the app can connect
uv run fintin check-connection
```

`uv run fintin --help` lists all commands. (The `fintin` console script lives in
the project's virtual environment; invoke it via `uv run fintin ...` unless you
have activated `.venv`.)

## Pipeline

Data is derived one way — EDGAR → **Tier 0** (raw) → **Tier 1** (canonical) →
resolution → **wide screening mart**:

```bash
# 1. Create the store schema (idempotent; also refreshes the mart view)
uv run fintin schema-init

# 2. Land one company's raw facts into Tier 0 (hits EDGAR — needs a real
#    contact_email in fintin.toml; rate-limited & fair-access compliant)
uv run fintin ingest-company 320193

# 3. Project Tier 0 → canonical Tier 1. canonical_concept = the standard XBRL
#    element itself (e.g. Assets, RevenueFromContractWithCustomerExcludingAssessedTax),
#    a 1:1 lossless projection. OFFLINE — no EDGAR, so it needs no contact email.
uv run fintin map-canonical 320193
```

Every standard-taxonomy fact projects 1:1 to Tier 1 (the concept is exact and
unambiguous — the FASB element itself, not a statistical standardization). Re-running
either command is idempotent on read (a higher ingest-monotonic `version` supersedes;
readers use `FINAL`).

Two query surfaces expose well-known concepts (`revenues`, `net_income`, `assets`,
`liabilities`, `stockholders_equity`, and more) as columns — screen them with plain SQL.
Each concept resolves to the **latest-filed** value across a curated, ordered list of
*synonymous* elements (so a filer using `SalesRevenueNet` vs
`RevenueFromContractWithCustomerExcludingAssessedTax` still lands under `revenues`, and a
restated period returns the newer value), tie-broken by element list-position, and only
from authoritative periodic reports (10-K/10-Q). The concept→elements lists live in
`fintin/adapters/store/concept_dictionary.py` — add a `ConceptDef` (name, unit,
`duration`/`instant`, ordered elements) and re-run `schema-init` to expose a new column.

- **`screening_wide`** — the main screening surface: one row per income period with the
  balance sheet *as of that period's end* joined on, so a single screen can mix flows
  (income) and stocks (balance sheet) and compute ratios.
- **`screening_mart`** — the base view: one row per `(cik, period_start, period_end)`.
  Because income facts are durations and balance-sheet facts are instants, they land in
  *separate* rows here — use `screening_wide` when combining the two.

```sql
-- companies with annual revenue over $100B, with ROA (a flow ÷ a stock)
SELECT cik, period_end, revenues, net_income,
       round(net_income / assets * 100, 1) AS roa_pct
FROM screening_wide
WHERE revenues > 100e9 AND period_start < period_end
ORDER BY revenues DESC;
```

> **Note:** `schema-init` is create-only for tables, but the mart/`screening_wide` views
> are `CREATE OR REPLACE` — re-run `schema-init` after upgrading to pick up view changes
> on an existing database.

## Configuration

All configuration lives in a single `fintin.toml`. **This repo is public, so
`fintin.toml` is gitignored** — copy the tracked template and edit your local
copy:

```bash
cp fintin.toml.example fintin.toml
```

- **`[clickhouse]`** — connection block; already matches `docker-compose.yml`, so
  it works out of the box.
- **`[edgar]`** — EDGAR fair-access settings (needed once ingestion arrives).
  EDGAR **requires** a real, identifying contact email in the User-Agent, so set
  `contact_email` to **your real address** before running any EDGAR command. The
  EDGAR client refuses to start on a blank/placeholder email (it would otherwise
  send an "Undeclared Automated Tool" User-Agent and risk a ban). `rate_limit_per_sec`
  is capped at the SEC max of 10 req/s and defaults to 9 for margin. On a throttle
  breach the client waits **at least** the ≥ 10-minute cool-down — honoring a
  *longer* `Retry-After` if the SEC sent one, never a shorter one — then retries.
- **`[universe]`** — the screening Universe: a static list of `tickers` and/or
  `ciks` (the S&P 500 in v1). Tickers and CIKs are **public data** (safe to keep
  in the tracked example), unlike your contact email.
- **`[reconcile]`** — work-list tuning. `lookback_days` (default 7) sizes the
  reordering-safe scan window `[high-water-mark − lookback, today]` — how far
  back to re-check EDGAR's index for stragglers and restatements.

Never commit your real email — keep it only in your local `fintin.toml`. A
missing or malformed config produces a clear error, not a stack trace.

### Define your Universe

List the companies you want to screen under `[universe]`, then resolve and
inspect the scope:

```bash
uv run fintin universe            # resolve & report scope + any gaps
uv run fintin universe --show-ciks   # also print the resolved CIK list
```

Tickers resolve to CIKs **offline** via edgartools' bundled reference table — no
EDGAR request and no contact email required. A ticker not in that table is
reported as an **explained gap** (never silently dropped); resolve it by adding
its numeric `cik` directly. The Universe is derived from config on every run
(never stored), so **growing it is a config edit alone** — no code or schema
change.

### Preview outstanding work

`fintin work-list` shows what an ingestion catch-up *would* fetch — the filings in
EDGAR's index over the lookback window (for your Universe) that aren't yet in the
store:

```bash
uv run fintin work-list              # summary: N outstanding filings across M companies
uv run fintin work-list --show-items # also list each accession / cik / form / filed-date
```

It reads EDGAR's **multi-filer index** (one request per calendar quarter the
window spans — not per-company crawling), so it **hits EDGAR and needs a real
contact email** (like ingestion). Per-accession **membership** against the store
is the authority (already-present filings are excluded; a newly-filed amendment
restating an old period shows up); the high-water mark only sizes the scan
window. It's a **read-only dry-run** — it ingests nothing (`fintin catch-up` does
the ingesting, see below).

### Backfill the Universe

Once your Universe is defined (and after `schema-init`), populate the store with
each company's full available history:

```bash
uv run fintin schema-init            # once — creates Tier 0/1, the MV, and the mart
uv run fintin backfill               # ingest every in-scope company's full history
uv run fintin backfill --show-gaps   # also list any companies recorded as explained gaps
uv run fintin backfill --refresh     # re-ingest even companies already present (supersedes on read)
```

Backfill fetches each company's entire history in **one `companyfacts` request**
(the request-minimizing per-company strategy), through the same rate-limited
client, and **commits per company**. It **hits EDGAR and needs a real contact
email**.

- **Resumable, no checkpoint file.** Companies that already hold facts in the
  store are skipped (without even re-fetching), so an interrupted backfill just
  re-run resumes where it left off — resumption is re-derived from the store each
  run, never from a saved cursor. Re-running is therefore a no-op for companies
  that landed facts; a company that returned *no* facts is re-checked each run (a
  bounded cost that also lets a newly-filing company get picked up). `--refresh`
  re-ingests everything, superseding prior values on read — note the insert-only
  model supersedes still-present facts but cannot retract one removed since.
- **Failures are explained gaps, not crashes.** A company with no facts or a fetch
  error is recorded `(cik, reason)` and the run continues to the next company — no
  silent omissions (`--show-gaps` lists them; the coverage report surfaces them).
- **Throttle aborts, by design.** If EDGAR throttles and the client exhausts its
  cool-down retries, the run stops rather than continuing to hammer EDGAR —
  ban-safety always outranks finishing faster. The run likewise aborts if many
  companies fail in a row (a systemic problem, e.g. the store dropped mid-run),
  rather than spending EDGAR requests it can't persist.

The backfill strategy is pluggable: the per-company `companyfacts` API is the v1
strategy; a bulk-download strategy for a much larger Universe can drop in behind
the same interface with no redesign.

### Catch up to today

Once the store has been backfilled, `fintin catch-up` brings it current — ingesting
everything filed since, any time, idempotently:

```bash
uv run fintin catch-up               # STARTED→COMPLETED, or NOTHING_TO_DO if current
uv run fintin catch-up --show-gaps   # also list any companies recorded as explained gaps
```

It **reuses the work-list mechanism** (`fintin work-list` previews exactly what it
will fetch): EDGAR's multi-filer index over the lookback window, minus the
accessions already in the store. It then re-ingests the **affected companies'** full
`companyfacts` through the same rate-limited client — so a newly-filed report, and
any **restatement** of an older period, lands and wins on read (latest-filed-wins).
Like backfill it **hits EDGAR and needs a real contact email**.

- **Success vocabulary, all exit-0.** A non-empty run reports `STARTED`→`COMPLETED`;
  an already-current store reports `NOTHING_TO_DO` (and makes no `companyfacts`
  request). Neither is an error — safe to wire to a scheduler or run right before a
  screen ("catch up, then query").
- **Failures are explained gaps, not crashes** (`--show-gaps` lists them), exactly
  as in backfill.
- **Throttle / systemic abort → exit 1.** An exhausted EDGAR cool-down, or many
  companies failing in a row (e.g. the store dropped mid-run), stops the run rather
  than continuing — ban-safety outranks finishing.

Catch-up derives its work fresh from the store + EDGAR's index every run — no cursor
or "last run" marker (a crash just resumes on the next trigger).

### Single-flight (one run at a time)

`backfill` and `catch-up` are both EDGAR-heavy — running two at once would double
the request rate toward a ban. They share a **single-flight self-expiring lease**
(a local lock file, `[lease].path`, default `fintin.lease`; **not** ClickHouse), so
at most one runs at a time:

- A trigger arriving while another run is active returns **`ALREADY_RUNNING`
  (exit-0)** and issues **no EDGAR request** — it coalesces, it doesn't queue. Safe
  to wire to a scheduler or a "catch up before I query" button.
- The lease **self-expires**: a run refreshes it every `heartbeat_seconds`, and it's
  considered dead `ttl_seconds` after the last heartbeat. So a crashed run never
  deadlocks the tool — the next trigger reclaims the stale lease and resumes the
  (DB-derived) remaining work.
- A run paused in an EDGAR **cool-down** keeps heartbeating (a background thread), so
  its lease is not reclaimed while it waits out a throttle.

The lease file holds only `{token, pid, host, timestamps, ttl}` — no secrets — and
is runtime state (gitignored). Tune `path` / `ttl_seconds` / `heartbeat_seconds` under `[lease]`
in `fintin.toml` (heartbeat must be ≤ half the TTL).

### Check coverage & status

`fintin status` reports how much of your Universe is ingested and what's still an
explained gap — no silent omissions:

```bash
uv run fintin status              # coverage summary + gap counts
uv run fintin status --show-gaps  # also list every explained gap
```

It reports the **count of in-scope companies present**, the **high-water mark**
(the latest `filed_date` in the store, or a note that the store is empty), and
every **explained gap** in two classes: unresolvable tickers from your config, and
in-scope companies with **zero facts** in the store. A company that failed during
backfill is exactly one absent from the store, so it shows as `no facts in store`
(the durable, DB-derived state — the specific per-run failure reason is shown at
backfill time by `backfill --show-gaps`).

`status` is **offline** — it reads ClickHouse and the bundled reference table
only, so it needs **no contact email** and makes no EDGAR request. Gaps don't
change the exit code (they're a known, reported state); only a bad config or an
unreachable store fails loudly.

## Testing

```bash
uv run pytest            # unit tests; integration tests auto-skip if ClickHouse is down
uv run pytest -m integration   # run only the container-dependent tests (needs `docker compose up`)
```

Integration tests connect to `localhost:8123` and are skipped automatically when
ClickHouse is unreachable, so the default suite stays green without Docker.

The EDGAR client tests **never hit live EDGAR** (a ban risk): they drive the
cool-down logic with an injected recording sleeper and assert request headers via
an in-process httpx `MockTransport`. No network is touched by the test suite.

## Volume persistence

The corpus persists across container restarts via the `fintin_ch_data` named
volume. `docker compose down` (without `-v`) then `docker compose up -d`
preserves data; `docker compose down -v` deletes it.

### Verifying persistence (manual)

A single automated test can't restart the container, so verify persistence
manually (auth uses the `default` user + the password from `fintin.toml`):

```bash
CH=http://localhost:8123/
AUTH=(-H "X-ClickHouse-User: default" -H "X-ClickHouse-Key: fintin_local")

# 1. write a row
curl -s "$CH" "${AUTH[@]}" --data-binary \
  "CREATE TABLE IF NOT EXISTS persist_check (x UInt8) ENGINE = MergeTree ORDER BY x"
curl -s "$CH" "${AUTH[@]}" --data-binary "INSERT INTO persist_check VALUES (42)"

# 2. restart WITHOUT deleting the volume
docker compose down && docker compose up -d
# (wait until http://localhost:8123/ping returns 200)

# 3. confirm the row survived, then clean up
curl -s "$CH" "${AUTH[@]}" --data-binary "SELECT x FROM persist_check"   # -> 42
curl -s "$CH" "${AUTH[@]}" --data-binary "DROP TABLE persist_check"
```

## Notes

- The BMad tooling scripts under `_bmad/` require Python ≥ 3.11; the fin-tin
  project itself targets Python ≥ 3.12.
