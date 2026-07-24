---
baseline_commit: 2cd12d56485d85f22f59366de1c888ffd4f41aa0
---
# Story 2.1: Resolve the Universe from config

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the builder,
I want the S&P 500 Universe defined in `fintin.toml` and resolved to a deduplicated set of CIKs (tickers mapped via edgartools' bundled reference table, offline),
so that ingestion knows its scope and the Universe can be widened later by editing config alone.

## Acceptance Criteria

1. **Given** `fintin.toml` lists tickers and/or CIKs under `[universe]` **When** the Universe is resolved **Then** tickers resolve to CIKs via edgartools **And** the resolved Universe (a deduplicated set of integer CIKs) is available to the pipeline as a single derived value — with **zero network requests** for tickers present in edgartools' bundled reference table (FR-7, AD-13).
2. **Given** a ticker edgartools cannot resolve **Then** it is surfaced as an **explained gap** (identifier + reason), never silently dropped; the remaining Universe still resolves (SM-2, FR-14 philosophy). **Given** a structurally invalid `[universe]` (missing section, both lists empty, non-string ticker, non-integer / out-of-range CIK) **Then** it fails with a clear `ConfigError` (exit 2), not a stack trace.
3. **Given** I add CIKs (or tickers) to `[universe]` in config **Then** the Universe grows with **no code or schema change** (NFR-2) — the Universe is derived from config at load, never persisted (AD-1).

## 🔑 Key design decision — offline resolution via edgartools' BUNDLED reference table (settled)

edgartools 5.43.0 resolves tickers→CIK from a **bundled parquet** (`edgar/reference/data/company_tickers.parquet`, ships in the package). Verified against the installed source:

- **`edgar.reference.tickers.get_company_cik_lookup()`** (and `edgar.get_ticker_to_cik_lookup()`) return the full `{TICKER: cik}` dict from that bundled parquet — **no network** for any bundled ticker. `@lru_cache`d, so the parquet is parsed once per process; resolving 500 tickers = one bundled load + dict lookups. This is the batch resolver we use.
- **Do NOT use `find_cik()` / `Company("AAPL")` for resolution.** `find_cik` has a *per-ticker live SEC fallback* (`_get_live_company_cik_lookup` → `company_tickers.json` on sec.gov) for tickers absent from the bundle, plus a mutual-fund network fallback. The plain dict getters have **no** such fallback — a ticker absent from the bundle is simply not a key. Using the dict getter keeps resolution **guaranteed-offline and deterministic**, and turns an unresolvable ticker into a clean recorded gap (AC-2) instead of a silent network hit.
- **AD-3 is not triggered.** Reading edgartools' bundled package data issues **no EDGAR request**, so this path needs **no `EdgarClient`, no rate limiter, and no contact email**. (For completeness: even edgartools' fallback fetch, which we deliberately avoid, routes through the rate-limited `HTTP_MGR` — nothing bypasses the limiter.) `fintin universe` is therefore a fully offline command, unlike `ingest-company`.
- **Ticker normalization:** the lookup keys are upper-case with `.`→`-` (e.g. `BRK-B`). Normalize each configured ticker (`strip().upper()`, `.`→`-`) before the dict lookup so `brk.b`, `BRK.B`, `BRK-B` all resolve.

**Rejected alternative:** routing resolution through `EdgarClient`/`find_cik` to get a live-refreshed mapping. Rejected because it would (a) require a real contact email for a lookup that is normally offline, (b) reintroduce a network path (ban surface + NFR-7 test hazard), and (c) make an unresolvable ticker a network round-trip rather than a deterministic gap. A stale bundle surfacing a very-recent S&P addition as a gap is acceptable — the operator resolves it by adding that CIK directly (AC-3).

## Tasks / Subtasks

- [x] **Task 1 — Parse `[universe]` in config (offline, structure/types/ranges only)** (AC: 2, 3) — `fintin/config.py`
  - [x] Add a frozen `UniverseConfig(tickers: tuple[str, ...], ciks: tuple[int, ...])` dataclass and `universe: UniverseConfig | None = None` on `Config` (mirror the optional `[edgar]` pattern — absent section → `None`, so non-universe commands are unaffected).
  - [x] `_parse_universe(u, path)`: `tickers` must be a list of non-empty strings; `ciks` a list of ints in `[1, 4_294_967_295]` (UInt32, same range guard as the CLI CIK args). Reject `bool` explicitly (it subclasses `int`) — mirror the `_parse_clickhouse`/`_parse_edgar` type guards. Both keys optional individually, but **at least one non-empty** — an empty Universe is a `ConfigError` (nothing to ingest). Trim/keep tickers verbatim here (normalization happens at resolve).
  - [x] Keep validation **offline and semantic-free** here (structure/types/ranges), consistent with the config layer's contract — ticker *resolvability* is decided at resolve time, not load time (an unknown-but-well-formed ticker loads fine, then becomes a gap).
- [x] **Task 2 — Pure Universe resolution in core** (AC: 1, 2, 3) — `fintin/core/universe.py` (NEW, pure; no `edgar`, no ClickHouse)
  - [x] `UniverseGap(NamedTuple)`: `identifier: str`, `reason: str`. `ResolvedUniverse(NamedTuple)`: `ciks: tuple[int, ...]` (sorted, deduplicated), `gaps: tuple[UniverseGap, ...]`, `tickers_resolved: int`, `explicit_ciks: int`.
  - [x] `resolve_universe(universe: UniverseConfig, *, resolve_tickers: Callable[[Sequence[str]], dict[str, int | None]]) -> ResolvedUniverse`: start from the explicit `ciks`; call the injected **batch** `resolve_tickers(tickers)` once; fold resolved CIKs into the set; a `None`/missing mapping → append a `UniverseGap(ticker, "not found in edgartools reference data")`. Deduplicate (a ticker resolving to an already-listed CIK is a union, not a duplicate). Return CIKs **sorted** for deterministic output. Do not call the resolver when there are no tickers (pure-CIK universes need no resolver).
  - [x] `resolve_tickers` is an injected **port** (same inversion as `ingest_company`'s `fetch_facts`/`insert_rows`) so core stays edgar-free and unit-testable with a fake resolver.
  - [x] Universe is **derived, never stored** (AD-1) — `resolve_universe` returns a value; nothing persists it. (Backfill in Story 2.3 calls this same function to get its scope.)
- [x] **Task 3 — edgartools bundled-table resolver adapter** (AC: 1, 2) — `fintin/adapters/edgar/universe.py` (NEW)
  - [x] `resolve_tickers(tickers: Sequence[str]) -> dict[str, int | None]` using `edgar.reference.tickers.get_company_cik_lookup()` (bundled, offline). Load the lookup **once**, normalize each input ticker (`strip().upper()`, `.`→`-`), map to `int(cik)` or `None`. Return keyed by the **original** configured ticker string (so the CLI can report the exact config value in a gap).
  - [x] This is the ONLY new module importing `edgar` (all `edgar` imports live in `adapters/edgar/`). Add a module docstring stating it is offline (bundled reference data, no EDGAR request, AD-3 not triggered) and WHY the dict getter is used instead of `find_cik` (avoids the live per-ticker fallback).
- [x] **Task 4 — `fintin universe` CLI command** (AC: 1, 2, 3) — `fintin/cli/app.py`
  - [x] New `universe` command: load config (→ `ConfigError` exit 2), require a non-empty `[universe]` (else clean error exit 2), lazily import the core resolver + edgar adapter (defer the heavy `edgar` import like `ingest-company` does), call `resolve_universe`, and render: count of companies in the resolved Universe, how many came from explicit CIKs vs resolved tickers, and each **explained gap** (unresolved ticker + reason). Exit **0** even with gaps (recorded, not fatal); exit 2 only on config/structure errors.
  - [x] No ClickHouse connection and no `EdgarClient` — resolution is offline. Do NOT construct `EdgarClient` (it would demand a real email for an offline op).
  - [x] Add a `--show-ciks` flag to also print the sorted resolved CIK list (default: summary + gaps only, since the full list can be 500 long).
  - [x] Error rendering matches the house style: `typer.secho(..., fg=RED, err=True)` + `raise typer.Exit(code=...)`; never leak a traceback.
- [x] **Task 5 — Config template** — `fintin.toml.example` (tracked; public repo)
  - [x] Add a `[universe]` block with a **representative** illustrative set (a dozen or so well-known S&P 500 tickers) + one or two example `ciks`, with a comment that the operator populates the full S&P 500 list here and that tickers/CIKs are public (safe to commit; unlike the contact email). Note that unresolved tickers surface as explained gaps.
- [x] **Task 6 — Tests (never live EDGAR; NFR-7)** — `tests/`
  - [x] `tests/test_config.py`: `[universe]` parsing — valid tickers+ciks; absent section → `universe is None`; both-empty / missing section for the universe path → `ConfigError`; non-string ticker, non-int CIK, `bool` CIK, out-of-range CIK (0 and > 4_294_967_295) all → `ConfigError`; a pure-CIK universe (no tickers) parses.
  - [x] `tests/test_universe.py` (NEW, pure): `resolve_universe` with an **injected fake** `resolve_tickers` — tickers union with explicit CIKs; an unresolved ticker (`None`) becomes a `UniverseGap` and the rest still resolve; duplicate CIK (ticker resolves to an explicit CIK) deduped; CIKs returned sorted; adding a CIK grows the set (AC-3); a pure-CIK universe never calls the resolver. Plus an **AST import-guard** (like `test_canonical.py`) asserting `fintin/core/universe.py` imports no `edgar`.
  - [x] `tests/test_edgar_universe.py` (NEW): the real adapter offline — `AAPL` → `320193`, `BRK.B`/`brk.b` normalize and resolve, a nonsense ticker (`Z> ZZINVALID`) → `None`. Include ONE test that blocks the network (patch `socket.socket.connect` + `socket.create_connection` to raise, **after** importing edgar) and asserts a resolve still succeeds — a hard NFR-7 proof that the bundled path touches no socket. (Do not null out `socket.socket` itself — that breaks ssl import; block only the connect methods, per the Story 1.4 lesson.)
  - [x] `tests/test_cli.py`: `--help` lists `universe`; missing config → exit 2 + "Config error" + no Traceback; missing `[universe]` → clean error exit 2; a config with a bogus ticker → exit 0, output names the ticker as a gap, no Traceback; `--show-ciks` prints a resolved CIK. These are offline (bundled resolver) — no ClickHouse, no integration marker.
- [x] **Task 7 — Validate & document**
  - [x] `uv run pytest` green (unit + integration with ClickHouse up — this story adds no integration tests, but must not regress existing ones).
  - [x] README: add `[universe]` to the Configuration section and a short "Define your Universe" note (tickers resolve offline via edgartools' bundled table; unresolved tickers are explained gaps; add CIKs to grow it). Do NOT overstate — no backfill yet (that's Story 2.3); `fintin universe` only resolves + reports scope.

## Dev Notes

### Current substrate (Epic 1, on main)

- **Config** (`fintin/config.py`): `load_config` → `Config(clickhouse, edgar)`. `[edgar]` is the optional-section pattern to copy for `[universe]` (absent → `None`; present → parsed with strict type/range guards; `bool`-rejects-before-int). All problems raise `ConfigError`, which the CLI renders as a clean message (never a traceback).
- **Core purity pattern** (`fintin/core/ingest.py`, `core/canonical.py`): pure modules with **injected ports** (`fetch_facts`, `insert_rows`, `read_raw_facts`). No `edgar`/ClickHouse import in `core`. `resolve_universe` follows this exactly — inject `resolve_tickers`.
- **edgar adapter isolation** (`fintin/adapters/edgar/`): the ONLY place `edgar` is imported. `client.py` (the rate-limited `EdgarClient`) + `facts.py` (`get_company_facts` through `EdgarClient.run`). The new `universe.py` adds the offline bundled-table lookup — the one edgar call that legitimately does NOT go through `EdgarClient` because it issues no request.
- **CLI pattern** (`fintin/cli/app.py`): Typer multi-command group; each command is a dumb trigger (AD-2). `_configure_logging()`, lazy `edgar` imports for heavy paths, CIK range guard `1 <= cik <= 4_294_967_295`, `ConfigError` → exit 2, connection/op failures → exit 1, `typer.secho(fg=RED, err=True)`, never a traceback. Copy the `map-canonical` command shape (which also skips `EdgarClient`) — `universe` is even simpler (no ClickHouse either).

### edgartools 5.43.0 — verified resolution facts (do not re-investigate)

- Bundled parquet `edgar/reference/data/company_tickers.parquet` ships in the package; **first lookup is offline**, not a network hit.
- `edgar.reference.tickers.get_company_cik_lookup()` → `{TICKER: cik}` incl. base-ticker aliases (e.g. `BRK` for `BRK-A`); `edgar.get_ticker_to_cik_lookup()` is the top-level equivalent. Both `@lru_cache(maxsize=1)`, bundled-only (no live fallback).
- `find_cik("ZZZZ")` returns `None` (doesn't raise); `Company("ZZZZ")` raises `CompanyNotFoundError`. We use neither — the dict getter + our own `.get(ticker)` gives `None`-for-absent without the network fallback.
- Any network path (which we avoid) goes through the rate-limited `HTTP_MGR`; there is no unthrottled EDGAR HTTP anywhere.

### Architecture constraints (authoritative)

- **AD-13** — "The Universe is a config list of CIKs; tickers resolve to CIKs via edgartools at load." Backfill is strategy-pluggable; this story only supplies its *scope*. [ARCHITECTURE-SPINE.md#AD-13]
- **AD-1** — derive state, never maintain a driftable copy. The Universe is computed from config each run; **nothing persists it** (no cached CIK table). [#AD-1]
- **AD-2** — pure engine, dumb triggers. `resolve_universe` is pure core; `universe` CLI is a dumb trigger. [#AD-2]
- **AD-3** — all EDGAR *requests* through the one client. The bundled-table lookup issues **no request**, so it is compliant by making no call (and deliberately avoids `find_cik`'s network fallback). [#AD-3]
- **NFR-2** — Universe-agnostic scalability: growing the Universe is a config edit, no code/schema change. [epics.md#NFR-2]
- **NFR-7 / Testing** — never hit live EDGAR in tests. Resolution is offline; the adapter test additionally blocks sockets to prove it. [ARCHITECTURE-SPINE.md#Testing]
- **SM-2 / Errors & status** — a per-company problem is a **recorded explained gap, never a silent omission**; the run continues. An unresolvable ticker is that, at Universe scope. [#Errors & status]
- **Universe sourcing (Deferred)** — dynamic S&P 500 membership tracking is out of v1; the list is static config. [#Deferred]

### Project Structure Notes

- New files: `fintin/core/universe.py` (pure), `fintin/adapters/edgar/universe.py` (offline edgar). Modified: `fintin/config.py`, `fintin/cli/app.py`, `fintin.toml.example`, `README.md`. New tests: `tests/test_universe.py`, `tests/test_edgar_universe.py`; extended: `tests/test_config.py`, `tests/test_cli.py`.
- No schema/DDL change (this story is pre-ingestion scope), so `adapters/store` is untouched. No new dependency (edgartools already pinned 5.43.0).
- Public-repo constraint: tickers and CIKs are **public data** — the full list is safe to commit in `fintin.toml.example`. The gitignored real `fintin.toml` still holds only the operator's real contact email as sensitive. Do not put an email in the universe example.

### Previous Story Intelligence (Epic 1)

- **Offline-proof socket blocking (Story 1.4/1.5 lesson):** to prove a path is offline, block `socket.socket.connect` + `socket.create_connection` **after** the import that needs SSL — do NOT set `socket.socket = None` (breaks ssl import) and do NOT block sockets around ClickHouse-touching code (localhost is a socket). This adapter test has no ClickHouse, so a clean connect-block proof is safe.
- **AST import-guard pattern** (`tests/test_canonical.py`): parametrized parse of a module's AST asserting no `import edgar` — reuse it to lock `core/universe.py` edgar-free.
- **`bool` is an `int` subclass** — every numeric config guard rejects `bool` first (see `_parse_clickhouse`/`_parse_edgar`); the CIK guard must too.
- **Lazy heavy imports in the CLI** keep `--help`/config-error paths fast; `edgar` is heavy — import the adapter inside the command body.
- Deterministic output matters (kboss values exactness/determinism): sort the CIKs and order gaps by config order.

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story-2.1] and #Epic-2 refinement (build resumability once; 2.1 supplies scope only)
- [Source: ARCHITECTURE-SPINE.md#AD-13, #AD-1, #AD-2, #AD-3, #Consistency-Conventions (Identity: cik=UInt32), #Deferred (static Universe)]
- [Source: fintin/config.py#_parse_edgar — optional-section + type/range guard pattern to mirror]
- [Source: fintin/core/ingest.py#ingest_company — injected-port purity pattern]
- [Source: fintin/cli/app.py#map_canonical_command — a CLI command that skips EdgarClient (closest shape)]
- [Source: edgartools 5.43.0 installed — edgar/reference/tickers.py:267 get_company_cik_lookup, :103 get_company_tickers, :57 bundled parquet loader]

## Review Findings

Code review of story-2.1 (2026-07-24) — 3 layers (Blind Hunter, Edge Case Hunter, Acceptance Auditor), all completed. Acceptance Auditor verdict: implementation satisfies all 3 ACs with genuine tests. Adversarial layers found real hardening gaps (verified against installed edgartools source). **7 patch, 1 defer, 0 decision-needed. Verdict: changes requested.**

- [x] [Review][Patch] **Resolve directly over the bundled loader — no network fallback, exact-key match.** `get_company_cik_lookup()` → `_get_company_tickers_raw()` falls through to a live `download_json(sec.gov)` fetch when the bundled parquet fails to load (verified `tickers.py`), bypassing the rate-limited client (AD-3) + FR-1 email gate — so "guaranteed offline" is conditional. It also injects base-ticker aliases (`ticker.split('-')[0]`), so a bare `BRK`/`CRD` silently resolves to a share-class issuer instead of being a gap (Edge #3; ~25 such bare-base keys, latent nondeterminism). Fix both by building `{TICKER: int(cik)}` directly from `load_company_tickers_from_package()` (exact keys only; hard-fail with a clear error on `None` — never the network). [fintin/adapters/edgar/universe.py]
- [x] [Review][Patch] **Wrap resolution in the `universe` command so a resolver/import failure renders cleanly, not as a traceback.** Only `load_config` is guarded; the lazy `edgar` import + `resolve_universe` call are bare, so an `ImportError`/parquet error/loader-None leaks a Python traceback — violates the house "clear error, not a stack trace" contract every sibling command honors. Wrap in `try/except Exception` → `typer.secho("Universe resolution failed: …", RED, err=True); Exit(1)`. [fintin/cli/app.py]
- [x] [Review][Patch] **Fail loudly on an empty resolved Universe.** A config that resolves to zero CIKs (e.g. only unresolvable tickers) prints green `Universe: 0 companies` and exits 0 — a downstream backfill (Story 2.3) or CI gate keying on exit code would proceed over an empty scope silently. When `resolved.ciks` is empty → distinct warning + `Exit(1)` (gaps alongside a non-empty Universe stay non-fatal, exit 0). [fintin/cli/app.py]
- [x] [Review][Patch] **Dedup tickers on normalized form.** Config keeps tickers verbatim and core iterates them, so `["BRK.B","BRK-B","brk.b"]` (or `["AAPL","AAPL"]`) inflates `tickers_resolved` (unreconcilable scope line) and emits duplicate gap lines. Dedup by normalized form (first-seen order) before resolving/counting. [fintin/core/universe.py]
- [x] [Review][Patch] **Range-check ticker-resolved CIKs against UInt32.** Config CIKs are validated `1..4_294_967_295`, but a ticker-resolved CIK passes through `int(cik)` unchecked — apply the same guard so an out-of-range resolution becomes a gap, not a downstream store insert failure (latent; max bundled CIK ≈ 2.1M). [fintin/core/universe.py]
- [x] [Review][Patch] **Pluralize "company"/"companies".** `f"{n} companies"` always pluralizes ("1 companies"); test asserts the literal. Cosmetic. [fintin/cli/app.py, tests/test_cli.py]
- [x] [Review][Patch] **Activate an example CIK in the template.** Task 5 asked for one or two example `ciks`; all are commented out. Uncomment a real public CIK (Alphabet 1652044) as a live example. [fintin.toml.example]
- [x] [Review][Defer] **Store keys `cik` as UInt32 (Story 1.2), but SEC CIKs are nominally up to 10 digits (> 2³²).** Currently unreachable (max assigned CIK ≈ 2.1M); pre-existing schema choice, not caused by this change. Revisit the column width if SEC assignments ever approach 2³². [fintin/adapters/store/schema.py] — deferred, pre-existing.

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Opus 4.8, 1M context)

### Debug Log References

- Full suite green: `uv run pytest -q` → **165 passed** (was 132; +33 for this story). No regressions.
- Verified `edgar.reference.tickers.get_company_cik_lookup()` returns a `dict` (~10,390 entries, `int` values) offline: `AAPL`→320193, `BRK-B`→1067983, `MSFT`→789019, `AMZN`→1018724.
- Smoke test `fintin universe` on a temp config (`tickers=["AAPL","MSFT","BRK.B","ZZZZINVALID"]`, `ciks=[1018724]`): `Universe: 4 companies (1 from CIKs, 3 from tickers)` + `ZZZZINVALID` reported as an explained gap; `--show-ciks` → `320193 789019 1018724 1067983` (sorted, deduped).

### Completion Notes List

- **AC-1 (offline resolution).** `resolve_universe` (pure core) unions explicitly-listed CIKs with the tickers resolved by an injected batch `resolve_tickers` port. The edgar adapter (`adapters/edgar/universe.py`) implements that port over edgartools' **bundled** `company_tickers.parquet` via `get_company_cik_lookup()` — a file read, **zero network** (AD-3 not triggered; no `EdgarClient`/email). `test_resolution_touches_no_socket` blocks `socket.socket.connect` + `socket.create_connection` (after edgar import; caches cleared to force an uncached rebuild) and proves resolution still succeeds — a hard NFR-7 guard.
- **AC-2 (gaps + config errors).** An unresolvable ticker → `UniverseGap(ticker, reason)` surfaced by `fintin universe` (exit 0, non-fatal; the rest still resolve — SM-2). Structural problems (missing `[universe]`, both lists empty, non-string ticker, non-int/`bool`/out-of-range CIK) → `ConfigError` → CLI exit 2, no traceback.
- **AC-3 (NFR-2).** The Universe is derived from config each run — nothing persists it (AD-1). `test_adding_a_cik_grows_the_universe_no_code_change` proves adding a CIK grows the set with no code/schema change. `fintin.toml.example` gained a `[universe]` block (public data; no email).
- **Design decision honored:** used the bundled dict getter, NOT `find_cik`/`Company` (which have a per-ticker live SEC fallback) — keeps resolution guaranteed-offline/deterministic and turns an absent ticker into a clean `None`.
- **Purity locked:** AST guard asserts `core/universe.py` imports no `edgar`; `adapters/edgar/universe.py` is the only new module importing `edgar` (lazily, inside the function).
- No schema/DDL change; `adapters/store` untouched. No new dependency.

### File List

- `fintin/config.py` (MOD) — `UniverseConfig` dataclass + `universe` field on `Config`; `_parse_universe` (structure/type/range guards, `bool`-rejects-before-int, empty-Universe error); wired into `load_config`.
- `fintin/core/universe.py` (NEW) — pure `resolve_universe` + `ResolvedUniverse`/`UniverseGap`; injected batch `resolve_tickers` port; derived-not-persisted (AD-1).
- `fintin/adapters/edgar/universe.py` (NEW) — offline `resolve_tickers` over edgartools' bundled table (`get_company_cik_lookup`), ticker normalization (`upper`, `.`→`-`), original-key preservation; docstring explains offline + why not `find_cik`.
- `fintin/cli/app.py` (MOD) — `universe` command (offline; no ClickHouse/`EdgarClient`; `--show-ciks`; config/section errors → exit 2; gaps non-fatal exit 0).
- `fintin.toml.example` (MOD) — `[universe]` block (representative S&P 500 tickers + example `ciks`; public-data note).
- `tests/test_config.py` (MOD) — `[universe]` parse + rejection cases.
- `tests/test_universe.py` (NEW) — pure `resolve_universe` (union/dedup/sort, gap, pure-CIK skips resolver, NFR-2 growth, config-ordered gaps) + AST edgar-free guard.
- `tests/test_edgar_universe.py` (NEW) — offline adapter (known/normalized/unknown/batch) + socket-block NFR-7 proof.
- `tests/test_cli.py` (MOD) — `universe` help/missing-config/missing-section/gap/`--show-ciks`.
- `README.md` (MOD) — `[universe]` in Configuration + a "Define your Universe" section.

## Change Log

- 2026-07-24 — Story 2.1 implemented: resolve the configured Universe (tickers and/or CIKs) to a deduplicated, sorted CIK set. Ticker→CIK resolution is **offline** via edgartools' bundled reference table (`get_company_cik_lookup`) — no EDGAR request, no contact email (AD-3 not triggered); the plain dict getter is used deliberately over `find_cik` to avoid its live network fallback. Pure core `resolve_universe` with an injected batch resolver port; offline edgar adapter; a `fintin universe` CLI trigger (`--show-ciks`). Unresolvable tickers → explained gaps (SM-2); the Universe is derived from config, never persisted (AD-1). 165 tests pass (+33). Status → review.
- 2026-07-24 — Code review (3 layers, all completed). Acceptance Auditor: all 3 ACs met with genuine tests. Applied all 7 patch findings: (1) **rebuilt the resolver on `load_company_tickers_from_package()` directly** — hard-fails (`UniverseReferenceError`) on a missing bundled table instead of falling through to a live SEC fetch, and matches **exact ticker keys only** (no base-ticker aliases, so bare `BRK`/`CRD` → gap not a silent share-class hit); (2) wrapped resolution in the `universe` command (clean error, no traceback); (3) empty resolved Universe → warn + **exit 1** (was green exit 0); (4) dedup tickers on normalized form (no inflated count / duplicate gap lines); (5) range-check ticker-resolved CIKs to UInt32 → gap; (6) pluralize "1 company"; (7) activate an example CIK in the template. `normalize_ticker` moved to `core` (pure) and shared with the adapter. 1 defer (store `cik` UInt32 vs 10-digit CIK — pre-existing, logged in deferred-work.md). **175 tests pass** (+10). Status → done. Files also touched by the review: `fintin/core/universe.py`, `fintin/adapters/edgar/universe.py` (`UniverseReferenceError`), `fintin/cli/app.py`, `fintin.toml.example`, `tests/test_universe.py`, `tests/test_edgar_universe.py`, `tests/test_cli.py`.
