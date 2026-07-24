# fin-tin

A locally-hosted query tool that turns SEC EDGAR financial disclosures into a
clean, normalized, always-current-enough local corpus you can screen across
companies with plain SQL against ClickHouse.

> **Status:** early development. Story 1.1 (runnable skeleton) — a `uv`-managed
> Python project with a Typer CLI and a local ClickHouse it connects to.

## Prerequisites

- Python **≥ 3.12**
- [`uv`](https://docs.astral.sh/uv/) (project + dependency management)
- Docker (for the local ClickHouse container)

## Quickstart

```bash
# 1. Install dependencies into a local .venv
uv sync

# 2. Start ClickHouse 26.3 (single node, persistent named volume)
docker compose up -d

# 3. Verify the app can connect
uv run fintin check-connection
```

`uv run fintin --help` lists all commands. (The `fintin` console script lives in
the project's virtual environment; invoke it via `uv run fintin ...` unless you
have activated `.venv`.)

## Configuration

All configuration lives in a single `fintin.toml`. Story 1.1 uses only the
`[clickhouse]` connection block; it must match the `docker-compose.yml` service.
A missing or malformed config produces a clear error, not a stack trace.

## Testing

```bash
uv run pytest            # unit tests; integration tests auto-skip if ClickHouse is down
uv run pytest -m integration   # run only the container-dependent tests (needs `docker compose up`)
```

Integration tests connect to `localhost:8123` and are skipped automatically when
ClickHouse is unreachable, so the default suite stays green without Docker.

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
