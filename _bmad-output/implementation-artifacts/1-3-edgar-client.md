# Story 1.3: Compliant rate-limited EDGAR client

Status: ready-for-dev

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the builder,
I want one EDGAR client that obeys SEC fair-access,
so that every fetch is safe from a ban.

## Acceptance Criteria

1. **Given** the client is configured **When** it makes any request **Then** it sends the configured identifying User-Agent (name + contact email, form `Name contact-email`) **And** `Accept-Encoding: gzip, deflate` **And** its rate is capped at **edgartools' own throttle** ≤ 10 req/s (never a naive per-call wrapper) (AD-3, FR-1).
2. **Given** EDGAR returns a throttle failure **When** the client detects it **Then** it honors `Retry-After` if present, else self-imposes a **≥ 10-minute cool-down**, then retries — **without crashing the run** (a typed, catchable domain error only if retries are exhausted; never an unhandled library exception).
3. **Given** any code path touches EDGAR **Then** it goes through this one client — no direct HTTP and no `edgar` import anywhere else in `fintin/` — **verified by tests/construction** (AD-3).
4. **Given** tests run **Then** they use recorded fixtures / injected fakes and **never** hit live EDGAR (ban risk; Testing convention).

## Tasks / Subtasks

- [ ] **Task 1 — `[edgar]` config block → validated `EdgarConfig`** (AC: 1)
  - [ ] Extend `fintin/config.py`: add a frozen `EdgarConfig` dataclass — `user_agent_name: str`, `contact_email: str`, `rate_limit_per_sec: float` (default `10.0`), `cooldown_seconds: int` (default `600`), `max_throttle_retries: int` (default `3`).
  - [ ] Parse an optional `[edgar]` section in `load_config`; add `edgar: EdgarConfig | None` to `Config`. **Absent `[edgar]` → `edgar = None`** (so `check-connection` / `schema-init` still work without it). **Present-but-structurally-invalid `[edgar]` → `ConfigError`** (fail loudly), consistent with the existing `[clickhouse]` validation style.
  - [ ] **Load-time validation = STRUCTURE + TYPES + RANGES ONLY** (see the regression warning below — do NOT reject a placeholder email here):
    - `user_agent_name`: a `str` (key present).
    - `contact_email`: a `str` (key present).
    - `rate_limit_per_sec`: a number with `0 < x <= 10` (10 = SEC published max; > 10 is a ban risk — reject; AD-3). Reject `bool` explicitly (as the existing `port` guard does).
    - `cooldown_seconds`: an `int` `>= 600` (the SEC-documented 10-minute cool-down floor; smaller is non-compliant — reject). Reject `bool`.
    - `max_throttle_retries`: an `int` `>= 0`. Reject `bool`.
  - [ ] 🚨 **REGRESSION WARNING — do not put the blank/placeholder-email rejection in `load_config`.** `fintin.toml` is loaded by `check-connection`, `schema-init`, **and `tests/conftest.py`** (`_local_config()` catches `ConfigError` and returns `None`). If `load_config` raised on the shipped placeholder email, **every** command would break and **all ClickHouse integration tests would silently skip** (masking real misconfig — the exact failure Story 1.1's review fixed). Therefore: a well-formed `[edgar]` block (valid types/ranges, placeholder email) **must load cleanly**. The ban-safety semantic gate (blank / bad-format / placeholder email) lives in **`EdgarClient` construction** — Task 2 — the moment before any EDGAR call.
  - [ ] The `[edgar]` block goes in the **tracked `fintin.toml.example`** (placeholder email) and the **gitignored real `fintin.toml`** — see Task 5's config-hygiene split (public repo: no email in tracked files). Update the header comment (Story 1.1 said `[edgar]` is "tolerated but not required") to reflect that `[edgar]` is now defined and required *for EDGAR access*.

- [ ] **Task 2 — The single EDGAR client (sole `edgar` owner, AD-3)** (AC: 1, 3)
  - [ ] New `fintin/adapters/edgar/client.py` — the **only** module in `fintin/` that imports `edgar` / issues EDGAR requests. Expose an `EdgarClient` (a small class is preferred over module globals for testability — see Task 4's injection points).
  - [ ] **Ban-safety gate (this is where FR-1 "never send a blank/undeclared UA" is enforced structurally).** On construction, before configuring identity, validate `config.edgar`: raise a typed domain error (`EdgarConfigError` — define it here, or reuse `ConfigError`) if `config.edgar is None` (no `[edgar]` block), if `user_agent_name` is blank, or if `contact_email` is blank / malformed (must contain `@` with a non-empty local part and a dotted domain) / a **known placeholder** (reject `you@example.com`, `your.email@example.com`, `changeme@example.com`, `example@example.com`). This gate — not `load_config` — is what forces a real address before any EDGAR call (see Task 1's regression warning).
  - [ ] On construction (after the gate passes), configure edgartools' **global** state from `EdgarConfig`, deterministically (do not rely on ambient `EDGAR_IDENTITY` / `EDGAR_NAME` env vars):
    - Set the declared identity via `edgar.set_identity(...)`. ⚠️ **Verify the exact 5.43.0 signature by reading the installed package** (`edgar/__init__.py`, `edgar/httprequests.py`) — it is either a single positional string `"Name contact-email"` or kwargs `name=/email=/organization=`. Build the identity from `user_agent_name` + `contact_email` in the FR-1 form `Name contact-email`.
    - Set the rate ceiling via `edgar.set_rate_limit(rate_limit_per_sec)` — this configures **edgartools' own token-bucket throttle** (`HttpxThrottleCache`/pyrate-limiter in `edgar/httpclient.py`), satisfying AD-3's "enforce at the library throttle, not a per-call wrapper." **Do NOT build a hand-rolled per-request limiter.**
  - [ ] Ensure `Accept-Encoding: gzip, deflate` is actually sent. ⚠️ **Verify what edgartools/httpx sends by default** (read the installed `edgar/httpclient.py` header setup, or capture via `MockTransport` in a test — never over live network). If `gzip, deflate` is not already present, set it on the client's default headers.
  - [ ] Provide the **generic guarded-execution primitive** the ingestion layer (Story 1.4) will call — e.g. `run(self, operation: Callable[[], T], *, description: str) -> T` — which wraps an edgartools call with the Task 3 cool-down policy. **Do NOT implement domain fetch methods** (companyfacts, filings index) here — that is Story 1.4. This story delivers the client + the safe-execution surface only.
  - [ ] Structured logging to stdout (identity configured — log the UA but consider whether to redact the email in logs; rate set; each cool-down with its duration and reason), matching the Story 1.1 logging style.

- [ ] **Task 3 — Throttle-failure cool-down + retry (AC: 2)**
  - [ ] In the guarded executor: run the operation; on a detected **throttle failure**, apply the policy — if a `Retry-After` is available on the failure/response, honor it; otherwise sleep `cooldown_seconds` (≥ 600). Then retry, up to `max_throttle_retries`. If still failing after the last cool-down, raise a typed domain error `EdgarThrottleError` (define it in this module). **Never let a raw library exception crash the run** (AC-2).
  - [ ] Detect the throttle signal via edgartools' `TooManyRequestsError` (HTTP 429). ⚠️ **Verify the exception's import path and attributes in the installed 5.43.0 source** — confirm whether it carries the `response`/headers (for `Retry-After`). The SEC documents no rate-limit status code or `Retry-After` (PRD §9), so the `Retry-After` branch is defensive; the realistic path is the ≥ 10-minute self-imposed cool-down. edgartools' own `stamina` retries cover transient network errors (`ConnectError`/`TimeoutException`/…) — do **not** duplicate those; this wrapper handles the **throttle/ban** signal specifically.
  - [ ] **Testability (mandatory design — do not skip):** inject a `sleep` callable (`EdgarClient(..., *, sleep=time.sleep)`), so tests substitute a recorder and the suite never actually waits 10 minutes. The executor takes a plain `Callable`, so tests pass a fake that raises `TooManyRequestsError` once then returns a value.
  - [ ] **AD-12 forward-hook (do NOT build the lease here):** a run inside an EDGAR cool-down must later keep the single-flight lease heartbeating (Epic 3). Keep the cool-down sleep injectable/interruptible so a future heartbeating sleeper can be passed in. Leave a comment noting this; implement nothing lease-related now.

- [ ] **Task 4 — Tests (fixtures/fakes only; NEVER live EDGAR)** (AC: 1, 2, 3, 4)
  - [ ] New `tests/test_edgar_client.py`. These are **pure unit tests** — no ClickHouse, no network — that pass under the default `uv run pytest` (do **not** mark `@pytest.mark.integration`).
  - [ ] **AC-1 config wiring:** construct `EdgarClient` with a valid `EdgarConfig`; patch `edgar.set_identity` / `edgar.set_rate_limit` and assert they were called with the correctly-formatted identity string (`Name contact-email`) and the configured rate.
  - [ ] **AC-1 on-the-wire headers:** if edgartools permits injecting an httpx client/transport, use httpx `MockTransport` (or `respx`) to capture one request and assert `User-Agent == "<name> <email>"` and `Accept-Encoding` contains `gzip, deflate`. If 5.43.0 does not allow transport injection, fall back to asserting the configuration boundary (the identity string handed to `set_identity`) **plus** a documented one-line manual verification, and record the limitation in Completion Notes. **Either way, no live EDGAR.**
  - [ ] **AC-2 cool-down/retry:** inject a recording `sleep`; pass a fake operation that raises `TooManyRequestsError` once then succeeds → assert exactly one cool-down of `cooldown_seconds` (or the `Retry-After` value when the exception carries one) and that the result is returned. Add: a fake that always raises → after `max_throttle_retries` cool-downs, `EdgarThrottleError` is raised (not a raw library exception). Assert **no real time elapses** (recorder only).
  - [ ] **AC-3 all-access-through-client (structural):** a test that scans `fintin/` (AST or source grep) and asserts **no module outside `fintin/adapters/edgar/`** imports `edgar` or a raw HTTP client (`httpx`, `requests`, `urllib.request`, `http.client`) for EDGAR use. This is the "verified by construction" guard.
  - [ ] **AC-4 fixtures:** create `tests/fixtures/` for any recorded EDGAR payloads used; keep them minimal (no real fetch method exists until 1.4). The rule under test is the mechanism: fixtures/fakes only, never the network.
  - [ ] Extend `tests/test_config.py` — **load-time (structural) validation only:** `rate_limit_per_sec` > 10 / ≤ 0 / `bool` rejected; `cooldown_seconds` < 600 / `bool` rejected; `max_throttle_retries` `bool` rejected; a fully-valid `[edgar]` parses into `EdgarConfig`; **a well-formed block with the placeholder email loads cleanly (`edgar` is not `None`, no error)** — this is the regression guard proving `check-connection`/`conftest` aren't broken; absent `[edgar]` yields `edgar = None`.
  - [ ] Test the **ban-safety gate in `test_edgar_client.py`** (it lives at `EdgarClient` construction, not `load_config`): constructing with `config.edgar is None`, a blank name/email, a malformed email, or a placeholder email each raises the typed domain error before any `edgar.*` call.

- [ ] **Task 5 — Config-hygiene split (public repo — do NOT bleed email into tracked files)** (AC: 1)
  - [ ] Create tracked **`fintin.toml.example`** — the committed template, placeholders only. It carries the full `[clickhouse]` block (with the local dev password `fintin_local`, which is fine: it's a local-container-only value already committed in `docker-compose.yml`) **and** the new `[edgar]` block with a **placeholder** `contact_email`. This is the single source new operators copy from.
  - [ ] **Untrack the real `fintin.toml`:** `git rm --cached fintin.toml` (keeps the working-copy file on disk) and add `fintin.toml` to `.gitignore`. From now on the real config — including the real contact email — is **never committed**. (History still contains the old `fintin_local` password; that's a harmless local value, no scrub needed.)
  - [ ] Keep the local working `fintin.toml` present (add the `[edgar]` block with kboss's **real** email locally) so `check-connection` / `schema-init` / `conftest` keep working here. `tests/conftest.py` needs **no change** — it still reads `fintin.toml`; a fresh clone without it simply skips integration tests (existing "auto-skip when unconfigured" behavior) until the operator copies the example.
- [ ] **Task 6 — Dependency + docs** (AC: 1)
  - [ ] Add `edgartools==5.43.0` to `[project].dependencies` in `pyproject.toml` (this is the story that introduces it — 1.1/1.2 deliberately deferred it). Run `uv sync` to resolve and update the committed `uv.lock`. (edgartools pulls `httpx`, `pyrate-limiter`, `stamina` transitively — no need to pin those directly.)
  - [ ] `README.md`: document the config-setup mechanism clearly — **`cp fintin.toml.example fintin.toml`**, then edit `[edgar].contact_email` to your **real** address before any EDGAR command (ban-critical: EDGAR rejects an undeclared/placeholder UA; the client refuses to start until it's real). Note the `[clickhouse]` block already matches `docker-compose.yml` so it works out of the box, that `fintin.toml` is gitignored (never commit your email), and that the test suite never hits live EDGAR.

### Config hygiene (DECIDED — public repo)
This is a **public repo**, so no contact email (nor real credentials) may land in tracked files. Chosen approach: a tracked **`fintin.toml.example`** (placeholders) + a **gitignored real `fintin.toml`** (Task 5). The `EdgarClient` construction-time gate still rejects the placeholder email, so an operator who copies the example but forgets to set a real address fails loudly before any EDGAR call. The local dev ClickHouse password (`fintin_local`) is not sensitive — it already lives in the committed `docker-compose.yml` — so it stays in the example for out-of-the-box local use.

## Dev Notes

### What this story IS
The **single, compliant, rate-limited EDGAR client** — the one and only doorway to EDGAR (AD-3). It sets the declared identifying User-Agent and `Accept-Encoding: gzip, deflate`, caps the request rate at edgartools' own throttle (≤ 10 req/s), and wraps calls with a Retry-After/≥10-min-cool-down-then-retry policy so a throttle breach never bans us and never crashes the run. It introduces `edgartools` to the project and a validated `[edgar]` config block. It is the substrate Story 1.4 (Tier 0 ingestion) fetches through.

### What this story is NOT (scope fences — do not implement)
- ❌ **No ingestion / no Tier 0 landing / no companyfacts or filings parsing.** Story 1.4 lands raw facts *through* this client. Deliver the client + the generic guarded-execution primitive only — **no domain fetch methods**.
- ❌ **No request-count minimization strategy (index-vs-per-company).** That is **FR-2 → Story 2.2** (re-pointed here during the readiness polish; Story 1.3 cites **FR-1 only**). Don't build discovery/backfill source selection. (Still: don't add gratuitous calls.)
- ❌ **No reconciler / work-list / catch-up / high-water mark.** Epic 3.
- ❌ **No single-flight lease / heartbeat (AD-12).** Epic 3. Keep the cool-down sleep injectable for a future heartbeating sleeper, but build nothing lease-related.
- ❌ **No bulk `companyfacts.zip` strategy (AD-13).** Deferred; per-company is v1 (and lands in 1.4/Epic 3, not here).
- ❌ **No ClickHouse / store changes.** This story doesn't touch the store adapter.

### Builds directly on Stories 1.1 & 1.2 (previous-story intelligence)
- **Config pattern:** `fintin/config.py` uses stdlib `tomllib`, frozen dataclasses, and a domain `ConfigError` rendered cleanly at the CLI boundary (never a traceback). **Follow it exactly** for `EdgarConfig` — same validation idioms (explicit `bool` rejection for numerics; present-key-but-blank handling; `f"[edgar].<key> in {path} …"` messages).
- **`[edgar]` was pre-declared optional:** Story 1.1's `config.py` docstring and `fintin.toml` already say later stories add `[edgar]`. The package dir `fintin/adapters/edgar/` already exists (only `__init__.py`). Add `client.py` there.
- **Adapter conventions:** structured logging to stdout (Story 1.1); always release external resources in a `finally` and suppress close errors (Story 1.1/1.2 review findings). Mirror that discipline for anything the client opens.
- **Test-gating:** `tests/conftest.py` gates only `@pytest.mark.integration` (ClickHouse). Story 1.3's tests are **not** integration — they must run green with no container and no network. Do not add an EDGAR network probe.
- **Runtime:** `uv`-managed; run everything via `uv run …`. Python ≥ 3.12.
- **Review discipline:** 1.1 and 1.2 each went through adversarial code review; 1.2's review caught a HIGH data-integrity bug. Expect the same scrutiny — write the cool-down/retry logic to be *provably* correct under test (injected sleeper + fake operation), not "looks right."

### Architecture decisions this story must obey
| AD | Rule as it applies here |
| --- | --- |
| **AD-3** | Every EDGAR request goes through this one client. Enforce the ceiling at **edgartools' own throttle** (`set_rate_limit`), never a naive per-call wrapper. Set the declared identifying User-Agent (`set_identity`, from config) + `Accept-Encoding: gzip, deflate`. Honor `Retry-After` if present, else self-impose a **≥ 10-minute cool-down** on a throttle failure. **No direct HTTP to EDGAR anywhere else.** Ban-critical. |
| **AD-2** | Throttle lives **inside** the engine/client, never in a trigger. The CLI (Story 1.4+) stays a dumb caller; all ban-avoidance is structural here. |
| **AD-13** | Backfill is strategy-pluggable behind one interface; v1 = per-company. Not built here, but don't design the client in a way that assumes a single strategy — keep the guarded-execution primitive generic. |
| **Errors & status** | Runs fail loudly **except** throttle → cool-down + retry (this story). A per-company ingest failure is later recorded-not-fatal (Story 1.4/Epic 3). Here: exhausted cool-down retries → typed `EdgarThrottleError` the caller can record; never an unhandled crash (AC-2). |
| **Testing** | EDGAR-touching code is tested against **recorded fixtures / injected fakes**; **never** hit live EDGAR in tests or CI (ban risk). |

### edgartools 5.43.0 — API facts (⚠️ verify against the *installed* package, never over live EDGAR)
Confirmed from edgartools docs + source-level analysis (2026-07-23). Web docs disagree on some points; the **installed 5.43.0 source is authoritative** — read it (it's offline, zero ban risk). Key modules: `edgar/__init__.py`, `edgar/httprequests.py`, `edgar/httpclient.py`.

- **Identity / User-Agent:** `edgar.set_identity(...)`. Accepted form is either a positional string `"Name email@x.com"` or kwargs `name=/email=/organization=` — **verify which 5.43.0 accepts**. Requests without an identity raise `IdentityNotSetException` (`edgar/httprequests.py` `@with_identity` decorator). Env fallbacks exist (`EDGAR_IDENTITY`, or `EDGAR_NAME`+`EDGAR_EMAIL`) — **we set it explicitly from config**, not via env, for determinism. Header form required by SEC: `Name EmailAddress`.
- **Rate limit:** `edgar.set_rate_limit(n)` → n req/sec. Default 10, implemented conservatively at ~9 for margin, via a `pyrate-limiter` token bucket in `HttpxThrottleCache` (`edgar/httpclient.py`). This **is** the library's own throttle — configure it (AD-3), don't wrap.
- **Retries (transient):** edgartools uses `stamina` (exponential backoff + jitter) for `RequestError`/`TimeoutException`/`ConnectError`/`RemoteProtocolError`/`BadGzipFile` (`should_retry()` in `edgar/httprequests.py`). `get_with_retry` ≈ 5 attempts / 16s max; `download_file` ≈ 8 / 120s. **We do not duplicate transient retry** — our wrapper handles the *throttle/ban* signal only.
- **Throttle/429:** on HTTP 429, edgartools raises `TooManyRequestsError` and (per source analysis) does **not** auto-retry and does **not** honor `Retry-After` — it warns not to hammer during the block. **This is exactly why our wrapper must catch it and apply the cool-down.** ⚠️ Verify the exception's import path + whether it exposes `response`/headers (for a possible `Retry-After`).
- **Accept-Encoding:** **not documented.** edgartools uses httpx (which sends `Accept-Encoding` by default, typically `gzip, deflate[, br, zstd]`). ⚠️ Verify what's actually sent; ensure `gzip, deflate` is present (AC-1), setting it on the client headers if needed.

### `[edgar]` config block (placeholder in tracked `fintin.toml.example`; real email only in your gitignored `fintin.toml`)
```toml
[edgar]
# EDGAR fair-access compliance (BAN-CRITICAL). EDGAR requires a declared,
# identifying User-Agent with REAL contact info; a blank/placeholder UA is
# rejected as an "Undeclared Automated Tool". Set contact_email to YOUR real
# address before running against EDGAR — the EDGAR client REJECTS the
# placeholder below at startup, so the tool fails loudly rather than send a bad UA.
user_agent_name  = "fin-tin"
contact_email    = "you@example.com"   # ← REPLACE with your real email
rate_limit_per_sec = 10                # SEC max; may be set lower for margin (0 < x <= 10)
cooldown_seconds   = 600               # >= 600 (SEC 10-min cool-down on a throttle breach)
max_throttle_retries = 3               # cool-down+retry attempts before failing loudly
```

### Contact-email / secrets handling (see "Config hygiene (DECIDED — public repo)" above)
Decided: **tracked `fintin.toml.example` (placeholder) + gitignored real `fintin.toml`** — Task 5 does the `git rm --cached fintin.toml` + `.gitignore` split; the real contact email lives only in the local `fintin.toml`. Belt-and-suspenders: even if a copied-but-unedited config reaches a run, `EdgarClient` construction rejects the placeholder email before any EDGAR call. Untracking `fintin.toml` is a deliberate change to the Story 1.1 artifact; `conftest.py` is unchanged (still reads `fintin.toml`; a fresh clone skips integration tests until the operator copies the example).

### Testability design (mandatory — the cool-down must be unit-testable offline)
- `EdgarClient(config: EdgarConfig, *, sleep: Callable[[float], None] = time.sleep)` — inject `sleep` so tests use a recorder and the suite never waits.
- Guarded executor takes a plain `Callable[[], T]`; tests pass fakes raising `TooManyRequestsError` to drive the cool-down/retry branches deterministically.
- For header verification prefer httpx `MockTransport`/`respx` if edgartools allows client/transport injection; otherwise assert at the `set_identity` boundary + a documented manual check. **The invariant is: the default test suite makes zero network calls to EDGAR.**

### Files to touch
```text
fintin/adapters/edgar/client.py   # NEW — the single EDGAR client (identity, rate, cool-down); SOLE edgar importer
fintin/config.py                  # UPDATE — add [edgar] → EdgarConfig (validated); Config.edgar: EdgarConfig | None
fintin.toml.example               # NEW (tracked) — template: [clickhouse] (fintin_local) + [edgar] placeholder email
fintin.toml                       # UNTRACK (git rm --cached) + gitignore — real [edgar] email lives here LOCALLY ONLY
.gitignore                        # UPDATE — add `fintin.toml`
pyproject.toml                    # UPDATE — add edgartools==5.43.0 (uv.lock updated via `uv sync`)
tests/test_edgar_client.py        # NEW — unit tests: config wiring, UA/Accept-Encoding, cool-down/retry, all-through-client
tests/test_config.py              # UPDATE — [edgar] validation cases
tests/fixtures/                   # NEW (if any recorded payloads are used) — fixtures only, never live
README.md                         # UPDATE — [edgar] setup + ban-safety note; tests never hit live EDGAR
```

### Testing standards
- `pytest` via `uv run pytest`. Story 1.3 tests are **unit** (no `@pytest.mark.integration`) — green with no Docker and **no network**.
- **Absolute rule:** never hit live EDGAR (ban risk). Verify edgartools internals by reading the **installed** package source; simulate throttle/headers with fakes / `MockTransport` / fixtures.
- Cool-down/retry timing is asserted via an injected recording `sleep` — assert durations and retry counts, never elapse real time.
- Keep the default suite fast; edgartools import must not trigger any network on import.

### Project Structure Notes
- `client.py` lives in `fintin/adapters/edgar/` beside the existing `__init__.py` — matches the architecture source tree and AD-3 ownership. No variances.
- The client exposes a **generic guarded-execution primitive**, not domain fetch methods (those are Story 1.4). This keeps the AD-3 boundary crisp: one place configures identity/rate and applies the cool-down; callers just hand it operations.

### References
- [Source: _bmad-output/planning-artifacts/epics.md#Story-1.3] — user story + ACs (cites AD-3, FR-1)
- [Source: .../architecture/.../ARCHITECTURE-SPINE.md#AD-3] — one rate-limited client; identity, Accept-Encoding, Retry-After/cool-down, throttle-at-library
- [Source: .../ARCHITECTURE-SPINE.md#AD-2] [#AD-13] [#Consistency-Conventions (Errors & status, Testing, Logging & config)] [#Stack (edgartools 5.43.0)]
- [Source: .../architecture/.../BUILD-SPLIT.md#Epic-B] — deliverables + "enforce at the library throttle, not a call-count wrapper (ban-critical)" watch
- [Source: .../prds/.../prd.md#FR-1] — centralized rate-limited client (testable consequences); [#9 Constraints & Guardrails] — EDGAR fair-access ban-critical (10 req/s, mandatory declared UA, 10-min cool-down, no documented Retry-After)
- [Source: _bmad-output/implementation-artifacts/1-1-runnable-skeleton.md] — config loader, CLI-as-dumb-trigger, logging, test-gating patterns
- [Source: _bmad-output/implementation-artifacts/1-2-store-schema-ddl.md] — adapter conventions, review rigor, resource-cleanup discipline
- edgartools docs: <https://edgartools.readthedocs.io/en/stable/resources/sec-compliance/>, <https://edgartools.readthedocs.io/en/stable/configuration/>, <https://deepwiki.com/dgunning/edgartools/8.1-http-client-and-rate-limiting>

## Dev Agent Record

### Agent Model Used

### Debug Log References

### Completion Notes List

### File List

### Change Log
