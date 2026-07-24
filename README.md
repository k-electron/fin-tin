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
