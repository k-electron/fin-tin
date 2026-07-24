# Architecture Decision Brief — Local EDGAR Financial-Statement Query Store

**Topic:** Local query tool for public-company financial statements, ingested from SEC EDGAR into a ClickHouse-backed store via a resumable, throttled pipeline.
**Date:** 2026-07-23
**Status:** v1 scope accepted (see MoSCoW). This brief captures the technical decisions and the reasoning behind them so an engineer — or a downstream `bmad-architecture` step — can build directly from it.

---

## 1. Overview

Build self-maintaining local data infrastructure to query financial statements **across** public companies. The primary job is **cross-sectional screening** — breadth across companies at a point/period — not deep single-company analysis. That job dictates the whole design: it is why we need a canonical concept layer (raw XBRL tags are not comparable across companies) and a wide comparable output shape (one row per company-period).

Two moving parts:

- A **local ClickHouse-backed store** of EDGAR numerical facts, structured so that messy per-company XBRL becomes comparable rows for screening, and so derived metrics (margins, ratios) sit on top.
- A **resumable, throttled ingestion pipeline** that keeps the store current from EDGAR with no hand-maintained bookkeeping.

---

## 2. Guiding Principle (the through-line)

> **Never maintain a second copy of state that can disagree with reality. Derive state from the DB + EDGAR, which already hold the truth.**

This is the single user instinct the entire architecture is a corollary of. Every property below descends from it:

- The **stateless reconciler** (work is derived, never tracked).
- **Universal resumability** (the DB is its own checkpoint; no separate progress file to drift).
- **Catch-up-to-today** as the only behavior.
- **Latest-filed-wins** resolution (truth is recomputed from what's filed, not stamped once).
- The **pure-command engine / dumb-trigger** split.

The rule has exactly one deliberate exception (ad-hoc reactive corruption repair, §7) — called out precisely because it is the one place where the principle cannot hold, and we isolate it rather than let it contaminate the rest.

---

## 3. Data Model

### 3.1 Two-tier store

| Tier | Contents | Properties | Rebuild source |
|------|----------|-----------|----------------|
| **Tier 0 — raw fact landing** | Local numeric mirror of EDGAR. Hoards **all standard-taxonomy numerical facts** (us-gaap / ifrs-full / dei / srt), keyed by accession. | Immutable, append-only. | Re-fetch from EDGAR (throttled cost). |
| **Tier 1 — canonical mapped facts** | Facts mapped onto the canonical concept dimension. Provenance columns live here. | Fully rebuildable from Tier 0 with **zero network**. | Rebuild from Tier 0 (free). |

**Recovery hierarchy: EDGAR (truth) → Tier 0 (raw mirror) → Tier 1 (canonical).** Each level is re-derivable from the level above. Tier 1 rebuilds from Tier 0 for free; Tier 0 rebuilds from EDGAR at throttled cost.

**Why two tiers.** It dissolves the disposable-vs-durable tension. The DB is fully rebuildable, yet never needs to re-hit rate-limited EDGAR for routine work because Tier 0 is a local safety net. It also **decouples expensive ingestion (rate-limited EDGAR fetch, done once) from cheap local standardization (re-runnable infinitely)** — the concept mapping can be re-run any number of times against Tier 0 without touching the network.

**Why hoard everything in Tier 0 (not just currently-mappable facts).** The re-map payoff depends on retaining facts that *today's* taxonomy cannot map. If a future taxonomy version learns to map a tag we discarded, we'd need a bounded EDGAR re-fetch to recover it. Hoarding all numerical facts makes taxonomy upgrades permanently **network-free** — this is the concrete payoff realized in §6 (re-map work list derived from Tier 0).

**Tier 0 is a cache, not ground truth.** EDGAR is the only real source of truth. An explicit EDGAR **re-fetch/recovery capability is required regardless**, so we can recover from Tier 0 corruption that cannot be debugged locally.

**Known limit on the hoard.** The SEC `companyfacts` / `companyconcept` / `frames` APIs **exclude custom company-extension concepts** — they return only standard taxonomies (us-gaap / ifrs-full / dei / srt). So "hoard all numerical facts" has a boundary: custom line items would require raw per-filing XBRL, not the aggregated API. Custom extensions are intentionally **out of scope** for v1. Conveniently, `companyfacts` returns all facts across all filings with no dedup, making it the ideal Tier 0 source.

### 3.2 Fact grain

**Long/narrow: one row per (fact, company, period).** Chosen over one-row-per-company-period because the long shape gives fidelity and makes incremental append trivial. The wide comparable shape is derived downstream (§3.4).

### 3.3 Tier 1 key — the critical modeling decision

**Tier 1 is keyed by RAW-FACT IDENTITY:** `accession + raw_tag + period + unit`.

The canonical concept, `taxonomy_version`, raw label, `filed_date`, and `content_hash` are **attributes**, not part of the key.

**Why (non-obvious).** If Tier 1 were keyed by the *canonical concept*, then re-mapping — which by definition changes the canonical concept assigned to a raw fact — would write a new row under the new concept and **orphan the stale vX row**. That is not idempotent and generates duplicates. Keying by raw-fact identity means a re-map is a **true in-place upsert**: same key, updated concept/version attributes, no orphaned stale rows. The raw-fact key doubles as the natural provenance key.

### 3.4 Concept dimension = edgartools standardization taxonomy

Adopt the **edgartools standardization taxonomy** as the canonical concept layer. This single decision **fuses three problems into one**:

1. **Concept dimension** — its standardized concept set *is* the dimension for screening.
2. **"Core concepts" noise filter** — unmappable tags simply never enter Tier 1.
3. **Cross-company normalizer** — `us-gaap:Revenues` vs `RevenueFromContractWithCustomer…` vs custom extensions all collapse to one canonical concept, which is what makes cross-company screening possible at all.

**Standard taxonomies only** (see the hoard limit above).

**Risk — coupling + drift** (see §9). Outsourcing the taxonomy means custom/uncommon tags fall through and mappings change across edgartools versions. **Mitigation:** store provenance on every fact row (raw XBRL tag + raw label + edgartools/`taxonomy_version`), which enables version-targeted re-mapping ("update values mapped by version X using version Y") **without re-fetching from EDGAR**.

### 3.5 Wide derived screening mart

A **ClickHouse materialized view** pivots the long facts into **one comparable row per company-period**. Derived metrics (margins, ratios) are **computed columns** in this layer. This is the reconciliation of the long base table (fidelity + easy incremental append) with the wide shape screening actually needs.

### 3.6 Restatements & resolution

Grounded in research against SEC docs, the live API, and edgartools source:

- **EDGAR filings are immutable** — never edited in place. (The only exceptions are rare staff-authorized PAC deletions for wrong-filer / duplicate / unreadable / sensitive filings.)
- **Revisions are NEW filings**, each with a new accession: either `/A` amendments (10-K/A, etc.) or — more commonly — **restated prior-period figures appearing as comparatives inside a later regular filing** (a 2024 10-K carrying restated 2023 numbers). So the same `(company, concept, period)` legitimately appears in **many** filings with possibly-different values. (8-K Item 4.02 is a restatement *signal*; corrected numbers arrive later.)
- **Keep all filed versions.** No field flags "restatement" — it is inferred from value divergence across filed dates.
- **Resolution rule = latest-filed-wins:** group facts by `(taxonomy+tag, unit, period_start, period_end)` and take `argmax(filed_date)`; tiebreak prefer `/A`. **Group on ACTUAL period dates, not fiscal `fy`/`fp` labels**, because 52/53-week fiscal drift makes labels unreliable.
- Because accessions are immutable, **corruption vs restatement becomes crisp**: a within-accession byte change (refetched bytes ≠ stored hash) is **corruption** → overwrite; a different value under a **new** accession is a **restatement** → keep both. So `content_hash` is purely a corruption detector; restatement history falls out of keying by accession + `filed_date` — no clobber-detection needed.

edgartools already does newest-filing-wins by default and offers `to_dataframe(pit_mode=True)` preserving all fact versions with `filing_date` / `form_type` — directly satisfying both the latest-correct requirement and full restatement history.

---

## 4. Ingestion Pipeline — the Stateless Reconciler

**The work list is DERIVED from the DB, never maintained.**

- Scoping high-water mark = `SELECT max(filed) FROM facts` — a query, not a stored cursor.
- The accession set is a projection of the fact table, always reconstructable. Nothing to hand-maintain, nothing to drift. The DB answers both "what I have" (query itself) and, combined with EDGAR's index, "what exists."

**"Catch up to today" is the only behavior the reconciler has.** Work = filings since `max(filed)` up to today, regardless of when or why it was invoked.

**The gap is bounded by TIME, and that is what makes it cheap.** EDGAR pre-partitions its indexes by day/quarter (daily-index + full-index files). A daily delta is a small index slice of newly-accepted filings minus what's already in the DB, resolved via indexed accession-membership lookup (columnar DBs are fast at this). edgartools reads these indexes (`get_filings(filing_date=…)`), so **discovery never hits individual filing pages**.

**Time-bounded delta is both cheap AND restatement-complete.** Restatements arrive as newly-filed accessions, so "process everything filed since `max(filed)`" captures them automatically. The reconciler and the restatement handling snap together with no extra machinery.

**Backfill is the only expensive diff** → handled as a one-time offline bootstrap via the **bulk `companyfacts.zip`**, never a crawl. Backfill splits into two sub-phases that **resume differently**:

- **ACQUIRE** — download the bulk zip; resume via HTTP range-request or idempotent re-fetch.
- **LOAD** — parse + upsert per company; resume via the reconciler gap.

Don't force one resume strategy across both.

**Two-layer fetch (prefer the most bulk/pushed source per layer):**

| Layer | Source | When per-filing pull is used |
|-------|--------|------------------------------|
| **Discovery** | Index files + RSS feed (already feed-based) | never — read the index |
| **Content** | Bulk `companyfacts.zip` (one download for the whole corpus) | only the **incremental tail** — "a filing landed today and I want it now" via the per-filing API |

---

## 5. Unified Resumability

**One mechanism — idempotent incremental commits + a DB-derived work list — covers daily delta, backfill, re-map (vX→vY), and proactive scrub.** Build it once.

- **Idempotent incremental commits** at **per-company grain** (the natural `companyfacts.zip` unit — one JSON per CIK). Commit granularity = how much you lose on a crash; per-company is fine-grained enough to bound rework while keeping overhead low. Never one big final write.
- Because commits are durable and incremental, **the DB is a continuously-valid partial checkpoint.** Resume-after-crash is just the same "derive the gap from DB" logic running again — **no separate checkpoint file** (which would be exactly the kind of driftable second copy the guiding principle forbids). There is no *mutable* state to maintain, but there *is* a durability discipline that makes the DB self-checkpointing.
- **Re-map** rides the same mechanism, given two fixes:
  1. Key Tier 1 by raw-fact identity (§3.3) so re-map is an idempotent in-place upsert, not a duplicate-generator.
  2. **Derive the re-map work list from Tier 0, not Tier 1** — "raw facts whose Tier 1 mapping is absent or `version < vY`" (a Tier0-anti-join-Tier1). Deriving from Tier 1 would miss facts vX couldn't map but vY can. This is the concrete payoff of hoarding raw facts.

**The mechanism boundary (crisp principle).** The reconciler works for **any operation whose remaining-work is a derivable difference between two cheaply-observable things** — EDGAR index vs DB, Tier 0 vs Tier 1, a timestamp column. It **breaks only when *detecting* the work is as expensive as *doing* it** — i.e. **ad-hoc reactive corruption repair** ("this number looks wrong"). That case cannot derive its work list from anything cheap, so it gets the one deliberately-separate path: an **external trigger + a small ephemeral suspect-accession queue** — the single bit of maintained state in the whole system, and the sole exception to the guiding principle. This is the edge of the mechanism, not a flaw in it.

---

## 6. Triggers, Concurrency, Throttle

### 6.1 Mechanism / policy separation

- **Engine** = a **pure, idempotent "catch up to today" command** with zero knowledge of its caller. Trivially testable and composable (invoked by hand, CI, script, or an app refresh button — all identical).
- **Triggers** are dumb invokers. **Policy (when / how often / who) lives entirely outside** the engine in swappable triggers.
- Cron is not architecture — it's one trigger (a one-line wrapper around the manual command). The **trigger source is a late-bound pluggable parameter**: manual CLI, on-app-open, cron, or EDGAR-RSS event ("new filing appeared → fire catch-up").
- Cadence is a **free variable**: trigger after 1 day or 3 weeks = same operation, bigger delta. There is **no "missed run" concept** and re-triggering is idempotent (empty delta → no-op). A long-gap catch-up degrades gracefully (bounded + resumable); a crash mid-catch-up resumes on the next trigger.
- Freshness flips **push → pull**: data is fresh when you ask, not always ~current. Elegant resolution for a query tool: **tie the trigger to use** — catch up right before running a screen, so data is freshest exactly when you care and idle otherwise (freshness-on-demand).

### 6.2 Concurrency — single-flight + coalescing

An open trigger means many askers can overlap a run. Idempotent upserts keep the *result* correct, but concurrent runs **double the EDGAR request rate = ban risk** (directly tied to the throttling goal). Therefore:

- **Single-flight guard** — an advisory lock/lease that is a **pure operational mutex** ("is a run active?"), NOT a domain checkpoint, so it cannot reintroduce drift.
- **Coalesce, don't queue.** A trigger arriving mid-run is mostly already satisfied (the running job catches up to today anyway) → drop it. (Optional deferrable refinement: a single "re-run requested" follow-up flag for the narrow case of a filing landing mid-run; the v1 "obvious" behavior is pure-drop.)
- **Success-not-failure status vocabulary**, all **exit-0**: `STARTED` / `ALREADY_RUNNING` / `NOTHING_TO_DO` / `COMPLETED`. Critical for cron — an overlapping trigger must **not** log-as-error, or real alarms get ignored. Also feeds the UI ("already refreshing"). Consistent with the mechanism/policy split: the engine emits structured outcomes, triggers interpret them. A trigger fired during an ongoing run returns `ALREADY_RUNNING` gracefully rather than erroring.
- **Self-expiring lease (TTL / heartbeat) is a must-have.** A naive lock held by a run that crashes = permanent "ongoing" that deadlocks all future triggers — silent, and on an ad-hoc tool could go unnoticed for weeks. A trigger seeing an **expired** lease treats it as not-running and proceeds; the resumable mechanism safely continues the crashed run's partial work. Crash + stale-lock + resume all resolve with one mechanism.

### 6.3 Throttle — placement and strategy

**Throttle and single-flight live IN THE ENGINE, not the trigger.** The engine self-limits the EDGAR rate regardless of who invokes it or how often — so triggers stay dumb and "mashing the button" cannot cause a ban. The engine is the responsible adult.

- **Don't guess the throttle — encode EDGAR's published fair-access policy:** an explicit rate ceiling, a **mandatory identifying User-Agent** (blank/spoofed UA is blocked), and honoring `429` / `Retry-After` with backoff.
- **Best throttle = minimize request COUNT, not tune the rate.** Bulk backfill + feed-based discovery + API-only tail means ban risk is **structural, not tuned**.
- **Centralize in ONE rate-limited client** every path funnels through; single-flight prevents concurrent rate-doubling.

---

## 7. Corruption Handling — three flavors

| Flavor | Detection | Fits the unified mechanism? |
|--------|-----------|------------------------------|
| **At-rest** (bitrot / truncation) | Recompute hash over stored bytes vs stored `content_hash` column | **Yes** — derivable |
| **Proactive scrub** | Add a `last_verified_at` column; work = "least-recently-verified N" | **Yes** — derivable, idempotent, resumable |
| **Ad-hoc reactive** ("a number looks wrong") | External trigger; work is **not** DB-derivable (detecting = as expensive as doing) | **No** — the deliberate exception: external trigger + small ephemeral suspect-accession queue (the one bit of maintained state) |

---

## 8. Bonus Capability — point-in-time queries

Keeping all filed versions (a `filed_date` per fact) yields **point-in-time queries** — "what was known about AAPL 2023 Q1 as of date X" — which enable **lookahead-bias-free backtesting**. edgartools `pit_mode` is purpose-built for this. Beyond original scope but nearly free (COULD-scope). Related idea: ingest 8-K Item 4.02 filings as a **restatement early-warning event stream** (flags companies whose prior financials are about to change before corrected numbers land).

---

## 9. Technology Choices

- **DB engine = ClickHouse** (chosen over StarRocks). The workload is **local, single-user, mostly-append columnar, one big flat fact table**. ClickHouse is simpler single-node, fast on this shape, and has **mature materialized-view support** for the screening mart. StarRocks is favored only for high-concurrency / JOIN-heavy profiles — not this one.
- **edgartools** for EDGAR reads — index/discovery (`get_filings`), bulk `companyfacts`, newest-filing-wins defaults, and `pit_mode` for full version history.

---

## 10. Key Risks / To-Verify

- **Verify EDGAR's exact published rate limit before hard-coding it.** Ban-critical: encode the current SEC fair-access notice's precise rate number, not a guessed one. Include the mandatory identifying User-Agent and `429`/`Retry-After` handling.
- **Coupling / drift from outsourcing the taxonomy** to edgartools (custom/uncommon tags fall through; mappings shift across versions). **Mitigated** by per-fact provenance (raw tag + raw label + `taxonomy_version`) plus the network-free re-map path — but it remains the standing external dependency to watch.

---

## Appendix — v1 Build Scope (MoSCoW, accepted)

**MUST:** two-tier store; long fact grain keyed by raw-fact identity + provenance cols; edgartools ingest of standard-taxonomy facts + its standardization as concept dimension; latest-filed-wins resolution; bulk `companyfacts.zip` backfill + stateless reconciler/catch-up delta; unified resumability (per-company idempotent commits); manual CLI trigger + single-flight with self-expiring lease; published-rate throttle + identifying User-Agent + one centralized client; wide screening mart/view.

**SHOULD:** proactive integrity scrub (`last_verified_at`) + at-rest hash check; derived-metrics layer (margins/ratios); cron wrapper.

**COULD:** point-in-time / backtesting surface (`pit_mode`); RSS-feed trigger; 8-K Item 4.02 early-warning feed; re-map (vX→vY) as a first-class command; HTTP/button trigger + "catch up before query".

**WON'T (this time):** custom company-extension concepts; ad-hoc reactive corruption repair; UI polish / multi-surface.
