# Spine ⇄ Inputs Reconciliation — fin-tin

**Date:** 2026-07-23
**Spine:** `../ARCHITECTURE-SPINE.md`
**Inputs:** PRD (`prd-fin-tin-2026-07-23/prd.md`), architecture brief (`brainstorm-edgar-financials-query-tool-2026-07-23/architecture-brief.md`), spine memlog (`../.memlog.md`)

## Method / coverage check

- **All 14 PRD FRs have a home** in the Capability→Architecture Map (FR-1..FR-14 each mapped to a component + governing AD). No FR is orphaned.
- **Guiding-principle single exception** (ad-hoc reactive repair): landed — AD-1 rule + Deferred (Won't-v1).
- **Two-layer discovery/content fetch** (brief §4): landed adequately — discovery-via-index in AD-10 (`get_filings(filing_date=)`), content in AD-4/AD-13. (Minor: FR-2's discovery-via-index consequence is governed by AD-10, but the map cites AD-3/AD-13 for FR-2 — a traceability nit, not a gap.)
- **Point-in-time bonus / 8-K 4.02** (brief §8): landed in Deferred (Could); substrate preserved (AD-7 keeps all filed versions).
- **Nothing material appears invented.** The Ports & Adapters paradigm, stack pins, and physical types (`cik` UInt32, `value` Float64, `Date` periods) all trace to the memlog / web-verification entries.
- **Not flagged (expected):** Should/Could/Won't items under Deferred; deep rationale living only in the memlog.

---

## Findings (most material first)

### 1. [MATERIAL] Screening Mart lost its WIDE / pivot shape — the reason the canonical layer exists
- **Inputs:** FR-13 requires the mart to *pivot* Canonical Facts into "**one row per company-period**," queryable so a cross-sectional screen is a single SQL statement; brief §3.4/§3.5 make the "wide comparable output shape (one row per company-period)" the whole point of the canonical dimension, with margins/ratios as **computed columns** on that wide row.
- **Spine (AD-8):** describes an `AggregatingMergeTree` MV storing `argMaxState(value, filed_date)` **per `(cik, canonical_concept, period_start, period_end, unit)`** — i.e. one row per company-**concept**-period (a LONG latest-filed-wins resolution table) — yet labels it "one comparable row per company-period."
- **Problem:** internally inconsistent (the key includes `canonical_concept`, so it is *not* one row per company-period) and it silently substitutes the long resolution view for the wide pivot FR-13 mandates. The deferred derived-metrics ("sit on the mart") assume the wide shape; on a long mart, ratio/margin columns don't compose the same way. Either a second wide construct is missing, or AD-8's "per company-period" claim is wrong. This is the mart's load-bearing shape decision.

### 2. [MEDIUM] Brief MUST "bulk `companyfacts.zip` backfill" deferred; v1 ships per-company API instead
- **Inputs:** brief Appendix MoSCoW lists **MUST: "bulk `companyfacts.zip` backfill"**, and §4 says backfill is "a one-time offline bootstrap via the bulk `companyfacts.zip`, never a crawl." PRD FR-7 opening: "The **default path is** the SEC bulk `companyfacts.zip` artifact."
- **Spine (AD-13):** inverts this — v1 implements the **per-company `companyfacts` API** strategy; the bulk `.zip` strategy is **Deferred**.
- **Assessment:** defensible as a resolution of PRD Open Question #2 (subset backfill strategy explicitly deferred to architecture) and the §11 assumption that per-company calls are acceptable for a small subset — and bulk stays pluggable behind AD-13, so no redesign. But it directly demotes a brief MUST-list item and contradicts FR-7's stated default. **Confirm the inversion was intended** (it materially changes the v1 backfill mechanism and its request-count profile for ~500 CIKs).

### 3. [MINOR] FR-1 quiet EDGAR-compliance details dropped
- **`Accept-Encoding: gzip, deflate`** header (PRD FR-1 testable consequence, required on every request) is absent from AD-3 and the conventions table.
- **Honor `Retry-After`/429 if present:** PRD FR-1 and brief §6.3 say detect-by-failure **and** honor `Retry-After`/`429` if present (just don't depend on one). AD-3 states only "no SEC status code / `Retry-After` is assumed," dropping the honor-if-present behavior.

### 4. [MINOR] Latest-filed-wins tiebreak "prefer `/A`" dropped
- **Brief §3.6:** resolution = `argmax(filed_date)`, **tiebreak prefer `/A`** (amendment).
- **Spine (AD-7/AD-8):** `argMax(value, filed_date)` with no tiebreak. ClickHouse `argMax` is nondeterministic on ties, so the stated correctness rule for a same-filed-date amendment vs. original is lost.

### 5. [MINOR] FR-14 "no silent omissions" / zero-fact-company gaps not surfaced as a requirement
- **Inputs:** SM-2 makes coverage completeness an *acceptance signal*; FR-14 requires the status report to list **in-scope companies present, the high-water mark, and any in-scope companies with zero Facts** (explained gaps, "not silently partial").
- **Spine:** FR-14 is mapped only to "cli + adapters/store, AD-10 (derived from DB)" and the memlog notes "coverage report is a CLI command." The specific "surface missing / zero-fact companies (no silent omissions)" requirement is not visible in any AD or the map. Arguably an acceptance criterion rather than an invariant, but it is a stated in-scope MUST behavior worth carrying forward.

---

## Noted but adequately covered (no action)
- **Taxonomy-coupling risk** (PRD §9, brief §9): the spine has no risk section, but the *mitigation* fully lands — per-fact `taxonomy_version` provenance (AD-9, AD-14, conventions) + deferred re-map (Deferred). The standing-dependency framing lives in the memlog; acceptable.
- **Tier 0 taxonomy scope:** brief §3.1 lists `ifrs-full` in the hoard; spine AD-9 restricts to `us-gaap/dei/srt`. Not a contradiction — the spine correctly follows the PRD's v1 scoping (IFRS/foreign filers deferred, Glossary + §5).
- **Backfill ACQUIRE/LOAD split with differing resume strategies** (brief §4): tied to the bulk-`.zip` path, which is deferred; not a v1 concern.
- **Long fact grain, hoard-everything, mechanism/policy split, self-expiring lease, coalesce-don't-queue, re-run-requested flag (Open Q#3):** all landed or correctly deferred.
