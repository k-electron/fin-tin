# Implementation Tasks — EDGAR Financials Query Tool (v1 Build Backlog)

Ordered, actionable backlog derived from the accepted MoSCoW v1 scope. Phases are sequenced so each one produces something runnable. Dependencies are noted inline.

**Guiding principle (from the session synthesis):** never maintain a second copy of state that can disagree with reality. All work lists are *derived* from the DB + EDGAR, which already hold the truth. Statelessness, resumability, catch-up-to-today, and latest-filed-wins all fall out of this.

**Recovery hierarchy:** EDGAR (ground truth) → Tier 0 (raw local mirror, rebuildable from EDGAR at throttled cost) → Tier 1 (canonical, rebuilds from Tier 0 with zero network) → screening mart (derived from Tier 1).

---

## ⚠️ To Verify Before Coding

- [ ] **Confirm EDGAR's exact published fair-access rate limit** against the current SEC fair-access notice before hard-coding it into the client. This is ban-critical — do not guess the number.

---

## Phase 1 — Foundation (rate-limited access + scaffolding)

- [ ] **Stand up ClickHouse locally** — single-node install for a local single-user, mostly-append columnar workload; confirms the engine chosen over StarRocks.
- [ ] **Scaffold the project** — repo layout, dependency management, config, and a CLI entrypoint skeleton the later trigger hangs off of.
- [ ] **Integrate edgartools** — pin a version; wire it in as the ingest + standardization library. _Depends on: scaffolding._
- [ ] **Build ONE centralized rate-limited EDGAR client** — every network path funnels through it; encodes the published fair-access ceiling, sends a mandatory identifying User-Agent (blank/spoofed is blocked), and honors 429/Retry-After with backoff. _Depends on: the to-verify item above._
- [ ] **Make request-count minimization structural in the client** — design so bulk/feed sources are preferred and only the incremental tail hits per-filing endpoints, so ban risk is structural not tuned. _Depends on: centralized client._

## Phase 2 — Schema (two-tier store + screening mart)

- [ ] **Create Tier 0 raw fact landing table** — immutable append-only local mirror of standard-taxonomy numerical facts (us-gaap / ifrs-full / dei / srt from the companyfacts API); keyed by accession; hoards ALL standard numerical facts; stores a `content_hash` per filing/fact for corruption detection. _Depends on: ClickHouse._
- [ ] **Create Tier 1 canonical fact table** — keyed by **raw-fact identity** (accession + raw_tag + period_start + period_end + unit) so a re-map upserts in place rather than orphaning stale rows; canonical concept, `taxonomy_version`, `filed_date`, and provenance (raw XBRL tag, raw label, edgartools version) live here as attributes. Rebuildable from Tier 0 with zero network. _Depends on: Tier 0 table._
- [ ] **Build the wide derived screening mart as a materialized view** — cross-sectional (one company-period per row) over Tier 1, with **latest-filed-wins** resolution: group facts by (taxonomy+tag, unit, period_start, period_end), take argmax(filed_date), tiebreak prefer `/A`. Group on actual period dates, not fy/fp labels. _Depends on: Tier 1 table._

## Phase 3 — Backfill (one-time bulk bootstrap, resumable)

- [ ] **Acquire bulk companyfacts.zip (ACQUIRE sub-phase)** — one download for the whole corpus instead of a crawl; resumable via HTTP range-request / idempotent re-fetch. _Depends on: centralized client._
- [ ] **Load per company into Tier 0 (LOAD sub-phase)** — parse each per-CIK JSON and upsert; commit in durable **idempotent per-company increments** (never one big final write) so the DB is a continuously-valid partial checkpoint. _Depends on: Tier 0 table, ACQUIRE._
- [ ] **Derive backfill resume from the DB, not a checkpoint file** — on restart, recompute the remaining work list from DB contents; resume-after-crash uses the same "derive the gap" logic as steady-state. _Depends on: per-company idempotent commits._

## Phase 4 — Reconciler / Catch-up (the unified resumable engine)

- [ ] **Build the stateless reconciler work-list derivation** — compute the gap as filings since `max(filed_date)` up to today, checked via indexed accession-membership lookup against EDGAR's day/quarter index files (read through edgartools `get_filings(filing_date=)`, so discovery never hits filing pages). High-water mark is DERIVED (`SELECT max(filed) FROM facts`), never maintained. _Depends on: Phase 2 schema, centralized client._
- [ ] **Implement idempotent upsert of the delta** — process everything filed since the high-water mark; because restatements arrive as newly-filed accessions, this captures restatements automatically and completely. _Depends on: work-list derivation._
- [ ] **Unify resumability via per-company incremental commits** — same mechanism the backfill uses; a crash mid-catch-up resumes cleanly on the next trigger. One mechanism covers daily delta + backfill. _Depends on: idempotent upsert._

## Phase 5 — Trigger + Concurrency (pure command, dumb triggers)

- [ ] **Expose a manual CLI trigger** — a pure, idempotent "catch up to today" entrypoint with zero knowledge of its caller (mechanism/policy separation). _Depends on: Phase 4 reconciler._
- [ ] **Add single-flight coalescing with a self-expiring lease (TTL/heartbeat)** — an advisory lock/lease so overlapping triggers can't double the EDGAR request rate (ban risk). Lease MUST self-expire: a trigger seeing an expired lease treats the run as not-running and proceeds, and the resumable mechanism continues the crashed run's partial work. A trigger arriving mid-run coalesces (pure-drop) rather than piling up. _Depends on: CLI trigger._
- [ ] **Define the status vocabulary as a success contract** — return `STARTED` / `ALREADY_RUNNING` / `NOTHING_TO_DO` / `COMPLETED`, all exit-0, so an overlapping trigger never logs as an error (critical for cron) and the engine emits structured outcomes for triggers/UI to interpret. _Depends on: single-flight lease._
- [ ] **Keep throttle + single-flight IN the engine** — triggers stay dumb; mashing the button cannot cause a ban. _Depends on: centralized client, single-flight lease._

## Phase 6 — Query Surface

- [ ] **Make the screening mart/view usable for cross-sectional queries** — verify the mart answers breadth-across-companies screening questions with latest-filed-wins values; this is the primary job-to-be-done. _Depends on: Phase 2 mart, at least one completed backfill/catch-up._

---

## Deferred Work (explicit scope boundary)

### Should (near-term next)
- Proactive integrity scrub: `last_verified_at` column + "least-recently-verified N" work list.
- At-rest hash check: recompute hash over stored bytes vs stored `content_hash` column (bitrot/truncation detection).
- Derived-metrics layer: margins / ratios as computed columns in the mart layer.
- Cron wrapper: a one-line wrapper calling the manual catch-up command.

### Could (opportunistic)
- Point-in-time / backtesting surface (edgartools `pit_mode`) for lookahead-bias-free "what was known as of date X" queries.
- RSS-feed trigger: "new filing appeared" fires catch-up (slots into the pluggable trigger parameter).
- 8-K Item 4.02 restatement early-warning event feed.
- Re-map (vX → vY) as a first-class command — work list derived as a Tier 0 anti-join Tier 1 (raw facts whose Tier 1 mapping is absent or version < vY), staying network-free.
- HTTP / button trigger + "catch up before running a screen" (freshness-on-demand).

### Won't (this time)
- **Custom company-extension concepts** — the companyfacts/companyconcept/frames APIs exclude them (standard taxonomies only); custom line items require raw per-filing XBRL and are intentionally out of scope.
- **Ad-hoc reactive corruption repair** ("this number looks wrong") — the one path the unified reconciler deliberately does NOT cover, because detecting the work is as expensive as doing it. Would need a small ephemeral suspect-accession queue driven by an external trigger — the single bit of maintained state. Left out of v1.
- **UI polish / multi-surface** presentation.
