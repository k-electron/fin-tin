# Intent: EDGAR Financials Query Tool

## Product

A locally-hosted query tool for public-company financial statements. It ingests numerical statement facts from SEC EDGAR into a ClickHouse star-schema database and lets a single user query financial statement concepts across many public companies. The store is self-maintaining: an ad-hoc "catch up to today" command pulls new and revised filings from EDGAR on demand, and the database serves as the query surface for screening and derived metrics. (The technical schema, two-tier store, and reconciler mechanics are covered in a separate architecture brief.)

## Problem & Job-to-Be-Done

The primary job is **cross-sectional screening** — comparing a financial concept (e.g. revenue, margins) across a broad set of companies for a given period. This is breadth-first: many companies, few concepts per query. The secondary job is **feeding derived metrics** (margins, ratios) computed on top of the statement facts.

Raw EDGAR XBRL makes this hard: the same economic concept is tagged inconsistently across companies (e.g. `us-gaap:Revenues` vs. `RevenueFromContractWithCustomer...` vs. custom extensions), and prior-period figures get restated over time. The user is hiring this tool to give them a normalized, always-queryable, always-current-enough local corpus so screening "just works" without wrestling with tag chaos or stale/superseded numbers.

## Core Intent / Guiding Principle

**Never maintain a second copy of state that can drift from reality; derive it from the DB + EDGAR, which already hold the truth.**

This single instinct is the spine of the whole design. Everything else is a corollary: a stateless reconciler that derives outstanding work from the gap between EDGAR's indexes and the DB's contents; universal resumability; "catch up to today" as the only ingestion behavior; latest-filed-wins correctness; and a pure command / dumb trigger split. There is deliberately almost no maintained mutable state — the DB is self-checkpointing, and the high-water mark of what's ingested is a query (`SELECT max(filed)`), not a hand-kept cursor.

## Key Product Decisions

- **edgartools standardization taxonomy = the concept dimension.** *Why:* one choice fuses three problems — it is the canonical concept dimension, the "core concepts" noise filter (unmappable tags simply don't enter), and the cross-company normalizer.
- **Keep only core numerical standard-taxonomy facts; custom extensions out of scope.** *Why:* the SEC companyfacts API already excludes company-extension concepts (us-gaap/ifrs-full/dei/srt only); accepting that boundary keeps ingestion simple and the corpus lean, at the cost of custom line items.
- **Correctness = latest-filed-wins.** *Why:* querying e.g. AAPL 2023 Q1 revenue must return the latest known correct (post-revision) value. Restatements arrive as new immutable filings (new accession, `/A` or restated comparatives in later filings), so grouping facts by concept/unit/actual-period and taking the most-recently-filed value yields the corrected number automatically.
- **On-demand / pull-based freshness.** *Why:* freshness flips from push to pull — data is fresh when you ask, not always current. For a query tool this is a feature: tie the trigger to use (catch up right before a screen) so data is freshest exactly when it matters and idle otherwise.
- **Provenance retained so data is re-derivable.** *Why:* keeping raw fact identity, source metadata, and filed dates makes the canonical layer rebuildable from the local raw mirror with zero network calls, and makes the raw mirror recoverable from EDGAR at throttled cost — nothing is a lossy dead-end.

## Scope (MoSCoW)

**Must**
- Two-tier store (raw mirror + canonical mapped facts)
- Long fact grain, keyed by raw-fact identity, with provenance columns
- edgartools ingest of standard-taxonomy facts, using its standardization as the concept dimension
- Latest-filed-wins resolution
- Bulk `companyfacts.zip` backfill + stateless reconciler / "catch up to today" delta
- Unified resumability via per-company idempotent incremental commits
- Manual CLI trigger with single-flight guard and a self-expiring lease
- Published-rate throttle + identifying User-Agent, funneled through one centralized rate-limited client
- Wide screening mart / materialized view

**Should**
- Proactive integrity scrub (`last_verified_at`) + at-rest content-hash check
- Derived-metrics layer (margins, ratios)
- Cron wrapper around the manual command

**Could**
- Point-in-time / backtesting surface (edgartools `pit_mode`)
- RSS-feed trigger
- 8-K Item 4.02 restatement early-warning feed
- Re-map (taxonomy vX -> vY) as a first-class command
- HTTP/button trigger and "catch up before query"

**Won't (this time)**
- Custom company-extension concepts
- Ad-hoc reactive corruption repair ("this number looks wrong")
- UI polish / multiple surfaces

## Constraints & Non-Negotiables

- **EDGAR rate-limit compliance (ban-critical).** Encode EDGAR's published fair-access policy — an explicit rate ceiling plus a mandatory identifying User-Agent (blank or spoofed UAs are blocked) — and honor 429/Retry-After with backoff. The structural throttle is to minimize request *count* (bulk backfill, feed-based discovery, per-filing pull only for the incremental tail). Throttle and single-flight live in the engine, not the trigger, so mashing the trigger cannot cause a ban.
- **Resumability / crash-safety.** Every long operation (daily delta, backfill, re-map, scrub) must survive crashes and pauses via idempotent incremental commits + a DB-derived work list — no separate checkpoint file. The single-flight lease must be self-expiring so a crash mid-run cannot permanently deadlock all future triggers.
- **Restatement correctness.** Original vs. restated values are a screening signal and must both be retained (keyed by accession + filed date); corruption (bytes changed under a fixed accession) is overwritten, restatements (new accession) are kept — never clobbered.

## Open Questions / To Verify

- **Verify EDGAR's exact published rate limit** against the current SEC fair-access notice before hard-coding it — this is ban-critical.
- **Re-run-requested follow-up flag** (for a filing landing mid-run) is a deferrable refinement; v1 coalesces triggers by pure-drop (a trigger during an active run returns "run ongoing", exit-0).
- **Ad-hoc reactive corruption repair** is the one case the unified reconciler does not cover (detecting the work is as expensive as doing it) and is intentionally deferred; if pursued later it needs a small ephemeral suspect-accession queue — the sole piece of genuinely maintained state.
