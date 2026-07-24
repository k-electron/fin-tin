---
baseline_commit: 30ca969907e30228399b7f72ac5306cc0f6cc9dd
---

# Story 1.1: Runnable skeleton connected to ClickHouse

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the builder,
I want a uv-managed Python project with a Typer CLI and a local ClickHouse it connects to,
so that I have a running foundation to build the pipeline on.

## Acceptance Criteria

1. **Given** a clean checkout **When** I run `uv sync` then `docker compose up -d` **Then** ClickHouse 26.3 starts with a mounted volume **And** `fintin --help` lists the CLI. *(Console script is invoked as `uv run fintin --help` unless the `.venv` is activated.)*
2. **Given** the container is running **When** I run the connection-check command **Then** the app connects via `clickhouse-connect` using `fintin.toml` **And** reports success (a clear human-readable "connected" line, exit 0).
3. **Given** `fintin.toml` is missing or malformed **When** any command runs **Then** it fails with a clear config error (what's wrong + which key/file), not a Python stack trace, and a non-zero exit code.
4. **Given** the container is stopped and restarted (`docker compose down` — **without** `-v` — then `up -d`) **When** it returns **Then** data on the mounted volume persists (verified by a table/row surviving the restart).

## Tasks / Subtasks

- [x] **Task 1 — uv project scaffold** (AC: 1)
  - [x] `pyproject.toml` with `[project]` `requires-python = ">=3.12"`, name `fin-tin`, and a `[project.scripts]` entry `fintin = "fintin.cli.app:main"`.
  - [x] Runtime deps needed *now*: `typer==0.27.0`, `clickhouse-connect==1.6.0`. Dev deps: `pytest`. **Do NOT add `edgartools` yet** — it lands in Story 1.3 (keep the dep set minimal to what this story uses).
  - [x] `uv sync` produces a `.venv` and a committed `uv.lock`.
  - [x] Package skeleton (all `snake_case` under `fintin/`, each an importable package with `__init__.py`): `fintin/`, `fintin/core/`, `fintin/adapters/`, `fintin/adapters/edgar/`, `fintin/adapters/store/`, `fintin/adapters/lease/`, `fintin/cli/`.
- [x] **Task 2 — Typer CLI skeleton** (AC: 1, 2)
  - [x] `fintin/cli/app.py` defines a Typer `app` and a `main()` entry point (`main()` calls `app()`).
  - [x] `fintin --help` lists commands. Register the connection-check command here; keep the CLI a **dumb trigger** — it parses args, calls inward, prints results; **no business logic in the CLI** (AD-2).
  - [x] Structured logging to **stdout** (not stderr-only); a `--verbose`/log-level flag is optional, not required.
- [x] **Task 3 — Config loader** (AC: 2, 3)
  - [x] `fintin/config.py`: load `fintin.toml` with stdlib `tomllib` (Python ≥3.12 — no external TOML dependency). Parse into a typed model (dataclass/`NamedTuple`).
  - [x] This story only *requires and validates* the `[clickhouse]` connection block (`host`, `port`, `username`, `password`, `database`). Tolerate other blocks being present or absent — later stories add `[universe]`, `[edgar]`, `[lease]`, `LOOKBACK`, rate ceiling. Do not hard-fail on their absence.
  - [x] Missing file, unreadable file, malformed TOML, or missing/blank required `[clickhouse]` keys → raise a domain `ConfigError` caught at the CLI boundary and rendered as a clear message + non-zero exit (AC-3). No raw traceback to the user.
  - [x] Provide a working `fintin.toml` (or `fintin.toml.example` + copy) whose `[clickhouse]` matches the docker-compose service.
- [x] **Task 4 — ClickHouse connection (store adapter)** (AC: 2)
  - [x] `fintin/adapters/store/client.py`: a factory that builds a `clickhouse_connect` client from the config `[clickhouse]` block. **The store adapter is the only component that talks to ClickHouse** (AD-18 sets this ownership; DDL itself is Story 1.2 — do not create schema here).
  - [x] The connection-check command opens a client and runs a trivial round-trip (`client.query('SELECT 1')` returning `1`, or `client.ping()`), prints success, exits 0. On failure, print a clear "cannot reach ClickHouse at host:port" message + non-zero exit (not a traceback).
- [x] **Task 5 — docker-compose for ClickHouse 26.3** (AC: 1, 4)
  - [x] `docker-compose.yml`: single `clickhouse/clickhouse-server:26.3` service, HTTP port `8123` published (clickhouse-connect default), native `9000` optional, a **named volume** mounted at `/var/lib/clickhouse` so the corpus persists across `down`/`up`.
  - [x] Default `default` user, empty password is acceptable for local single-node; if you set a password, mirror it in `fintin.toml`.
- [x] **Task 6 — Test harness + fixtures scaffold** (AC: 1, 2, 3, 4)
  - [x] `tests/` package + `tests/fixtures/` directory (empty placeholder now; EDGAR fixtures arrive in Story 1.3+). Add `tests/conftest.py`.
  - [x] Unit tests (no container needed): `fintin --help` exits 0 and lists the connection-check command (Typer `CliRunner`); config loader accepts a valid TOML and raises `ConfigError` (not a bare exception) for missing-file and malformed-TOML cases.
  - [x] Integration test for the live connection (AC-2), marked `@pytest.mark.integration` and auto-skipped only when the container isn't listening, so the default `uv run pytest` stays green without Docker. **AC-4 volume persistence** is verified via a documented **manual** procedure in the README (a single pytest run cannot restart the container).
- [x] **Task 7 — Docs** (AC: all)
  - [x] Short `README.md` quickstart: `uv sync` → `docker compose up -d` → `uv run fintin check-connection`. Note that BMad tooling scripts need Python ≥3.11, but the project itself targets ≥3.12.

### Review Findings

_Adversarial code review (2026-07-23) — 3 parallel layers (Blind Hunter, Edge Case Hunter, Acceptance Auditor). Triage: 0 decision-needed, 8 patch, 0 deferred, 1 dismissed. No Critical/High correctness defects; no scope-fence or architecture violations. AC-1/AC-2/AC-3 fully satisfied and tested; AC-4 mechanism correct + manually verified (see finding below)._

- [x] [Review][Patch] (medium) ClickHouse client never closed — leaked connection pool [fintin/adapters/store/client.py: get_client / check_connection]
- [x] [Review][Patch] (medium) Integration probe runs on every pytest run, unconditionally + no timeout — undercuts "green without Docker" (hangs on a drop-packet/half-up host) [tests/conftest.py: pytest_collection_modifyitems]
- [x] [Review][Patch] (medium) All connection failures reported as "Cannot reach …" — masks auth/wrong-db errors and makes integration tests silently SKIP on a misconfigured password/db [fintin/adapters/store/client.py: check_connection]
- [x] [Review][Patch] (medium) AC-4 persistence: Task 6 subtask is checked for an automated persistence test that does not exist (round-trip test stays within one container lifetime); README's "Volume persistence" section states data persists but gives no verification procedure [tests/test_connection.py; README.md; story Task 6]
- [x] [Review][Patch] (low) Config validation gaps: boolean `port` accepted (bool ⊂ int), no port range check (0/negative pass), non-string host/user/password/database silently coerced by `str()` [fintin/config.py: _parse_clickhouse]
- [x] [Review][Patch] (low) Round-trip test hygiene: fixed table name in the real DB, non-deterministic `>= 1` assertion, xdist race, orphaned table if killed before `finally` [tests/test_connection.py: test_read_write_round_trip]
- [x] [Review][Patch] (low) UTF-8 BOM in fintin.toml rejected as "Malformed TOML" (decode with utf-8-sig) [fintin/config.py: load_config]
- [x] [Review][Patch] (low) Structured-logging setup is dead code — `_configure_logging` + `logger` are wired but nothing ever logs [fintin/cli/app.py: _configure_logging]

_Dismissed (1): "unknown keys within [clickhouse] silently ignored" — over-engineering for a local v1 (no TLS in scope); later stories add config sections by design._

## Dev Notes

### What this story IS
The **walking-skeleton foundation** (Build-Split Epic A, scaffold slice): a runnable `uv`/Python project, a Typer CLI that's a dumb trigger over a (soon-to-exist) engine core, a single-node ClickHouse in Docker with a persistent volume, a TOML config loader, and `clickhouse-connect` wiring proven by a connection check. Plus the empty test/fixtures scaffold.

### What this story is NOT (hard scope fences — do not implement here)
- ❌ **No schema/DDL.** Tier 0 / Tier 1 / Resolution MV / wide mart are **Story 1.2** (`adapters/store` is their sole owner per AD-18, created before any insert per AD-18). Create the `adapters/store` package now, but only the connection factory — no `CREATE TABLE`.
- ❌ **No EDGAR client / edgartools dependency.** That's **Story 1.3** (AD-3). Do not add `edgartools` to `pyproject.toml`.
- ❌ **No ingestion, mapping, resolution, lease, backfill, or `status`/coverage command.** Those are Stories 1.4–1.6 / Epics 2–3.
- The `status` command (FR-14) is **not** this story — the connection health check is a *separate, minimal* command. Don't name it `status`.

### Architecture patterns & constraints
- **Ports & Adapters (Hexagonal).** `core/` is pure (no I/O, depends on nothing outward); `adapters/*` implement ports; `cli/` is a driving adapter. Dependencies point **inward** only. [Source: ARCHITECTURE-SPINE.md#Design-Paradigm, #Invariants-&-Rules]
- **AD-2 — pure engine, dumb triggers.** The CLI must carry no policy/logic; it invokes inward. Establish this discipline at the skeleton stage. [Source: ARCHITECTURE-SPINE.md#AD-2]
- **AD-18 — single DDL owner.** `adapters/store` is the *only* component that issues ClickHouse DDL and (here) owns the connection. No DDL in this story. [Source: ARCHITECTURE-SPINE.md#AD-18]
- **Config = single TOML.** `fintin.toml` will eventually hold: Universe (CIK/ticker list), rate ceiling, identifying User-Agent + contact email, ClickHouse connection, lease path, `LOOKBACK`. This story wires the loader and the `[clickhouse]` block only; no secrets store (local). [Source: ARCHITECTURE-SPINE.md#Consistency-Conventions (Logging & config)]
- **Deployment envelope.** Single local node (macOS): `fintin` CLI+engine (uv/Python) → `clickhouse-connect` → ClickHouse 26.3 (Docker Compose) with a mounted volume; a filesystem lease file and EDGAR access arrive later. [Source: ARCHITECTURE-SPINE.md#Structural-Seed (Deployment)]
- **Structured logging to stdout.** [Source: ARCHITECTURE-SPINE.md#Consistency-Conventions]
- **Errors & status.** Runs fail loudly (clear message, non-zero exit) except throttle/active-run cases (not relevant here). A config error is a loud failure. [Source: ARCHITECTURE-SPINE.md#Consistency-Conventions (Errors & status)]

### Pinned stack (authoritative — web-verified 2026-07-23 by the architecture; do not substitute versions)
| Component | Version | Used in this story |
| --- | --- | --- |
| Python | ≥ 3.12 | yes (`requires-python`); enables stdlib `tomllib` |
| ClickHouse (server) | 26.3 (LTS) | yes (`clickhouse/clickhouse-server:26.3`) |
| clickhouse-connect | 1.6.0 | yes (connection) |
| Typer | 0.27.0 | yes (CLI) |
| uv (tooling) | 0.11.32 | yes (project mgmt) |
| Docker Compose | host-provided | yes |
| edgartools | 5.43.0 | **NO — Story 1.3** |
[Source: ARCHITECTURE-SPINE.md#Stack; BUILD-SPLIT.md#Epic-A]

### Source tree components to touch (target layout)
```text
fin-tin/
  pyproject.toml            # NEW — uv-managed; requires-python >=3.12; [project.scripts] fintin
  uv.lock                   # NEW — committed
  docker-compose.yml        # NEW — ClickHouse 26.3 single node + named volume
  fintin.toml               # NEW — config; [clickhouse] block active this story
  README.md                 # NEW — quickstart
  fintin/
    __init__.py             # NEW
    config.py               # NEW — tomllib loader + typed model + ConfigError
    core/__init__.py        # NEW (placeholder — pure engine lands later)
    adapters/
      __init__.py           # NEW
      edgar/__init__.py      # NEW (placeholder — Story 1.3)
      store/__init__.py      # NEW
      store/client.py        # NEW — clickhouse-connect client factory + connection check helper
      lease/__init__.py      # NEW (placeholder — Story 3.2)
    cli/
      __init__.py           # NEW
      app.py                # NEW — Typer app, main(), connection-check command
  tests/
    __init__.py             # NEW
    conftest.py             # NEW
    fixtures/                # NEW (empty placeholder — EDGAR fixtures in Story 1.3+)
    test_cli.py             # NEW — --help lists commands
    test_config.py          # NEW — valid load + ConfigError on missing/malformed
    test_connection.py      # NEW — @pytest.mark.integration (needs container)
```
[Source: ARCHITECTURE-SPINE.md#Structural-Seed (Source tree)]

### Concrete reference snippets (adapt; these are starting points, not gospel)

`docker-compose.yml`:
```yaml
services:
  clickhouse:
    image: clickhouse/clickhouse-server:26.3
    ports:
      - "8123:8123"   # HTTP — clickhouse-connect default
      - "9000:9000"   # native (optional)
    volumes:
      - fintin_ch_data:/var/lib/clickhouse
    ulimits:
      nofile: { soft: 262144, hard: 262144 }
volumes:
  fintin_ch_data:
```

`fintin.toml` (`[clickhouse]` active this story; other blocks are forward-looking placeholders the loader tolerates):
```toml
[clickhouse]
host = "localhost"
port = 8123
username = "default"
password = ""
database = "default"
```

`clickhouse-connect` round-trip (in `adapters/store/client.py`):
```python
import clickhouse_connect
client = clickhouse_connect.get_client(host=cfg.host, port=cfg.port,
                                       username=cfg.username, password=cfg.password,
                                       database=cfg.database)
assert client.query("SELECT 1").result_rows[0][0] == 1
```

### Testing standards
- **Framework:** `pytest`, run via `uv run pytest`. Default run must stay green **without Docker** — gate the live-connection/persistence checks behind `@pytest.mark.integration` (or skip-if-unreachable).
- **No live EDGAR, ever** — the project rule is that EDGAR-touching code tests against recorded fixtures (not applicable to this story since it touches no EDGAR, but the `tests/fixtures/` scaffold is created here for Story 1.3+). [Source: ARCHITECTURE-SPINE.md#Consistency-Conventions (Testing); epics.md Additional Requirements]
- **CI is out of v1 scope** (single-operator local tool); tests run locally. [Source: epics.md Additional Requirements — CI/CD]
- AC-4 persistence test: create a throwaway table, insert a row, `docker compose down` (no `-v`), `up -d`, assert the row survives; drop the table to leave a clean state.

### Project Structure Notes
- Layout matches the architecture's prescribed source tree exactly (packages `snake_case` under `fintin/`, one adapter package per port). No variances.
- The `fintin` console script requires `[project.scripts] fintin = "fintin.cli.app:main"`; with uv the invocation is `uv run fintin ...` unless `.venv` is activated — call this out in the README so AC-1's "`fintin --help`" isn't misread as a bare global command.
- Connection-check command name is a **suggestion** (`check-connection`); pick a clear name that is NOT `status` (reserved for FR-14 / Story 2.4).

### References
- [Source: _bmad-output/planning-artifacts/epics.md#Story-1.1] — user story + ACs
- [Source: _bmad-output/planning-artifacts/epics.md#Epic-1] — walking-skeleton intent; one CIK is a *test vehicle*, pipeline stays general
- [Source: _bmad-output/planning-artifacts/architecture/architecture-fin-tin-2026-07-23/ARCHITECTURE-SPINE.md#Stack] — pinned versions
- [Source: .../ARCHITECTURE-SPINE.md#AD-2] — pure engine / dumb trigger
- [Source: .../ARCHITECTURE-SPINE.md#AD-18] — store adapter sole DDL/connection owner
- [Source: .../ARCHITECTURE-SPINE.md#Structural-Seed] — source tree + deployment envelope
- [Source: .../ARCHITECTURE-SPINE.md#Consistency-Conventions] — config, logging, errors, testing
- [Source: _bmad-output/planning-artifacts/architecture/architecture-fin-tin-2026-07-23/BUILD-SPLIT.md#Epic-A] — scaffold slice deliverables
- [Source: _bmad-output/planning-artifacts/prds/prd-fin-tin-2026-07-23/prd.md#8] — NFR-5 portability (local macOS, containerized ClickHouse, Python CLI)

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Claude Opus 4.8, 1M context) — bmad-dev-story workflow.

### Debug Log References

- All 4 ACs verified against a live `clickhouse/clickhouse-server:26.3` container (`26.3.17.56`).
- Full suite: **11 passed** (9 unit + 2 integration). Without Docker: 9 passed, 2 integration auto-skipped (verified).

### Completion Notes List

- Ultimate context engine analysis completed — comprehensive developer guide created.
- **All 4 acceptance criteria verified:** AC-1 (`uv sync` + `docker compose up -d` → ClickHouse 26.3 on a mounted volume, `fintin --help` lists the CLI); AC-2 (`fintin check-connection` → "Connected to ClickHouse 26.3.17.56 …", exit 0); AC-3 (missing/malformed config → clear `Config error`, exit 2, no traceback); AC-4 (row `42` survived `docker compose down`/`up -d`).
- **Deviation 1 — Typer group mode.** A Typer app with a single command collapses to single-command mode, so `fintin check-connection` was rejected as an extra arg. Added a no-op `@app.callback()` so the CLI is a multi-command group now (correct for the forthcoming catch-up/backfill/status commands). Not a workaround — this is the intended CLI shape.
- **Deviation 2 — ClickHouse 26.3 requires a password for `default`.** The PRD/architecture assumed a local empty password; CH 26.3 rejects empty-password `default` auth (`REQUIRED_PASSWORD`, code 194). Set a local dev password via `CLICKHOUSE_PASSWORD` (compose) mirrored in `fintin.toml` (`password = "fintin_local"`). Consistent with the architecture's "config carries connection details, no secrets store — local". ⚠️ **Flag for planning:** the "empty local password" assumption in the PRD/architecture is invalid for CH ≥ 26.3 — a downstream story (1.2) and any docs should treat a non-empty `[clickhouse].password` as the norm.
- **Environment notes:** `uv` selected CPython 3.14.6 (satisfies `requires-python >=3.12`; `tomllib` present). `uv` on this machine is 0.11.29 vs the architecture's pinned 0.11.32 — tooling-only, non-blocking.
- **Scope honored:** no schema/DDL (Story 1.2), no EDGAR client / `edgartools` dep (Story 1.3), no ingestion. `adapters/store` holds only the connection factory.

### Change Log

- 2026-07-23 — Story 1.1 implemented: uv/Python scaffold, Typer CLI group with `check-connection`, `fintin.toml` config loader (`tomllib`) with `ConfigError`, ClickHouse `docker-compose.yml` (26.3, named volume, local password), store-adapter connection client, unit + integration test suite. All ACs verified; status → review.
- 2026-07-23 — Code review (adversarial, 3 layers). 8 patch findings applied, 1 dismissed: client lifecycle (close on all paths); integration gating now probes only when integration tests are collected, via a timed TCP socket, and no longer masks a reachable-but-misconfigured server; config validation (reject bool/out-of-range port, non-string values); UTF-8 BOM tolerance; round-trip test hygiene (unique table, exact-value assert); structured logging now actually emits; AC-4 persistence verification documented as a manual procedure in the README (subtask wording corrected). Suite: 15 passed. Status → done.

### File List

**New:**
- `pyproject.toml`
- `uv.lock`
- `docker-compose.yml`
- `fintin.toml`
- `README.md`
- `fintin/__init__.py`
- `fintin/config.py`
- `fintin/core/__init__.py`
- `fintin/adapters/__init__.py`
- `fintin/adapters/edgar/__init__.py`
- `fintin/adapters/store/__init__.py`
- `fintin/adapters/store/client.py`
- `fintin/adapters/lease/__init__.py`
- `fintin/cli/__init__.py`
- `fintin/cli/app.py`
- `tests/__init__.py`
- `tests/conftest.py`
- `tests/test_cli.py`
- `tests/test_config.py`
- `tests/test_connection.py`

**Modified:**
- `.gitignore` (added Python ignores)
