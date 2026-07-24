---
title: fin-tin — Local EDGAR Financial-Statement Query Tool
status: final
created: 2026-07-23
updated: 2026-07-23
---

# PRD: fin-tin — Local EDGAR Financial-Statement Query Tool
*Working title — confirm.*

## 0. Document Purpose

This PRD is for the builder (kboss) as the sole developer, operator, and user of fin-tin, and for any downstream BMad workflow (architecture, epics/stories) that consumes it. It is deliberately lean and technical: capabilities and functional requirements live here; the detailed technical design — two-tier schema, Reconciler internals, ClickHouse specifics, the exact resumability mechanism — lives in the companion **architecture brief** at `../../brainstorming/brainstorm-edgar-financials-query-tool-2026-07-23/architecture-brief.md` (produced in the brainstorming session) and is referenced, not duplicated. Vocabulary is anchored in the Glossary (§3); features are grouped with globally-numbered FRs nested; inferred decisions are tagged `[ASSUMPTION]` inline and indexed in §11. This PRD builds on the brainstorming intent doc (same folder as the architecture brief).

## 1. Vision

fin-tin is a locally-hosted query tool that turns SEC EDGAR's messy, inconsistently-tagged financial disclosures into a clean, normalized, always-current-enough local corpus you can screen across companies with plain SQL. Point it at a set of public companies, run one command, and it ingests their numerical financial-statement Facts into a ClickHouse star schema; run that command again any day and it catches the store up to today, picking up new Filings and Restatements on its own. Freshness is deliberately pull-based — the store is as current as your last Catch-up — so you run one right before a screen and the data is freshest exactly when it matters.

The problem it removes is real: raw XBRL tags the same economic concept differently across companies (`us-gaap:Revenues` vs. `RevenueFromContractWithCustomerExcludingAssessedTax` vs. custom extensions), and prior-period numbers get silently restated over time. fin-tin normalizes concepts through a maintained Taxonomy and always resolves to the Latest-filed-wins (post-revision) value, so a cross-sectional screen — "every company whose revenue exceeded $1B in 2023 Q1" — just works, without wrestling tag chaos or trusting a stale number.

The design rests on a single principle: **never maintain a second copy of state that can drift from reality — derive it from the database and EDGAR, which already hold the truth.** Everything downstream (a stateless Reconciler, universal crash-resumability, "catch up to today" as the only ingestion behavior, and a strict engine/trigger separation) is a corollary. The result is a tool with almost no maintained mutable state and correspondingly few ways to corrupt itself.

## 2. Target User

### 2.1 Jobs To Be Done

- **Primary — cross-sectional screening.** Compare a financial concept (revenue, margin, debt, etc.) across a broad set of companies for a given period. Breadth-first: many companies, few concepts per query.
- **Secondary — feed derived metrics.** Compute margins, ratios, and other derived measures on top of the normalized statement Facts.
- **Operational — keep a trustworthy local corpus current on demand,** without babysitting a pipeline, re-fetching everything, or risking an EDGAR ban.
- **Builder's job — "this is for me."** A personal analysis instrument; success is whether kboss actually relies on it, not adoption.

### 2.2 Non-Users (v1)

Not for multi-user or hosted/shared use; not for non-technical users (the query surface is raw SQL); not for anyone needing footnote/narrative disclosures, custom company-specific line items, or non-US/IFRS filers in v1.

### 2.3 Key User Journey

Single operator role, so one lightweight journey (no multi-persona narrative warranted):

- **UJ-1. kboss screens the market before running an analysis.** Before a screening session, kboss runs the Catch-up command; it brings the local store current with EDGAR (new Filings + any Restatements) and reports what changed. kboss then connects a SQL client to ClickHouse and runs a cross-sectional screen against the Screening Mart, trusting every value is the latest-filed. Value lands when the query returns a ranked, normalized result set across the configured Universe with no manual data wrangling.

## 3. Glossary

*Downstream workflows and readers must use these terms exactly; no synonyms elsewhere in the PRD.*

- **Filing** — A single accepted EDGAR submission (e.g. a 10-K or 10-Q), permanent and immutable, identified by an Accession.
- **Accession** — The unique immutable identifier of a Filing. The unit of ingestion and the anchor for provenance and corruption detection.
- **Fact** — One numeric financial-statement value as reported in a Filing, with its Concept, unit, and reporting period.
- **Concept** — A financial-statement line item. A **Raw Concept** is the as-tagged XBRL element; a **Canonical Concept** is its normalized form under the Taxonomy.
- **Taxonomy** — The edgartools standardization mapping from Raw Concepts to Canonical Concepts. Versioned; the active version is recorded per Fact.
- **Standard-taxonomy Fact** — A Fact tagged with a standard taxonomy the tool ingests: `us-gaap`, `dei`, and `srt`. (`ifrs-full` / foreign filers are deferred — §5.) Custom company-extension concepts are out of scope (§5).
- **Raw Fact Landing (Tier 0)** — The immutable local store of ingested Standard-taxonomy Facts, keyed by Accession; a local mirror of EDGAR's numbers.
- **Canonical Store (Tier 1)** — Facts mapped to Canonical Concepts, keyed by Raw-Fact Identity, rebuildable from Tier 0 with no network access.
- **Raw-Fact Identity** — The key of a Tier 1 row: Accession + Raw Concept + period + unit. Canonical Concept and Taxonomy version are attributes, not part of the key.
- **Restatement** — A revised value for a prior period, arriving as a *new* Filing (a `/A` amendment or restated comparatives in a later Filing) — never an edit to an existing Accession.
- **Latest-filed-wins** — The resolution rule: for a (Canonical Concept, unit, period), the queryable value is the one from the most-recently-filed Filing.
- **Catch-up** — The single ingestion operation: bring the store current with EDGAR by ingesting everything filed since the High-water mark.
- **Reconciler** — The stateless engine that computes outstanding work as the gap between EDGAR's indexes and the store's contents, then performs a Catch-up.
- **High-water mark** — The most recent filed date already in the store, obtained by querying the store (not a maintained cursor).
- **Backfill** — Initial ingestion of full available history for the configured Universe.
- **Universe** — The configured set of companies in scope, supplied as configuration. v1 is seeded with the **S&P 500 constituents**; the Universe can be widened up to the full US `us-gaap` filer population by configuration alone (FR-7, §6.2).
- **Screening Mart** — The wide, one-row-per-company-period materialized view used for cross-sectional SQL screening.
- **Single-flight** — The guarantee that at most one ingestion run executes at a time, enforced by a self-expiring **Lease**.
- **EDGAR Client** — The single centralized, rate-limited, identified HTTP client through which all EDGAR access flows.

## 4. Features

### 4.1 Compliant EDGAR Access

**Description:** Every byte fetched from EDGAR flows through one EDGAR Client that makes ban-avoidance structural rather than a tuned afterthought. It enforces the published fair-access rate and a mandatory identifying User-Agent, self-imposes a cool-down when throttled, and minimizes the *number* of requests by preferring bulk and index sources over per-Filing crawling. Throttling lives here in the engine, never in the trigger, so no caller and no amount of re-triggering can cause a ban.

**Functional Requirements:**

#### FR-1: Centralized rate-limited client
All EDGAR access is funneled through a single EDGAR Client that enforces EDGAR's published fair-access policy: a configurable request-rate ceiling defaulting to the SEC maximum of **10 requests/second** (an aggregate per requester across all EDGAR hosts, regardless of machine count) and a mandatory declared, identifying User-Agent. Because the SEC does not document a rate-limit status code or `Retry-After`, the client detects throttling by request failure and self-imposes a cool-down.

**Consequences (testable):**
- No code path issues an EDGAR request outside the client (verified by construction/tests).
- Every request carries a declared, identifying User-Agent of the form `Company/App Name contact-email` plus `Accept-Encoding: gzip, deflate`; a blank/undeclared UA (which EDGAR rejects as an "Undeclared Automated Tool") is never sent.
- The rate ceiling is a single configurable value; its default is 10 req/s (the SEC published maximum) and it may be set lower for safety margin.
- On a detected block, the client backs off for at least the SEC-documented 10-minute cool-down before resuming; a `Retry-After` header is honored if present, but the client does not depend on one.

#### FR-2: Request-count minimization
The system prefers the lowest-request-count source for each need: index/feed for discovery, bulk artifacts for large-scale content, per-Filing API only for the incremental tail.

**Consequences (testable):**
- A full-Universe Backfill does not issue one API call per company when a single bulk artifact covers them (FR-7).
- Discovery of new Filings reads an index rather than crawling per-company pages.

### 4.2 Two-Tier Fact Store

**Description:** Facts land in an immutable Raw Fact Landing (Tier 0) keyed by Accession — a local mirror of EDGAR's numbers — and are mapped into the Canonical Store (Tier 1) for querying. Tier 1 is fully rebuildable from Tier 0 without touching the network, and Tier 0 is recoverable from EDGAR at throttled cost, so no layer is a lossy dead-end. Provenance travels with every Fact, which is what makes re-derivation and corruption recovery possible.

**Functional Requirements:**

#### FR-3: Immutable raw fact landing (Tier 0)
The system ingests Standard-taxonomy Facts for in-scope companies into Tier 0, keyed by Accession, retaining all such Facts (not only those currently mappable) plus provenance: Raw Concept, label, unit, period, filed date, and a content hash.

**Consequences (testable):**
- No normal operation edits a stored Fact in place; the only write to an existing Accession's Facts is a recovery re-fetch that restores the authoritative EDGAR bytes (FR-6).
- Custom company-extension Concepts are excluded (§5); only `us-gaap`/`dei`/`srt` enter.
- Provenance columns are populated for every stored Fact.

#### FR-4: Rebuildable canonical store (Tier 1)
The system maps Tier 0 Facts to Canonical Concepts via the Taxonomy into Tier 1, keyed by Raw-Fact Identity, with Canonical Concept and Taxonomy version as attributes; Tier 1 is rebuildable from Tier 0 with zero EDGAR calls.

**Consequences (testable):**
- Rebuilding Tier 1 issues no network requests.
- Re-mapping an already-mapped Fact overwrites in place (same key) rather than creating a duplicate row.
- Each Tier 1 Fact records the Taxonomy version that produced it.

#### FR-5: Latest-filed-wins resolution
For any (Canonical Concept, unit, period) the queryable value is the one from the most-recently-filed Filing; all filed versions are retained and distinguishable.

**Consequences (testable):**
- Querying a period known to have been restated returns the post-revision value.
- Both original and restated values remain retrievable, distinguished by Accession and filed date.
- Resolution groups on actual reporting-period dates, not fiscal-period labels.

#### FR-6: Tier 0 recovery from EDGAR
Because EDGAR is the sole source of truth, Tier 0 content for any Accession or company can be re-fetched from EDGAR and rebuilt, repairing local corruption or loss.

**Consequences (testable):**
- Re-ingesting an Accession restores its Tier 0 content from EDGAR via idempotent upsert, superseding a corrupted or lost prior copy.
- Recovery uses the same throttled ingestion path (FR-1) and requires no separate tooling in v1 — it is an ingestion run scoped to the target Accession/company.
- Detecting *which* data is corrupt is out of v1 scope: automated integrity checks (at-rest hash, proactive scrub) are deferred (§6.2 Should) and ad-hoc reactive repair is excluded (§5).

### 4.3 Backfill & Catch-up

**Description:** One behavior, two entry points. Backfill loads full available history for the Universe; Catch-up brings the store current with everything filed since its High-water mark. Both are the same Reconciler computing outstanding work as the gap between EDGAR's indexes and the store's own contents — no maintained cursor, no separate progress ledger. Because a Restatement arrives as a newly-filed Accession, Catch-up captures revisions automatically.

**Functional Requirements:**

#### FR-7: Configured-Universe backfill
The system backfills full available history for the configured Universe. The default path is the SEC bulk `companyfacts.zip` artifact (one download for the whole corpus); a small curated Universe may instead use per-company retrieval.

**Consequences (testable):**
- After Backfill, every in-scope company with available Standard-taxonomy Facts is present in Tier 0.
- Backfill is resumable (FR-10): interrupting and restarting does not re-ingest already-committed companies.
- Widening the Universe — up to the full `us-gaap` market — is a configuration change requiring no schema or pipeline redesign; the backfill selects the request-minimizing strategy appropriate to Universe size (per-company for a small subset, bulk artifact for large/full).

#### FR-8: Catch-up to today
A single command brings the store current by ingesting everything filed since the High-water mark. Realizes UJ-1.

**Consequences (testable):**
- Outstanding work is computed from `max(filed)` + Accession membership in the store versus EDGAR's index; no cursor file is read or written.
- A period restated by a Filing newer than the High-water mark is updated without any special "restatement mode."
- Re-running Catch-up with nothing new filed ingests nothing and reports `NOTHING_TO_DO`.

#### FR-9: DB-derived work list
The Reconciler derives all outstanding work from the store's contents and EDGAR's indexes; it maintains no separate progress state.

**Consequences (testable):**
- Deleting and rebuilding any derived cache (e.g. an Accession-membership projection) does not lose ingestion progress.
- The High-water mark is obtained by querying the store.

### 4.4 Resumability & Concurrency

**Description:** Every long operation must survive a hard kill and a re-trigger without corruption or lost progress, and concurrent triggers must never double the EDGAR request rate. This is achieved with idempotent incremental commits plus the DB-derived work list, and a Single-flight guarantee backed by a self-expiring Lease so a crashed run cannot deadlock all future runs.

**Functional Requirements:**

#### FR-10: Universal resumability
Every long operation (Backfill, Catch-up, and any future re-map/scrub) survives crash and pause via idempotent incremental commits and the DB-derived work list — no checkpoint file.

**Consequences (testable):**
- Killing a run mid-operation and restarting resumes from the last committed unit (per-company).
- Re-running any operation is idempotent: it neither duplicates data nor redoes committed work.

#### FR-11: Single-flight with self-expiring lease
At most one ingestion run executes at a time. A trigger arriving during an active run returns a graceful status, not an error; the Lease self-expires so a crashed run does not permanently block future runs.

**Consequences (testable):**
- A second trigger during an active run returns `ALREADY_RUNNING` with a success (exit-0) status; it does not start a second run or issue EDGAR requests.
- The run exposes a status vocabulary: `STARTED` / `ALREADY_RUNNING` / `NOTHING_TO_DO` / `COMPLETED`.
- After a crash that leaves a Lease held, a later trigger observes the expired Lease, reclaims it, and resumes; the store is not permanently deadlocked.

#### FR-12: Pure engine, decoupled trigger
The Catch-up engine is a pure command with no knowledge of its caller; v1 exposes a manual CLI trigger, with other triggers (cron/HTTP/feed) as future wrappers over the same command.

**Consequences (testable):**
- The engine behaves identically whether invoked from a shell, a script, or (future) a scheduler.
- Throttle (FR-1) and Single-flight (FR-11) are enforced inside the engine, so any trigger inherits them.

### 4.5 Screening Query Surface

**Description:** The queryable product is a wide Screening Mart — a ClickHouse materialized view that pivots the long Canonical Facts into one comparable row per company-period — queried directly with SQL. There is no bespoke UI or query language in v1; the database *is* the tool.

**Functional Requirements:**

#### FR-13: Wide screening mart
The system maintains a Screening Mart pivoting Canonical Facts into one row per company-period, queryable with raw SQL, reflecting Latest-filed-wins.

**Consequences (testable):**
- A user can express a cross-sectional screen (e.g. "revenue > $1B for 2023 Q1 across the Universe") as a single SQL query.
- Values in the mart reflect Latest-filed-wins (FR-5).
- The mart reflects newly-ingested Facts after a Catch-up (refreshed automatically/incrementally on ingest).

### 4.6 Coverage & Status Visibility

**Description:** Because coverage completeness is an acceptance signal, the tool must make "what's in vs. what's missing" observable — not silently partial.

**Functional Requirements:**

#### FR-14: Coverage & status report
The user can see ingestion coverage and currency for the configured Universe.

**Consequences (testable):**
- A status query/report shows: count of in-scope companies present, the store's High-water mark (latest filed date), and any in-scope companies with zero Facts.
- After a Catch-up, the report reflects the run's outcome (companies/Filings added).

## 5. Non-Goals (Explicit)

- **Not a hosted or multi-user service.** Single local operator only.
- **Not a UI or a custom query language.** SQL against ClickHouse is the surface.
- **Not a full-text / footnote / narrative store.** Numeric Standard-taxonomy Facts only.
- **Not ingesting custom company-extension Concepts** — accepted data-completeness cost for a lean, consistent corpus.
- **Not covering foreign / IFRS filers in v1.** US-domestic `us-gaap` only.
- **Not doing ad-hoc reactive corruption repair** ("this number looks wrong") — the one operation the Reconciler cannot derive from the DB; intentionally deferred.
- **Not becoming a general EDGAR mirror.** It stores what screening needs, not filings wholesale.

## 6. MVP Scope

### 6.1 In Scope
The Must-have capabilities of §4 (FR-1–FR-14): compliant centralized EDGAR access; the two-tier store with provenance, Latest-filed-wins, and Tier 0 recovery; Backfill + Catch-up Reconciler with universal resumability and single-flight; the SQL Screening Mart; and coverage/status visibility — over the S&P 500 Universe, `us-gaap` only.

### 6.2 Out of Scope for MVP
- **Should (fast-follow):** proactive integrity scrub (`last_verified_at`) + at-rest content-hash verification (automated corruption *detection*, building on FR-6); derived-metrics layer (margins/ratios) — `[NOTE FOR PM]` the derived-metrics job is a *stated secondary goal* (§2.1); revisit early if timeline permits; cron wrapper around the manual trigger.
- **Could (later):** point-in-time / backtesting surface; RSS-feed trigger; 8-K Item 4.02 restatement early-warning feed; re-map (Taxonomy vX→vY) as a first-class command; HTTP/button trigger and "catch up before query"; **widening the Universe from the S&P 500 seed toward the full `us-gaap` market** — a configuration change by design (FR-7, §8), not a re-architecture.
- **Won't (this time):** see §5 Non-Goals (custom company-extension Concepts; foreign/IFRS filers; ad-hoc reactive corruption repair; UI / multiple surfaces).

## 7. Success Metrics

**Primary**
- **SM-1 — Restatement correctness.** On a fixed test set of periods known to have been restated, the store returns the latest-filed value in 100% of cases. Validates FR-5, FR-8.
- **SM-2 — Coverage completeness.** Every S&P 500 constituent is either successfully ingested or listed as an explained gap in the status report (no silent omissions). Validates FR-7, FR-14.

**Secondary**
- **SM-3 — Currency after catch-up.** Immediately after a Catch-up, no Filing for the Universe with a filed date at or before the run's start remains un-ingested. Validates FR-8, FR-9.
- **SM-4 — Adoption / builder value.** kboss runs a screen against the store at least `[ASSUMPTION: weekly]` and continues past the first month. Validates the product overall.

**Counter-metrics (do not optimize)**
- **SM-C1 — Request throughput.** Do *not* maximize ingestion speed/throughput at the expense of rate-limit compliance; a ban is catastrophic and fewer/slower requests are always preferred. Counterbalances SM-3.
- **SM-C2 — Universe breadth.** Do *not* grow the Universe at the expense of correctness or completeness; a broad-but-wrong store is worse than a narrow-but-trustworthy one. Counterbalances SM-2.

## 8. Cross-Cutting NFRs

- **Reliability / crash-safety.** Any operation is safe to interrupt at any moment and re-run, with no partial write corrupting the store and no progress lost (FR-10).
- **Universe-agnostic scalability.** Nothing in the storage schema or ingestion pipeline assumes a particular Universe size; growing from the S&P 500 seed toward the full `us-gaap` market is a configuration change (FR-7), not a redesign.
- **Performance.** `[ASSUMPTION]` A curated-Universe Backfill completes within a single unattended session on a developer laptop; screens over the Screening Mart return interactively (single-digit seconds). Concrete budgets deferred to architecture.
- **Observability.** Runs emit progress and a final status (the FR-11 status vocabulary); coverage and gaps are queryable (FR-14).
- **Portability / deployment.** Runs locally on a single node (macOS) with ClickHouse in a container and a Python CLI; no cloud dependency.
- **Cost.** External cost is $0 (EDGAR is free); local disk footprint scales with the Universe.

## 9. Constraints & Guardrails

- **EDGAR fair-access compliance (ban-critical).** The tool must obey EDGAR's published policy (verified 2026-07-23 against the SEC Internet Security Policy and Accessing EDGAR Data pages): **10 requests/second** max per requester across all hosts, a **mandatory declared identifying User-Agent**, and a **10-minute cool-down** on breach (no documented status code or `Retry-After`). Enforced centrally per FR-1 and minimized structurally per FR-2; throttle and single-flight live in the engine, not the trigger.
- **Restatement correctness.** Original and restated values are both retained and never clobbered; only true corruption is overwritten (FR-5, FR-6).
- **Legal / ToS.** Respect EDGAR's terms of use; the identifying User-Agent carries real contact information.
- **Taxonomy coupling (standing dependency).** Outsourcing normalization to the edgartools Taxonomy couples the corpus to that library's mapping choices and versions; the risk is mitigated — not eliminated — by per-Fact Taxonomy-version provenance (FR-4) and the deferred re-map capability (§6.2).

## 10. Open Questions

1. **S&P 500 constituent list sourcing** — v1 Universe = S&P 500 (confirmed); what remains open is how the constituent list is sourced and refreshed as membership changes over time; deferred to architecture.
2. **Subset backfill strategy** — bulk-artifact-and-filter vs. per-company API for the S&P 500 subset specifically (FR-2, FR-7); deferred to architecture.
3. **Re-run-requested follow-up flag** — deferred; v1 coalesces triggers by pure-drop (FR-11). Revisit if mid-run Filings matter.

## 11. Assumptions Index

- §4.1 FR-2 — Bulk-artifact path for large/full Universe; per-company calls acceptable for small subsets (strategy deferred, §10.2).
- §7 SM-4 — "Regular use" ≈ weekly.
- §8 NFR — Subset Backfill fits one unattended session; interactive (single-digit-second) screen latency (concrete budgets deferred to architecture).

*Confirmed — no longer assumptions:* EDGAR rate limit = 10 req/s + mandatory declared User-Agent (FR-1, verified); v1 Universe = S&P 500 seed, widenable by configuration (FR-7); commit granularity = per-company (FR-10); Screening Mart auto-refreshes on ingest (FR-13); local single-node macOS deployment with containerized ClickHouse + Python CLI (§8 NFR).
