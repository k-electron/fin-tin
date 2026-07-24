---
stepsCompleted:
  - step-01-validate-prerequisites
  - step-02-design-epics
  - step-03-create-stories
inputDocuments:
  - _bmad-output/planning-artifacts/prds/prd-fin-tin-2026-07-23/prd.md
  - _bmad-output/planning-artifacts/architecture/architecture-fin-tin-2026-07-23/ARCHITECTURE-SPINE.md
  - _bmad-output/planning-artifacts/architecture/architecture-fin-tin-2026-07-23/BUILD-SPLIT.md
---

# fin-tin - Epic Breakdown

## Overview

This document provides the complete epic and story breakdown for fin-tin, decomposing the requirements from the PRD and Architecture Spine (build-split) into implementable stories. There is no UX design contract — v1 is a SQL query surface with no UI.

## Requirements Inventory

### Functional Requirements

FR-1: All EDGAR access is funneled through one centralized, rate-limited client (≤10 req/s, mandatory declared identifying User-Agent, self-imposed ≥10-minute cool-down on throttle failure).
FR-2: The system minimizes EDGAR request count — index/feed for discovery, bulk/per-company for content, per-Filing API only for the incremental tail.
FR-3: The system ingests all standard-taxonomy facts for in-scope companies into an immutable Tier 0 raw fact landing keyed by accession, with full provenance (raw tag, label, unit, period, filed date, content hash).
FR-4: The system maps Tier 0 facts to canonical concepts into a Tier 1 canonical store, rebuildable from Tier 0 with zero network calls.
FR-5: For any (canonical concept, unit, period) the queryable value is the most-recently-filed (latest-filed-wins); all filed versions are retained and distinguishable.
FR-6: Tier 0 content for any accession/company can be re-fetched from EDGAR and rebuilt, repairing local corruption or loss.
FR-7: The system backfills full available history for the configured Universe (v1: S&P 500), via a strategy-pluggable interface (per-company API in v1).
FR-8: A single command brings the store current by ingesting everything filed since the high-water mark ("catch up to today").
FR-9: The reconciler derives all outstanding work from the store's contents + EDGAR's indexes; it maintains no separate progress state.
FR-10: Every long operation survives crash and pause via idempotent incremental (per-company) commits and the DB-derived work list — no checkpoint file.
FR-11: At most one ingestion run executes at a time (single-flight), guarded by a self-expiring filesystem lease; a trigger during an active run returns ALREADY_RUNNING gracefully.
FR-12: The catch-up engine is a pure command with no knowledge of its caller; v1 exposes a manual CLI trigger, with other triggers as future wrappers.
FR-13: The system maintains a wide Screening Mart (one row per company-period, canonical concepts as columns) queryable with raw SQL, reflecting latest-filed-wins.
FR-14: The user can see ingestion coverage and currency for the configured Universe (companies present, high-water mark, explained gaps for zero-fact/failed companies).

### NonFunctional Requirements

NFR-1: Reliability / crash-safety — any operation is safe to interrupt at any moment and re-run, with no partial write corrupting the store and no progress lost.
NFR-2: Universe-agnostic scalability — nothing in the schema or pipeline assumes a Universe size; growing from S&P 500 toward the full us-gaap market is a configuration change, not a redesign.
NFR-3: Performance — a curated-Universe backfill completes within a single unattended session on a developer laptop; screens over the mart return interactively (single-digit seconds).
NFR-4: Observability — runs emit progress and a final status (STARTED/ALREADY_RUNNING/NOTHING_TO_DO/COMPLETED); coverage and gaps are queryable.
NFR-5: Portability / deployment — runs locally on a single node (macOS) with ClickHouse in a container and a Python CLI; no cloud dependency.
NFR-6: Cost — external cost is $0 (EDGAR is free); local disk footprint scales with the Universe.
NFR-7: EDGAR fair-access compliance (ban-critical) — 10 req/s max per requester across all hosts; mandatory declared identifying User-Agent; 10-minute cool-down on breach; throttle + single-flight live in the engine, not the trigger.
NFR-8: Restatement correctness — original and restated values are both retained (keyed by accession + filed date) and never clobbered; only true corruption is overwritten.
NFR-9: Legal / ToS — respect EDGAR's terms of use; the identifying User-Agent carries real contact information.
NFR-10: Taxonomy coupling (standing dependency) — the corpus is coupled to edgartools' mapping/versions; mitigated (not eliminated) by per-fact taxonomy-version provenance and the deferred re-map path.

### Additional Requirements

Extracted from the Architecture Spine (ADs) and Build-Split — technical/implementation constraints that shape stories:

- **Greenfield scaffold (no starter template):** a `uv`-managed Python project (`pyproject.toml`, `requires-python >=3.12`), Typer CLI over a pure engine core. This is Epic 1 / Story 1.
- **Pinned stack (web-verified 2026-07-23):** Python ≥3.12, ClickHouse 26.3 (LTS), edgartools 5.43.0, clickhouse-connect 1.6.0, Typer 0.27.0, uv 0.11.32, Docker Compose (host-provided).
- **Deployment:** ClickHouse single-node via `docker-compose.yml` with a mounted volume so the corpus persists across restarts; single local environment.
- **Single DDL owner + creation order (AD-18):** one component (`adapters/store`) owns all ClickHouse DDL/migrations; Tier 0, Tier 1, the Resolution MV, and the wide Mart are created **before any backfill insert** (ClickHouse MVs do not backfill pre-existing rows).
- **Insert-only mutation (AD-6):** Tier 0 and Tier 1 are `ReplacingMergeTree(version)` with an **ingest-monotonic** version column (not filed_date); no UPDATE/DELETE in normal operation; reads via `FINAL`/`argMax` and never assume a merge ran.
- **Fact identity + consolidated-only (AD-5/AD-15):** Tier 0/Tier 1 key = `(accession, raw_tag, period_start, period_end, unit)`; v1 ingests **consolidated facts only** (no dimensional/segment axis members).
- **Period representation (AD-17):** instant facts stored `period_start = period_end = instant_date`; duration facts `period_start < period_end`.
- **Concept dimension (AD-9):** canonical concepts come solely from the edgartools standardization taxonomy; only `us-gaap`/`dei`/`srt`; `taxonomy_version` = edgartools version string.
- **Correctness authority (AD-16):** per-accession **membership over a reordering-safe LOOKBACK window** decides done-ness; `max(filed)` is only a scan-bounding hint.
- **Throttle placement (AD-3):** rate enforced at **edgartools' own throttle** (it owns the socket / sub-request fan-out), plus `Accept-Encoding: gzip,deflate` and honoring `Retry-After` if present.
- **Single-flight lease (AD-12):** a **filesystem** lease file (path from config) with heartbeat ≪ TTL; a run in EDGAR cool-down keeps heartbeating; NOT stored in ClickHouse.
- **Provenance + recovery (AD-14):** `content_hash` = sha256 over the normalized raw-fact tuple; recovery re-ingests (ingest-monotonic version) and re-derives Tier 1 → Resolution → Mart.
- **Resolution + wide mart (AD-7/AD-8):** deterministic latest-filed-wins tiebreak (prefer `/A`, then greatest accession); resolution via `AggregatingMergeTree` (`argMaxState`), then a wide screening view pivoting concepts to columns. A future re-map requires a mart rebuild.
- **Config (single TOML):** Universe (CIK/ticker list), rate ceiling, identifying User-Agent + contact email, ClickHouse connection, lease path, LOOKBACK.
- **Universe sourcing:** static config list of tickers/CIKs; tickers→CIK resolved via edgartools at load; no live index-membership tracking.
- **Error handling:** a per-company ingest failure is recorded (not fatal); the run continues and the coverage report lists zero-fact/failed companies as explained gaps.
- **Testing:** EDGAR-touching code tested against **recorded fixtures**; never hit live EDGAR in tests/CI (ban risk). SM-1's restatement test set is a fixture.
- **CI/CD (out of v1 scope):** no CI pipeline in v1 — fin-tin is a single-operator local tool; the fixture-based suite runs locally (`uv run pytest`). A CI wrapper running the same fixtures is a trivial future add and, per the Testing rule above, must never touch live EDGAR.

### UX Design Requirements

None — v1 has no UI. The query surface is raw SQL against ClickHouse; no visual identity, components, or interaction patterns to implement.

### FR Coverage Map

FR-1: Epic 1 — centralized rate-limited EDGAR client
FR-2: Epic 2 — request-count minimization (index-based discovery in the work list, Story 2.2; per-company content, Story 2.3; bulk-artifact path deferred)
FR-3: Epic 1 — Tier 0 raw fact landing
FR-4: Epic 1 — Tier 1 canonical store
FR-5: Epic 1 — latest-filed-wins (restatement-fixture tested)
FR-6: Epic 3 — Tier 0 recovery (thin scoped-catch-up flag)
FR-7: Epic 2 — configured-Universe backfill (per-company strategy, v1)
FR-8: Epic 3 — catch-up to today
FR-9: Epic 2 — DB-derived work list (membership over lookback)
FR-10: Epic 2 — universal resumability (per-company idempotent commits)
FR-11: Epic 3 — single-flight self-expiring lease
FR-12: Epic 3 — pure engine / decoupled CLI trigger
FR-13: Epic 1 — wide screening mart
FR-14: Epic 2 — coverage & status report

## Epic List

### Epic 1: Prove an end-to-end trustworthy screen (walking skeleton)
Stand up the store and pipeline and prove — on one company as the *test vehicle*, not the deliverable — that a raw EDGAR fact becomes a normalized, latest-filed-correct value queryable via SQL through the wide mart. De-risks the ClickHouse mutation/resolution mechanics (AD-6, AD-8) and the edgartools integration before any scale.
**FRs covered:** FR-1, FR-3, FR-4, FR-5, FR-13
**Refinement notes:**
- One CIK is the test vehicle — no throwaway single-company special-casing; the pipeline is general.
- FR-5 acceptance **rides a restatement fixture** (two filings of the same period with different filed-dates → newer wins), not a happy path. This is the product-defining test — a screener that serves stale numbers is a failure.

### Epic 2: Backfill the S&P 500 Universe
Populate the full v1 Universe so screening spans the market; the coverage report shows what's in and what's an explained gap.
**FRs covered:** FR-2, FR-7, FR-9, FR-10, FR-14
**Refinement notes:**
- Build the resumability mechanism **once** here — FR-9/FR-10 (per-accession membership over a lookback window + per-company idempotent commits). Epic 3 reuses it; a story-review guardrail prevents a second implementation.

### Epic 3: Keep it current & safe (catch-up, concurrency, recovery)
Bring the store up to date any time with one command, safely — single-flight so concurrent runs can't trigger an EDGAR ban, and recoverable from source if Tier 0 corrupts.
**FRs covered:** FR-6, FR-8, FR-11, FR-12
**Refinement notes:**
- Catch-up (FR-8) and the single-flight self-expiring lease (FR-11) are **first-class** stories; FR-11 is ban-avoidance (NFR-7), a real 3am risk, not theoretical.
- Recovery (FR-6) is a **thin** story: `fintin recover --cik X` = catch-up scoped to one accession/company, riding Epic 2's machinery — not a new subsystem.
- **Reuses** (never re-implements) Epic 2's resumability / work-list mechanism.

**Dependency flow:** Epic 1 → Epic 2 → Epic 3. Each is standalone and does not require a later epic to function. NFRs are cross-cutting and enforced within the relevant stories (esp. NFR-7 EDGAR compliance, NFR-1 crash-safety).

## Epic 1: Prove an end-to-end trustworthy screen (walking skeleton)

Stand up the store and pipeline and prove — on one company as the *test vehicle*, not the deliverable — that a raw EDGAR fact becomes a normalized, latest-filed-correct value queryable via SQL through the wide mart.

### Story 1.1: Runnable skeleton connected to ClickHouse

As the builder,
I want a uv-managed Python project with a Typer CLI and a local ClickHouse it connects to,
So that I have a running foundation to build the pipeline on.

**Acceptance Criteria:**

**Given** a clean checkout **When** I run `uv sync` then `docker compose up -d` **Then** ClickHouse 26.3 starts with a mounted volume **And** `fintin --help` lists the CLI.
**Given** the container is running **When** I run the connection-check command **Then** the app connects via clickhouse-connect using `fintin.toml` **And** reports success.
**Given** `fintin.toml` is missing or malformed **When** any command runs **Then** it fails with a clear config error, not a stack trace.
**Given** the container is stopped and restarted **When** it returns **Then** data on the mounted volume persists.

### Story 1.2: Store schema and DDL (single owner, correct creation order)

As the builder,
I want the store adapter to create Tier 0, Tier 1, the resolution MV, and the wide mart in the correct order,
So that ingestion and querying have a correct, mutation-safe schema before any data lands.

**Acceptance Criteria:**

**Given** an empty ClickHouse **When** schema-init runs **Then** `raw_fact` (Tier 0) and `canonical_fact` (Tier 1) are `ReplacingMergeTree` ordered by `(accession, raw_tag, period_start, period_end, unit)` with an ingest-monotonic `version` column (AD-5, AD-6, AD-15).
**Given** the base tables exist **When** schema-init runs **Then** the resolution MV (`AggregatingMergeTree`, `argMaxState`) and the wide mart are created **before any insert** (AD-18) **And** only the store adapter issues DDL.
**Given** schema-init is run twice **Then** it is idempotent (no error, no duplicate objects).
**Given** instant vs. duration facts **Then** the schema represents them per AD-17 (instant: `period_start = period_end`).

### Story 1.3: Compliant rate-limited EDGAR client

As the builder,
I want one EDGAR client that obeys SEC fair-access,
So that every fetch is safe from a ban.

**Acceptance Criteria:**

**Given** the client is configured **When** it makes any request **Then** it sends the configured identifying User-Agent (name + contact email) and `Accept-Encoding: gzip,deflate` **And** its rate is capped at edgartools' own throttle ≤10 req/s (AD-3, FR-1).
**Given** EDGAR returns a throttle failure **When** the client detects it **Then** it honors `Retry-After` if present, else self-imposes a ≥10-minute cool-down, then retries — without crashing the run.
**Given** any code path touches EDGAR **Then** it goes through this client (no direct HTTP elsewhere), verified by tests/construction.
**Given** tests run **Then** they use recorded fixtures, never live EDGAR.

### Story 1.4: Land one company's raw facts in Tier 0

As the builder,
I want to ingest a single company's standard-taxonomy facts into Tier 0 with full provenance,
So that the raw local mirror exists for that company.

**Acceptance Criteria:**

**Given** a CIK **When** I ingest it **Then** its `us-gaap`/`dei`/`srt` numerical facts land in `raw_fact` keyed by raw-fact identity with provenance (`raw_tag`, `raw_label`, `unit`, period, `filed_date`, `content_hash`=sha256 of the normalized tuple, `taxonomy_version`) (FR-3, AD-14).
**Given** facts carrying dimensional/segment axis members **Then** they are NOT ingested (consolidated-only, AD-15).
**Given** the same CIK is ingested twice **Then** Tier 0 is unchanged on read (idempotent insert; ingest-monotonic version; FINAL/argMax) (AD-6).
**Given** a tag outside the standard taxonomies **Then** it is not stored as a mapped concept (AD-9 scope).

### Story 1.5: Map raw facts to canonical Tier 1 (standard-element concepts)

As the builder,
I want Tier 0 facts projected into Tier 1 keyed by their standard XBRL element, with zero network,
So that every fact is addressable by an exact, unambiguous standard concept.

**Acceptance Criteria:**

**Given** Tier 0 has a company's facts **When** I run the projection **Then** `canonical_fact` is populated with `canonical_concept` = the fact's standard element local name (e.g. `Assets`, `RevenueFromContractWithCustomerExcludingAssessedTax`) — a 1:1, lossless projection of `raw_tag` (namespace stripped), keyed by raw-fact identity, issuing zero EDGAR/network requests (FR-4, AD-4, AD-9).
**Given** every ingested fact is already `us-gaap`/`dei`/`srt` scope (AD-9/AD-15) **Then** every Tier 0 fact projects to exactly one Tier 1 row — no statistical standardization and no "unmappable" drop; `canonical_concept` is exact and unambiguous by construction, and each row carries `taxonomy_version` (carried over from Tier 0).
**Given** the projection is re-run **Then** it is an in-place upsert with no orphaned/duplicate rows on read (ingest-monotonic version; ReplacingMergeTree; FINAL) (AD-5, AD-6).

_Cross-company screening concepts (revenue, net income, …) are NOT built here — they are the versioned concept dictionary over these elements, delivered in Story 1.6 (AD-8/AD-9)._

### Story 1.6: Resolve latest-filed-wins and screen via the wide mart

As the builder,
I want the wide screening mart to return the latest-filed value per company-period,
So that a SQL screen returns trustworthy (post-revision) numbers.

**Acceptance Criteria:**

**Given** multiple filed versions of the same (element, unit, period) **When** I query the mart **Then** it returns the most-recently-filed value, tiebreak prefer `/A` then greatest accession (FR-5, AD-7).
**Given** the restatement fixture (two filings of one period, different `filed_date`, differing values) **When** resolved **Then** the newer value wins — **this AC is required; it is the product-defining test.**
**Given** a versioned **concept dictionary** (each screening concept = an ordered list of standard elements: FASB-primary + observed-frequency-ranked fallbacks) **When** the mart resolves a concept for a `(cik, period)` **Then** it returns the **latest-filed** value across the *union* of that concept's elements, breaking ties deterministically by element list-position (then the AD-7 filing tiebreak) — so recency is respected (AD-7) AND multiple elements collapsing to one screening concept never produce a nondeterministic value (AD-8, AD-9). _(Position-first resolution is insufficient: it can return a stale or subtotal value when a period is reported/restated under different elements across filings.)_
**Given** Tier 1 receives inserts **When** I query **Then** the mart reflects them (auto-populated) presented **wide**: one row per `(cik, period)` with screening concepts as columns (FR-13, AD-8).
**Given** a SQL screen (concept > threshold for a period) **When** run against the mart **Then** it returns the matching company-period rows.
**Given** a representative cross-sectional screen over the mart **When** run on the developer laptop **Then** it returns in single-digit seconds — a soft NFR-3 sanity target (regression tripwire), not a hard SLA.

## Epic 2: Backfill the S&P 500 Universe

Populate the full v1 Universe so screening spans the market; the coverage report shows what's in and what's an explained gap.

### Story 2.1: Resolve the Universe from config

As the builder,
I want the S&P 500 Universe defined in config and resolved to CIKs,
So that ingestion knows its scope and can be widened later by editing config alone.

**Acceptance Criteria:**

**Given** `fintin.toml` lists tickers/CIKs **When** the app loads **Then** tickers resolve to CIKs via edgartools **And** the Universe is available to the pipeline.
**Given** an unresolvable ticker **Then** it is reported as a config error / recorded gap, not silently dropped.
**Given** I add CIKs to the config **Then** the Universe grows with no code or schema change (NFR-2).

### Story 2.2: DB-derived work list via membership over lookback

As the builder,
I want outstanding work derived from the DB and EDGAR's index,
So that no cursor is maintained and restatements are caught.

**Acceptance Criteria:**

**Given** the store and a Universe **When** the work list is computed **Then** it = accessions in the EDGAR index over the lookback window minus accessions already present (membership authority, AD-16) **And** `max(filed)` is only a scan-sizing hint (FR-9, FR-2 — discovery reads the EDGAR index rather than crawling per-company pages).
**Given** an accession already in the store **Then** it is not re-fetched.
**Given** a newly-filed accession restating an old period **Then** it appears in the work list.

### Story 2.3: Per-company resumable backfill

As the builder,
I want to backfill the whole Universe resumably via the per-company strategy,
So that I can populate the market and survive interruptions.

**Acceptance Criteria:**

**Given** the Universe **When** I run backfill **Then** each company's full available history is ingested via the per-company `companyfacts` strategy behind the pluggable interface (FR-7, AD-13), committing per company.
**Given** backfill is killed mid-run **When** I restart it **Then** it resumes without re-ingesting already-committed companies (per-company idempotent commits + membership; no checkpoint file) (FR-10, AD-11, AD-16).
**Given** a per-company fetch fails **Then** it is recorded (not fatal) **And** the run continues.
**Given** a much larger Universe later **Then** the strategy can switch to bulk without redesign (interface-level; bulk impl deferred).
**Given** the full S&P 500 Universe from empty **When** a backfill runs **Then** it completes within a single unattended session on the developer laptop — a soft NFR-3 target, never traded against rate-limit compliance (SM-C1: fewer/slower requests are always preferred to a ban).

### Story 2.4: Coverage & status report

As the builder,
I want to see coverage and currency for the Universe,
So that I know what's ingested and what's an explained gap.

**Acceptance Criteria:**

**Given** an ingested store **When** I run `fintin status` **Then** it reports the count of in-scope companies present, the high-water mark (latest `filed_date`), and any in-scope companies with zero facts or recorded failures as **explained gaps** — no silent omissions (FR-14, SM-2).
**Given** a company failed during backfill **Then** it appears as an explained gap with a reason.

## Epic 3: Keep it current & safe (catch-up, concurrency, recovery)

Bring the store up to date any time with one command, safely — single-flight so concurrent runs can't trigger an EDGAR ban, and recoverable from source if Tier 0 corrupts.

### Story 3.1: Pure catch-up engine + CLI trigger

As the builder,
I want a single "catch up to today" command,
So that I can bring the store current any time before screening.

**Acceptance Criteria:**

**Given** the store at some high-water mark **When** I run `fintin catch-up` **Then** everything filed since (via the Epic 2 work-list mechanism, reused not re-implemented) is ingested **And** the run reports `STARTED`→`COMPLETED` (FR-8).
**Given** the engine **Then** it is a pure command with no knowledge of its caller; the CLI is a dumb trigger; throttle + single-flight live in the engine (FR-12, AD-2).
**Given** nothing new filed **When** catch-up runs **Then** it ingests nothing and returns `NOTHING_TO_DO`.

### Story 3.2: Single-flight self-expiring lease

As the builder,
I want at most one run at a time with a self-expiring lease,
So that concurrent triggers can't double the EDGAR rate (ban) or deadlock the tool.

**Acceptance Criteria:**

**Given** a run is active and heartbeating **When** a second trigger fires **Then** it returns `ALREADY_RUNNING` (exit-0) and issues no EDGAR requests — coalesce, don't queue (FR-11, AD-12, NFR-7).
**Given** a run crashes holding the lease **When** a later trigger sees the expired lease (TTL passed, heartbeat stale) **Then** it reclaims it and resumes the partial work.
**Given** a run is in an EDGAR cool-down **Then** it keeps heartbeating so its lease is not reclaimed mid-cool-down.
**Given** the status vocabulary **Then** `STARTED`/`ALREADY_RUNNING`/`NOTHING_TO_DO`/`COMPLETED` are all exit-0.

### Story 3.3: Scoped recovery

As the builder,
I want to re-fetch and rebuild a company/accession from EDGAR,
So that I can repair Tier 0 corruption or loss.

**Acceptance Criteria:**

**Given** a target CIK/accession **When** I run `fintin recover --cik X` **Then** it re-ingests from EDGAR via the normal throttled path (scoped catch-up), superseding the prior copy with an ingest-monotonic version, and re-derives Tier 1 → resolution → mart (FR-6, AD-6, AD-14).
**Given** recovery **Then** it reuses the existing ingest machinery — no new subsystem (thin flag).
**Given** automated corruption *detection* **Then** it is out of v1 scope (scrub is Should; ad-hoc reactive repair is Won't) — recovery here is manually invoked.
