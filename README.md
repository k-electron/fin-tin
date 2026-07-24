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

Never commit your real email — keep it only in your local `fintin.toml`. A
missing or malformed config produces a clear error, not a stack trace.

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
