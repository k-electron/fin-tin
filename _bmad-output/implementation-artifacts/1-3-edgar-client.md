---
baseline_commit: c2e9fc53dad599f7d8d3729cc575ac4ea76d851c
---

# Story 1.3: Compliant rate-limited EDGAR client

Status: done

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

- [x] **Task 1 — `[edgar]` config block → validated `EdgarConfig`** (AC: 1)
  - [x] Extend `fintin/config.py`: add a frozen `EdgarConfig` dataclass — `user_agent_name: str`, `contact_email: str`, `rate_limit_per_sec: float` (default `10.0`), `cooldown_seconds: int` (default `600`), `max_throttle_retries: int` (default `3`).
  - [x] Parse an optional `[edgar]` section in `load_config`; add `edgar: EdgarConfig | None` to `Config`. **Absent `[edgar]` → `edgar = None`** (so `check-connection` / `schema-init` still work without it). **Present-but-structurally-invalid `[edgar]` → `ConfigError`** (fail loudly), consistent with the existing `[clickhouse]` validation style.
  - [x] **Load-time validation = STRUCTURE + TYPES + RANGES ONLY** (see the regression warning below — do NOT reject a placeholder email here):
    - `user_agent_name`: a `str` (key present).
    - `contact_email`: a `str` (key present).
    - `rate_limit_per_sec`: a number with `0 < x <= 10` (10 = SEC published max; > 10 is a ban risk — reject; AD-3). Reject `bool` explicitly (as the existing `port` guard does).
    - `cooldown_seconds`: an `int` `>= 600` (the SEC-documented 10-minute cool-down floor; smaller is non-compliant — reject). Reject `bool`.
    - `max_throttle_retries`: an `int` `>= 0`. Reject `bool`.
  - [x] 🚨 **REGRESSION WARNING — do not put the blank/placeholder-email rejection in `load_config`.** `fintin.toml` is loaded by `check-connection`, `schema-init`, **and `tests/conftest.py`** (`_local_config()` catches `ConfigError` and returns `None`). If `load_config` raised on the shipped placeholder email, **every** command would break and **all ClickHouse integration tests would silently skip** (masking real misconfig — the exact failure Story 1.1's review fixed). Therefore: a well-formed `[edgar]` block (valid types/ranges, placeholder email) **must load cleanly**. The ban-safety semantic gate (blank / bad-format / placeholder email) lives in **`EdgarClient` construction** — Task 2 — the moment before any EDGAR call.
  - [x] The `[edgar]` block goes in the **tracked `fintin.toml.example`** (placeholder email) and the **gitignored real `fintin.toml`** — see Task 5's config-hygiene split (public repo: no email in tracked files). Update the header comment (Story 1.1 said `[edgar]` is "tolerated but not required") to reflect that `[edgar]` is now defined and required *for EDGAR access*.

- [x] **Task 2 — The single EDGAR client (sole `edgar` owner, AD-3)** (AC: 1, 3)
  - [x] New `fintin/adapters/edgar/client.py` — the **only** module in `fintin/` that imports `edgar` / issues EDGAR requests. Expose an `EdgarClient` (a small class is preferred over module globals for testability — see Task 4's injection points).
  - [x] **Ban-safety gate (this is where FR-1 "never send a blank/undeclared UA" is enforced structurally).** On construction, before configuring identity, validate `config.edgar`: raise a typed domain error (`EdgarConfigError` — define it here, or reuse `ConfigError`) if `config.edgar is None` (no `[edgar]` block), if `user_agent_name` is blank, or if `contact_email` is blank / malformed (must contain `@` with a non-empty local part and a dotted domain) / a **known placeholder** (reject `you@example.com`, `your.email@example.com`, `changeme@example.com`, `example@example.com`). This gate — not `load_config` — is what forces a real address before any EDGAR call (see Task 1's regression warning).
  - [x] On construction (after the gate passes), configure edgartools' **global** state from `EdgarConfig`, deterministically (do not rely on ambient `EDGAR_IDENTITY` / `EDGAR_NAME` env vars):
    - Set the declared identity via `edgar.set_identity(...)`. ⚠️ **Verify the exact 5.43.0 signature by reading the installed package** (`edgar/__init__.py`, `edgar/httprequests.py`) — it is either a single positional string `"Name contact-email"` or kwargs `name=/email=/organization=`. Build the identity from `user_agent_name` + `contact_email` in the FR-1 form `Name contact-email`.
    - Set the rate ceiling via `edgar.set_rate_limit(rate_limit_per_sec)` — this configures **edgartools' own token-bucket throttle** (`HttpxThrottleCache`/pyrate-limiter in `edgar/httpclient.py`), satisfying AD-3's "enforce at the library throttle, not a per-call wrapper." **Do NOT build a hand-rolled per-request limiter.**
  - [x] Ensure `Accept-Encoding: gzip, deflate` is actually sent. ⚠️ **Verify what edgartools/httpx sends by default** (read the installed `edgar/httpclient.py` header setup, or capture via `MockTransport` in a test — never over live network). If `gzip, deflate` is not already present, set it on the client's default headers.
  - [x] Provide the **generic guarded-execution primitive** the ingestion layer (Story 1.4) will call — e.g. `run(self, operation: Callable[[], T], *, description: str) -> T` — which wraps an edgartools call with the Task 3 cool-down policy. **Do NOT implement domain fetch methods** (companyfacts, filings index) here — that is Story 1.4. This story delivers the client + the safe-execution surface only.
  - [x] Structured logging to stdout (identity configured — log the UA but consider whether to redact the email in logs; rate set; each cool-down with its duration and reason), matching the Story 1.1 logging style.

- [x] **Task 3 — Throttle-failure cool-down + retry (AC: 2)**
  - [x] In the guarded executor: run the operation; on a detected **throttle failure**, apply the policy — if a `Retry-After` is available on the failure/response, honor it; otherwise sleep `cooldown_seconds` (≥ 600). Then retry, up to `max_throttle_retries`. If still failing after the last cool-down, raise a typed domain error `EdgarThrottleError` (define it in this module). **Never let a raw library exception crash the run** (AC-2).
  - [x] Detect the throttle signal via edgartools' `TooManyRequestsError` (HTTP 429). ⚠️ **Verify the exception's import path and attributes in the installed 5.43.0 source** — confirm whether it carries the `response`/headers (for `Retry-After`). The SEC documents no rate-limit status code or `Retry-After` (PRD §9), so the `Retry-After` branch is defensive; the realistic path is the ≥ 10-minute self-imposed cool-down. edgartools' own `stamina` retries cover transient network errors (`ConnectError`/`TimeoutException`/…) — do **not** duplicate those; this wrapper handles the **throttle/ban** signal specifically.
  - [x] **Testability (mandatory design — do not skip):** inject a `sleep` callable (`EdgarClient(..., *, sleep=time.sleep)`), so tests substitute a recorder and the suite never actually waits 10 minutes. The executor takes a plain `Callable`, so tests pass a fake that raises `TooManyRequestsError` once then returns a value.
  - [x] **AD-12 forward-hook (do NOT build the lease here):** a run inside an EDGAR cool-down must later keep the single-flight lease heartbeating (Epic 3). Keep the cool-down sleep injectable/interruptible so a future heartbeating sleeper can be passed in. Leave a comment noting this; implement nothing lease-related now.

- [x] **Task 4 — Tests (fixtures/fakes only; NEVER live EDGAR)** (AC: 1, 2, 3, 4)
  - [x] New `tests/test_edgar_client.py`. These are **pure unit tests** — no ClickHouse, no network — that pass under the default `uv run pytest` (do **not** mark `@pytest.mark.integration`).
  - [x] **AC-1 config wiring:** construct `EdgarClient` with a valid `EdgarConfig`; patch `edgar.set_identity` / `edgar.set_rate_limit` and assert they were called with the correctly-formatted identity string (`Name contact-email`) and the configured rate.
  - [x] **AC-1 on-the-wire headers:** if edgartools permits injecting an httpx client/transport, use httpx `MockTransport` (or `respx`) to capture one request and assert `User-Agent == "<name> <email>"` and `Accept-Encoding` contains `gzip, deflate`. If 5.43.0 does not allow transport injection, fall back to asserting the configuration boundary (the identity string handed to `set_identity`) **plus** a documented one-line manual verification, and record the limitation in Completion Notes. **Either way, no live EDGAR.**
  - [x] **AC-2 cool-down/retry:** inject a recording `sleep`; pass a fake operation that raises `TooManyRequestsError` once then succeeds → assert exactly one cool-down of `cooldown_seconds` (or the `Retry-After` value when the exception carries one) and that the result is returned. Add: a fake that always raises → after `max_throttle_retries` cool-downs, `EdgarThrottleError` is raised (not a raw library exception). Assert **no real time elapses** (recorder only).
  - [x] **AC-3 all-access-through-client (structural):** a test that scans `fintin/` (AST or source grep) and asserts **no module outside `fintin/adapters/edgar/`** imports `edgar` or a raw HTTP client (`httpx`, `requests`, `urllib.request`, `http.client`) for EDGAR use. This is the "verified by construction" guard.
  - [x] **AC-4 fixtures:** create `tests/fixtures/` for any recorded EDGAR payloads used; keep them minimal (no real fetch method exists until 1.4). The rule under test is the mechanism: fixtures/fakes only, never the network.
  - [x] Extend `tests/test_config.py` — **load-time (structural) validation only:** `rate_limit_per_sec` > 10 / ≤ 0 / `bool` rejected; `cooldown_seconds` < 600 / `bool` rejected; `max_throttle_retries` `bool` rejected; a fully-valid `[edgar]` parses into `EdgarConfig`; **a well-formed block with the placeholder email loads cleanly (`edgar` is not `None`, no error)** — this is the regression guard proving `check-connection`/`conftest` aren't broken; absent `[edgar]` yields `edgar = None`.
  - [x] Test the **ban-safety gate in `test_edgar_client.py`** (it lives at `EdgarClient` construction, not `load_config`): constructing with `config.edgar is None`, a blank name/email, a malformed email, or a placeholder email each raises the typed domain error before any `edgar.*` call.

- [x] **Task 5 — Config-hygiene split (public repo — do NOT bleed email into tracked files)** (AC: 1)
  - [x] Create tracked **`fintin.toml.example`** — the committed template, placeholders only. It carries the full `[clickhouse]` block (with the local dev password `fintin_local`, which is fine: it's a local-container-only value already committed in `docker-compose.yml`) **and** the new `[edgar]` block with a **placeholder** `contact_email`. This is the single source new operators copy from.
  - [x] **Untrack the real `fintin.toml`:** `git rm --cached fintin.toml` (keeps the working-copy file on disk) and add `fintin.toml` to `.gitignore`. From now on the real config — including the real contact email — is **never committed**. (History still contains the old `fintin_local` password; that's a harmless local value, no scrub needed.)
  - [x] Keep the local working `fintin.toml` present so `check-connection` / `schema-init` / `conftest` keep working here. **The `[edgar]` block is intentionally NOT added to the local `fintin.toml`** — `[edgar]` is optional (absent → `edgar=None`), so non-EDGAR commands and `conftest` work as-is, and no email is invented on kboss's behalf; kboss adds `[edgar]` with a real email locally when the first EDGAR-touching command lands (Story 1.4). `tests/conftest.py` needs **no change** — a fresh clone without `fintin.toml` simply skips integration tests until the operator copies the example.
- [x] **Task 6 — Dependency + docs** (AC: 1)
  - [x] Add `edgartools==5.43.0` to `[project].dependencies` in `pyproject.toml` (this is the story that introduces it — 1.1/1.2 deliberately deferred it). Run `uv sync` to resolve and update the committed `uv.lock`. (edgartools pulls `httpx`, `pyrate-limiter`, `stamina` transitively — no need to pin those directly.)
  - [x] `README.md`: document the config-setup mechanism clearly — **`cp fintin.toml.example fintin.toml`**, then edit `[edgar].contact_email` to your **real** address before any EDGAR command (ban-critical: EDGAR rejects an undeclared/placeholder UA; the client refuses to start until it's real). Note the `[clickhouse]` block already matches `docker-compose.yml` so it works out of the box, that `fintin.toml` is gitignored (never commit your email), and that the test suite never hits live EDGAR.

### Config hygiene (DECIDED — public repo)
This is a **public repo**, so no contact email (nor real credentials) may land in tracked files. Chosen approach: a tracked **`fintin.toml.example`** (placeholders) + a **gitignored real `fintin.toml`** (Task 5). The `EdgarClient` construction-time gate still rejects the placeholder email, so an operator who copies the example but forgets to set a real address fails loudly before any EDGAR call. The local dev ClickHouse password (`fintin_local`) is not sensitive — it already lives in the committed `docker-compose.yml` — so it stays in the example for out-of-the-box local use.

### Review Findings

_Adversarial code review (2026-07-23) — 3 parallel layers (Blind Hunter, Edge Case Hunter, Acceptance Auditor), every library claim verified against the installed edgartools 5.43.0 source; no network touched (suite offline). Triage: 1 decision-needed, 12 patch, 3 deferred, 0 dismissed. All 4 ACs met; one ban-critical refinement (F1) in the cool-down layer, on which all three layers converged._

- [x] [Review][Decision] (high) **RESOLVED — kboss chose the floor+cap (option 1); applied.** **Retry-After honored below the ≥10-min cool-down floor — ban-critical.** `run()` does `wait = exc.retry_after if exc.retry_after else cooldown_seconds`, so a 429 carrying `Retry-After: 60` sleeps only 60s and retries *inside* the SEC's ~10-min IP block — which the SEC penalizes by extending the block (edgartools' own `TooManyRequestsError` docstring warns of exactly this). Satisfies AC2's literal wording but violates its ban-safety intent (NFR-7, SM-C1). Recommended: floor+cap the wait — `wait = min(max(exc.retry_after or 0, cooldown_seconds), MAX_COOLDOWN)` (honor a *longer* Retry-After, never a shorter one) + update the test that asserts `[42]`. [fintin/adapters/edgar/client.py:151, tests/test_edgar_client.py]
- [x] [Review][Patch] (medium) Validate ALL `EdgarConfig` fields at construction *before* any edgar global mutation — re-assert `cooldown_seconds>=600`, `max_throttle_retries>=0`, integer `rate>=1` (the public dataclass has no `__post_init__`, so a directly-built config bypasses `load_config`; `max_throttle_retries=-1` currently reaches `raise AssertionError("unreachable")` and never runs the op). [fintin/adapters/edgar/client.py:79-100,141-142]
- [x] [Review][Patch] (medium) Ban-safety email gate blocks only 4 literal placeholders — reject RFC-2606 reserved domains (`example.com/.org/.net`, `.invalid/.test/.localhost`) so `admin@example.com` etc. can't pass. [fintin/adapters/edgar/client.py:51,126]
- [x] [Review][Patch] (medium) Load-time accepts `0<rate<1` but the client rejects `int(rate)<1` — a config `load_config` blesses then crashes at first EDGAR use; align the loader to `1<=rate<=10` and fix the advertised range. [fintin/config.py:159, fintin/adapters/edgar/client.py:89]
- [x] [Review][Patch] (medium) Default `rate_limit_per_sec=10` is the SEC hard ceiling (zero margin); edgartools itself defaults to 9. Change the default to 9 (SM-C1: prefer slower/safer). [fintin/config.py:49,153; fintin.toml.example]
- [x] [Review][Patch] (low) Cap the cool-down wait so a hostile/garbage `Retry-After` (huge int / far-future date) can't wedge the run indefinitely (folds into F1's `min(...)`). [fintin/adapters/edgar/client.py:151,160]
- [x] [Review][Patch] (low) Strengthen the AST import guard — catch `from urllib import request` / `from http import client` and add `urllib`/`http`/`aiohttp`/`urllib3`/`socket` roots. [tests/test_edgar_client.py]
- [x] [Review][Patch] (low) Mask/omit the contact email in logs (public-repo email privacy). [fintin/adapters/edgar/client.py:108]
- [x] [Review][Patch] (low) Reject control chars/newlines in `user_agent_name` at the gate (else header injection → late httpx crash mid-run). [fintin/adapters/edgar/client.py:119]
- [x] [Review][Patch] (low) Add the AD-12 forward-hook comment the Task 3 subtask claims (heartbeating sleeper for the future lease). [fintin/adapters/edgar/client.py:160]
- [x] [Review][Patch] (low) Autouse test fixture snapshotting/restoring `EDGAR_IDENTITY`, `EDGAR_RATE_LIMIT_PER_SEC`, `httpclient.HTTP_MGR` around client-constructing tests (avoid latent cross-test global pollution). [tests/test_edgar_client.py]
- [x] [Review][Patch] (low) Fix docs: README "honors Retry-After" wording → "waits at least the cool-down, honoring a longer Retry-After"; fix the rate range; drop the inaccurate "integer-seconds or HTTP-date" note (edgar normalizes to int). [README.md, fintin/adapters/edgar/client.py:20]
- [x] [Review][Patch] (low) Story Task 5 subtask "add `[edgar]` with real email to the local `fintin.toml`" is checked but intentionally NOT done (email is kboss's to set; `[edgar]` is optional so `conftest`/`check-connection` work with `edgar=None`) — annotate for accuracy. [this story file]
- [x] [Review][Defer] (medium) No singleton enforcement — every `EdgarClient(...)` stomps global identity/rate and a concurrent construction could close another's transport. [fintin/adapters/edgar/client.py:85,96-100] — deferred, v1 is single-process + single-flight (AD-12, Epic 3).
- [x] [Review][Defer] (medium) `run()` is sync-only — can't wrap edgartools' async/bulk-download 429s, so a future bulk `companyfacts.zip` path would throttle outside the cool-down. [fintin/adapters/edgar/client.py:133] — deferred, bulk strategy (AD-13) + async are not in v1.
- [x] [Review][Defer] (low) The cool-down is a single uninterruptible blocking `sleep` with no progress signal. [fintin/adapters/edgar/client.py:160] — deferred, ties to the AD-12 heartbeat (Epic 3); the sleep is already injectable for that.

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

claude-opus-4-8[1m] (Claude Opus 4.8, 1M context) — bmad-dev-story workflow.

### Debug Log References

- **API verified against the *installed* edgartools 5.43.0 source (offline)** — the spec's several "verify against installed package" flags resolved to:
  - `edgar.set_identity(user_identity: str)` — a **single string** `"Name email"` (not kwargs). Implemented accordingly.
  - **No `set_rate_limit()` exists.** The rate lives in the import-time singleton `edgar.httpclient.HTTP_MGR`, seeded from env `EDGAR_RATE_LIMIT_PER_SEC` (default `"9"`, read as `int`). To honor a configured (often lower) rate on the live client I set the env var **and** rebuild the manager: `httpclient.HTTP_MGR = httpclient.get_http_mgr(request_per_sec_limit=rate)`. Confirmed `httprequests.py` calls the module-level `http_client()` per request, which reads the current `HTTP_MGR` global — so the reassignment propagates. This is the AD-3 "enforce at the library throttle" path (no hand-rolled limiter).
  - `edgar.httprequests.TooManyRequestsError(url, retry_after=None)` **carries `.retry_after`** (edgar extracts `Retry-After`, integer-seconds or HTTP-date, via `_get_retry_after`; `BLOCK_DURATION_MINUTES = 10`). Our cool-down reads `exc.retry_after`.
  - **Accept-Encoding:** httpx's default is exactly `gzip, deflate` here (no brotli/zstd installed); edgar sends it unchanged — no override needed. Verified via `MockTransport`.
- **Testing finding (recorded so it's never repeated):** passing `transport=` to `edgar.httpclient.http_client(...)` is **ignored and issues a real network request** — so tests must NOT use it. The offline header test instead builds an `httpx.Client` from `edgar.httpclient.get_http_params()` (edgar's real params incl. the populated UA) over an httpx `MockTransport`. (One incidental live probe request was made while discovering this during exploration — not part of the test suite; the suite makes zero EDGAR network calls.)
- **Sequencing note:** `edgartools==5.43.0` (Task 6's dep) was installed first because Task 2 imports `edgar` and the API had to be verified against the installed package. It is a declared story dependency (no unapproved deps added).
- Full suite: **52 passed** (42 unit + 10 ClickHouse integration, live CH `26.3.17.56`). EDGAR tests are pure unit (no `@pytest.mark.integration`), offline. CLI (`--help`, `check-connection`) unaffected — the CLI never imports `edgar`.

### Completion Notes List

- **All 4 ACs satisfied and tested offline:**
  - **AC-1** — `EdgarClient` sets the declared UA `"{user_agent_name} {contact_email}"` via `set_identity` (asserted through `edgar.get_identity()`), sends `Accept-Encoding: gzip, deflate` (asserted via `get_http_params()` + `MockTransport`), and caps the rate at edgartools' own throttle ≤ 10 req/s (asserted via `get_edgar_rate_limit_per_sec()` and `HTTP_MGR.request_per_sec_limit`).
  - **AC-2** — `run(operation)` catches `TooManyRequestsError`, honors `Retry-After` when present else waits `cooldown_seconds` (≥ 600), retries up to `max_throttle_retries`, and raises the typed `EdgarThrottleError` on exhaustion (never a raw library crash). Cool-down timing asserted via an injected recording sleeper — no real waits.
  - **AC-3** — an AST scan test asserts no module outside `fintin/adapters/edgar/` imports `edgar`/`httpx`/`requests`/raw HTTP.
  - **AC-4** — the whole EDGAR test set is offline (fakes + `MockTransport`); README documents "never hits live EDGAR".
- **Ban-safety is structural (FR-1):** the loader validates `[edgar]` only structurally (types/ranges), so a well-formed block with the placeholder email loads cleanly (non-EDGAR commands + `conftest` keep working); the **semantic gate** (no `[edgar]` block / blank name / blank-malformed-placeholder email / sub-1 rate) lives in `EdgarClient.__init__` and fails loudly *before* any EDGAR call.
- **Config hygiene (public repo):** added tracked `fintin.toml.example` (placeholders + local `fintin_local` CH password) and untracked the real `fintin.toml` via `git rm --cached` + `.gitignore`. The real EDGAR contact email is never committed; `conftest.py` unchanged (reads the local `fintin.toml`).
- `tests/fixtures/` was **not** created — no recorded EDGAR payloads are needed yet (the tests use fakes/`MockTransport`); it arrives with Story 1.4's real fetch methods.
- **Deviation from spec, no scope change:** spec said "use `edgar.set_rate_limit`"; that function doesn't exist in 5.43.0, so the rate is applied via `EDGAR_RATE_LIMIT_PER_SEC` + `HTTP_MGR` rebuild — same AD-3 outcome (throttle at the library). Rate is applied as `int(rate_limit_per_sec)` (edgartools' throttle is integer req/s); a configured value that floors below 1 is rejected at construction.
- New dependency: `edgartools==5.43.0` (pulls `httpx`, `pyrate-limiter`, `stamina`, `httpxthrottlecache`, pandas, etc. transitively); `uv.lock` updated.

### File List

**New:**
- `fintin/adapters/edgar/client.py` — the single EDGAR client (`EdgarClient`, `EdgarConfigError`, `EdgarThrottleError`); sole `edgar` importer
- `tests/test_edgar_client.py` — offline unit tests (identity/rate/headers, cool-down/retry, gate, structural all-through-client)
- `fintin.toml.example` — tracked config template (`[clickhouse]` + `[edgar]` placeholder)

**Modified:**
- `fintin/config.py` — added `EdgarConfig` + `[edgar]` parsing/validation; `Config.edgar: EdgarConfig | None`
- `tests/test_config.py` — `[edgar]` structural-validation cases + placeholder-loads-cleanly regression guard
- `pyproject.toml` — added `edgartools==5.43.0`
- `uv.lock` — resolved edgartools + transitive deps
- `README.md` — config-setup mechanism (`cp fintin.toml.example fintin.toml`), `[edgar]`/ban-safety, no-live-EDGAR testing note
- `.gitignore` — ignore local `fintin.toml`

**Removed from tracking (kept on disk):**
- `fintin.toml` — `git rm --cached` (real EDGAR email lives here locally only)

### Change Log

- 2026-07-23 — Story 1.3 implemented: the single compliant rate-limited EDGAR client (`fintin/adapters/edgar/client.py`) — declared User-Agent via `set_identity`, rate capped at edgartools' own throttle (`EDGAR_RATE_LIMIT_PER_SEC` + `HTTP_MGR` rebuild), `Accept-Encoding: gzip, deflate` (httpx default), and a `run()` executor applying Retry-After/≥10-min cool-down + bounded retry (→ typed `EdgarThrottleError`). Added the validated `[edgar]` config block, the ban-safety construction gate, `edgartools==5.43.0`, and the public-repo config-hygiene split (`fintin.toml.example` + gitignored `fintin.toml`). 27 new tests; **52 passed** total. Status → review.
- 2026-07-23 — Code review (adversarial, 3 layers, all verified against the installed edgartools 5.43.0 source; no network): 1 decision + 12 patches applied, 3 deferred, 0 dismissed. **F1 (ban-critical):** the cool-down now waits **at least** the ≥10-min floor and honors only a *longer* `Retry-After` (capped at 24h) — a shorter `Retry-After` can no longer undercut the SEC block. Also: validate all `[edgar]` fields **before** mutating any edgar global (re-assert `cooldown_seconds>=600`, `max_throttle_retries>=0`, `rate∈[1,10]` — closes the directly-built-config bypass and the reachable `AssertionError`); reject RFC-2606 reserved placeholder domains; align the loader's rate range to `[1,10]`; default rate 10→9 (safety margin); mask the email in logs; reject control chars in `user_agent_name`; strengthen the AST import guard; autouse fixture restoring edgar globals between tests; doc fixes; add the AD-12 forward-hook comment. Suite: **63 passed**. Status → done.
