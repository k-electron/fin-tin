---
stepsCompleted:
  - step-01-document-discovery
  - step-02-prd-analysis
  - step-03-epic-coverage-validation
  - step-04-ux-alignment
  - step-05-epic-quality-review
  - step-06-final-assessment
readinessStatus: READY
openFindings: 0
remediationApplied: true
documentsIncluded:
  - prds/prd-fin-tin-2026-07-23/prd.md
  - architecture/architecture-fin-tin-2026-07-23/ARCHITECTURE-SPINE.md
  - architecture/architecture-fin-tin-2026-07-23/BUILD-SPLIT.md
  - epics.md
uxStatus: not-applicable-deferred
---

# Implementation Readiness Assessment Report

**Date:** 2026-07-23
**Project:** fin-tin

## 1. Document Inventory

| Type | Document(s) | Status |
|------|-------------|--------|
| PRD | `prds/prd-fin-tin-2026-07-23/prd.md` (24K) | ✅ Assessing |
| Architecture | `architecture/.../ARCHITECTURE-SPINE.md` (18K), `BUILD-SPLIT.md` (4.8K) | ✅ Assessing |
| Epics & Stories | `epics.md` (22K) | ✅ Assessing |
| UX Design | — | ⏭️ N/A — deferred; fin-tin is a data pipeline with no UI at this stage |

**Duplicates:** None detected (no whole-vs-sharded conflicts).
**Confirmed by:** kboss on 2026-07-23.

## 2. PRD Analysis

Source: `prds/prd-fin-tin-2026-07-23/prd.md` (status: final). Read in full.

### 2.1 Functional Requirements (14 total)

| ID | Title | Requirement (condensed to intent; full text + testable consequences in PRD §4) |
|----|-------|--------------------------------------------------------------------------------|
| FR-1 | Centralized rate-limited client | All EDGAR access funneled through one client enforcing fair-access: configurable rate ceiling (default 10 req/s aggregate per requester), mandatory identifying User-Agent, failure-detected throttling with ≥10-min self-imposed cool-down; `Retry-After` honored if present but not depended on. |
| FR-2 | Request-count minimization | Prefer lowest-request-count source per need: index/feed for discovery, bulk artifacts for large-scale content, per-Filing API only for the incremental tail. |
| FR-3 | Immutable raw fact landing (Tier 0) | Ingest Standard-taxonomy Facts (`us-gaap`/`dei`/`srt`) into Tier 0 keyed by Accession, retaining all such Facts + provenance (Raw Concept, label, unit, period, filed date, content hash). No in-place edits except recovery re-fetch (FR-6). |
| FR-4 | Rebuildable canonical store (Tier 1) | Map Tier 0 → Canonical Concepts via Taxonomy into Tier 1, keyed by Raw-Fact Identity, Canonical Concept + Taxonomy version as attributes; Tier 1 rebuildable from Tier 0 with zero EDGAR calls; re-map overwrites in place; each Tier 1 Fact records Taxonomy version. |
| FR-5 | Latest-filed-wins resolution | For any (Canonical Concept, unit, period) the queryable value is from the most-recently-filed Filing; all filed versions retained & distinguishable; groups on actual reporting-period dates, not fiscal labels. |
| FR-6 | Tier 0 recovery from EDGAR | Any Accession/company re-fetchable from EDGAR to repair corruption/loss via idempotent upsert on the same throttled ingestion path; no separate tooling in v1. (Corruption *detection* is deferred.) |
| FR-7 | Configured-Universe backfill | Backfill full available history for the configured Universe; default = SEC bulk `companyfacts.zip`; small curated Universe may use per-company; resumable (FR-10); widening Universe up to full `us-gaap` is config-only, no schema/pipeline redesign. |
| FR-8 | Catch-up to today | Single command brings store current by ingesting everything filed since High-water mark (realizes UJ-1); restatements captured without special mode; nothing-new run reports `NOTHING_TO_DO`. |
| FR-9 | DB-derived work list | Reconciler derives all outstanding work from store contents + EDGAR indexes; maintains no separate progress state; High-water mark obtained by querying the store; derived caches rebuildable without losing progress. |
| FR-10 | Universal resumability | Every long operation (Backfill, Catch-up, future re-map/scrub) survives crash/pause via idempotent incremental commits (per-company granularity) + DB-derived work list; no checkpoint file; re-runs idempotent. |
| FR-11 | Single-flight with self-expiring lease | At most one ingestion run at a time; second trigger returns `ALREADY_RUNNING` (exit-0) without issuing EDGAR requests; status vocabulary `STARTED`/`ALREADY_RUNNING`/`NOTHING_TO_DO`/`COMPLETED`; self-expiring Lease prevents permanent deadlock after crash. |
| FR-12 | Pure engine, decoupled trigger | Catch-up engine is a pure command with no caller knowledge; v1 exposes a manual CLI trigger; throttle (FR-1) + single-flight (FR-11) enforced inside engine so any trigger inherits them. |
| FR-13 | Wide screening mart | Maintain a Screening Mart (ClickHouse MV) pivoting Canonical Facts to one row per company-period, queryable with raw SQL, reflecting Latest-filed-wins, auto-refreshed on ingest. |
| FR-14 | Coverage & status report | User can see ingestion coverage + currency: count of in-scope companies present, store High-water mark, in-scope companies with zero Facts; reflects run outcome after Catch-up. |

### 2.2 Non-Functional Requirements (§8 — 6 categories)

| ID | Category | Requirement |
|----|----------|-------------|
| NFR-1 | Reliability / crash-safety | Any operation safe to interrupt at any moment and re-run — no partial-write corruption, no lost progress (ties FR-10). |
| NFR-2 | Universe-agnostic scalability | No storage schema or pipeline assumption about Universe size; S&P 500 → full `us-gaap` is a config change (FR-7), not a redesign. |
| NFR-3 | Performance `[ASSUMPTION]` | Curated-Universe Backfill completes in one unattended session on a dev laptop; screens return interactively (single-digit seconds). Concrete budgets deferred to architecture. |
| NFR-4 | Observability | Runs emit progress + final status (FR-11 vocabulary); coverage/gaps queryable (FR-14). |
| NFR-5 | Portability / deployment | Runs locally on single node (macOS), ClickHouse in a container, Python CLI; no cloud dependency. |
| NFR-6 | Cost | External cost $0 (EDGAR free); local disk footprint scales with Universe. |

### 2.3 Additional Requirements / Constraints & Guardrails (§9)

- **C-1 EDGAR fair-access compliance (ban-critical):** obey 10 req/s max per requester across hosts, mandatory declared identifying UA, 10-min cool-down on breach; central per FR-1, minimized per FR-2; throttle + single-flight in engine not trigger. (Verified 2026-07-23 vs SEC policy.)
- **C-2 Restatement correctness:** original + restated values both retained, never clobbered; only true corruption overwritten (FR-5, FR-6).
- **C-3 Legal / ToS:** respect EDGAR terms; identifying UA carries real contact info.
- **C-4 Taxonomy coupling (standing dependency):** outsourcing normalization to edgartools Taxonomy couples corpus to its mapping/versions; mitigated (not eliminated) by per-Fact version provenance (FR-4) + deferred re-map (§6.2).

### 2.4 Success Metrics (§7)

- **SM-1** Restatement correctness — 100% latest-filed on a fixed restated-period test set (validates FR-5, FR-8).
- **SM-2** Coverage completeness — every S&P 500 constituent ingested or listed as explained gap, no silent omissions (validates FR-7, FR-14).
- **SM-3** Currency after catch-up — no Filing filed ≤ run-start left un-ingested (validates FR-8, FR-9).
- **SM-4** Adoption/builder value — kboss screens ≥ weekly `[ASSUMPTION]` past first month.
- **Counter-metrics:** SM-C1 do not maximize throughput over rate-limit compliance; SM-C2 do not grow Universe over correctness/completeness.

### 2.5 PRD Completeness Assessment (initial)

**Strengths:**
- Every FR carries explicit **testable consequences** — traceability-friendly and acceptance-ready.
- Clear Glossary (§3) with a "use these terms exactly" mandate — strong anchor for epic/story language consistency.
- Explicit Non-Goals (§5) and tiered MVP scope (§6) — Must/Should/Could/Won't are unambiguous.
- Success metrics map directly back to FRs.

**Watch-items to carry into coverage validation:**
- **NFRs are categorical, not individually numbered** in the source; I've numbered them NFR-1–6 for traceability. Epics must still be checked for coverage of NFR-1 (crash-safety) and NFR-4 (observability) as testable acceptance, not just prose.
- **Performance (NFR-3) is `[ASSUMPTION]`** with budgets explicitly deferred to architecture — a known soft spot; confirm architecture pinned concrete budgets.
- **Open Questions (§10):** (1) S&P 500 constituent list sourcing/refresh, (2) subset backfill strategy (bulk-and-filter vs per-company), (3) re-run follow-up flag — all "deferred to architecture." Coverage validation must confirm these were actually resolved in the architecture, not left dangling.
- **Secondary goal drift risk:** derived-metrics layer is a *stated* secondary JTBD (§2.1) but sits in Should/fast-follow (§6.2) — legitimately out of MVP, but flagged so epics aren't expected to cover it.

## 3. Epic Coverage Validation

Source: `epics.md` (3 epics, 13 stories). Read in full. The epics doc carries an explicit **FR Coverage Map**, per-epic FR lists, and story-level acceptance criteria that cite FR/AD numbers — strong traceability signal.

### 3.1 FR Coverage Matrix

| FR | PRD Requirement (short) | Epic / Story | AC Evidence | Status |
|----|-------------------------|--------------|-------------|--------|
| FR-1 | Centralized rate-limited client | Epic 1 / Story 1.3 | 1.3 AC1 (identifying UA + ≤10 req/s), AC2 (cool-down / `Retry-After`), AC3 (no HTTP outside client) | ✅ Covered |
| FR-2 | Request-count minimization | Epic 1 / Story 1.3 (cited); Epic 2 / Story 2.2 (substantive) | 1.3 AC1 cites FR-2; index-based discovery in 2.2 AC1; **bulk-artifact minimization explicitly deferred** (2.3 AC4) | ⚠️ Covered–partial (see 3.3) |
| FR-3 | Immutable Tier 0 landing | Epic 1 / Story 1.4 | 1.4 AC1 (provenance + content_hash), AC2 (consolidated-only), AC3 (idempotent) | ✅ Covered |
| FR-4 | Rebuildable Tier 1 | Epic 1 / Story 1.5 | 1.5 AC1 (zero-network map), AC3 (in-place upsert) | ✅ Covered |
| FR-5 | Latest-filed-wins | Epic 1 / Story 1.6 | 1.6 AC1 (tiebreak), AC2 (**restatement fixture — product-defining test**) | ✅ Covered |
| FR-6 | Tier 0 recovery | Epic 3 / Story 3.3 | 3.3 AC1 (scoped re-ingest + re-derive), AC2 (thin reuse) | ✅ Covered |
| FR-7 | Configured-Universe backfill | Epic 2 / Story 2.3 | 2.3 AC1 (per-company strategy), AC4 (bulk switch deferred at interface) | ✅ Covered |
| FR-8 | Catch-up to today | Epic 3 / Story 3.1 | 3.1 AC1 (`STARTED`→`COMPLETED`), AC3 (`NOTHING_TO_DO`) | ✅ Covered |
| FR-9 | DB-derived work list | Epic 2 / Story 2.2 | 2.2 AC1 (membership over lookback, `max(filed)` hint only) | ✅ Covered |
| FR-10 | Universal resumability | Epic 2 / Story 2.3 (+2.2 mechanism) | 2.3 AC2 (resume, no checkpoint file) | ✅ Covered |
| FR-11 | Single-flight self-expiring lease | Epic 3 / Story 3.2 | 3.2 AC1 (`ALREADY_RUNNING` exit-0), AC2 (reclaim expired), AC3 (heartbeat during cool-down) | ✅ Covered |
| FR-12 | Pure engine, decoupled trigger | Epic 3 / Story 3.1 | 3.1 AC2 (pure command, dumb CLI) | ✅ Covered |
| FR-13 | Wide screening mart | Epic 1 / Story 1.6 (+1.2 DDL) | 1.2 AC2 (MV/mart created before insert), 1.6 AC3 (wide, one row per cik/period) | ✅ Covered |
| FR-14 | Coverage & status report | Epic 2 / Story 2.4 | 2.4 AC1 (present count, high-water mark, explained gaps) | ✅ Covered |

**FRs in epics but NOT in PRD:** None. The epics FR list matches PRD FR-1…FR-14 one-for-one (wording lightly condensed but semantically faithful).

### 3.2 Coverage Statistics

- **Total PRD FRs:** 14
- **FRs covered in epics:** 14
- **Coverage:** **100%** — every FR has a traceable epic + story + acceptance-criteria path.
- **Bonus — NFR traceability:** epics enumerate NFR-1…NFR-10, folding PRD §9 constraints (C-1…C-4) into NFR-7 (EDGAR compliance), NFR-8 (restatement correctness), NFR-9 (legal/ToS), NFR-10 (taxonomy coupling). This is *more* explicit than the PRD's §8 and reconciles cleanly.

### 3.3 Missing / Watch Requirements

No **critical** or **high-priority** missing FRs — coverage is complete. Two nuances to carry into story-quality review (Step 5), not blockers:

- **FR-2 (request-count minimization) is thin for v1.** The FR Coverage Map assigns it to Epic 1/Story 1.3, but 1.3 only enforces the *rate* (that's FR-1). FR-2's substance splits: **index-based discovery** is genuinely present (Story 2.2 reads the EDGAR index rather than crawling per-company pages ✅), but **bulk-artifact minimization** (the `companyfacts.zip` path in FR-7's testable consequence) is *deliberately deferred* — v1 backfills per-company (Story 2.3 AC1/AC4). This is a conscious, documented scope choice (PRD §6.2 lists bulk widening as "Could"), **not a gap** — but FR-2's mapping to Story 1.3 is slightly mislabeled; its real home is Story 2.2. Recommend noting FR-2 → Story 2.2 in the coverage map.
- **NFR-3 (performance) has no dedicated acceptance criterion.** It's `[ASSUMPTION]` in the PRD with budgets deferred to architecture; no story asserts a measurable latency/throughput target. Will verify in Step 4/5 whether the architecture pinned concrete budgets and whether that needs a story AC.

## 4. UX Alignment Assessment

### UX Document Status

**Not Applicable (confirmed).** No UX document exists, and none is required for v1.

### Is UX Implied?

Assessed against the "is UI implied?" checklist — **No** on every axis, and confirmed by kboss:

- **PRD mentions a UI?** Only to *exclude* it. §5 Non-Goals: "Not a UI or a custom query language. SQL against ClickHouse is the surface." §4.5: "There is no bespoke UI or query language in v1; the database *is* the tool."
- **Web/mobile components implied?** None. Deployment is a local Python CLI + containerized ClickHouse (NFR-5).
- **User-facing application?** The surface is raw SQL against ClickHouse plus a Typer CLI for operations — a technical, single-operator instrument, not a GUI product.
- **Builder confirmation (2026-07-23):** "currently it is a data pipeline. later on we might have a ui but not at the moment."

The epics document is consistent: "There is no UX design contract — v1 is a SQL query surface with no UI," and its UX Design Requirements section reads "None."

### Alignment Issues

None. There is no UX contract to misalign with PRD or Architecture.

### Warnings

- ℹ️ **No warning raised.** Missing UX is intentional and documented, not a planning gap.
- 🔭 **Future note (non-blocking):** if a UI is added later, it becomes a net-new planning cycle (UX spec → PRD delta → architecture delta → new epics). The current CLI/SQL boundary is clean, so a future UI can layer on the existing pure-engine + SQL surface without disturbing v1.

## 5. Epic & Story Quality Review

Reviewed all 3 epics / 13 stories against create-epics-and-stories standards: user-value focus, epic independence, story sizing, forward-dependency prohibition, AC quality, DB-creation timing, and greenfield indicators. Cross-referenced against `ARCHITECTURE-SPINE.md` (18 ADs) and `BUILD-SPLIT.md`.

### 5.1 Epic Structure

**User-value focus — PASS (notably strong).** All three epics are outcome-framed, not technical milestones:
- Epic 1 "Prove an end-to-end trustworthy screen (walking skeleton)" — a vertical slice delivering a working SQL screen (one company as *test vehicle*), not a horizontal "setup DB" layer.
- Epic 2 "Backfill the S&P 500 Universe" — delivers the actual market-wide screening value.
- Epic 3 "Keep it current & safe" — delivers ongoing currency + ban-safety + recoverability.

🟢 **Green flag:** the BUILD-SPLIT proposed **6 horizontal build-units** (A Foundations, B Client, C Ingestion, D Reconciler, E Trigger, F Resolution) — a literal 1:1 mapping would have produced exactly the "Setup Database / API Development" technical-epic anti-pattern this review exists to catch. The epics author instead **re-sliced them into 3 vertical value epics** (walking skeleton → market → currency). I verified all 6 build-units are fully absorbed with nothing dropped (A→1.1/1.2, B→1.3, C→1.4/1.5, D→2.2/2.3/3.1/3.3, E→3.1/3.2, F→1.6/2.4).

**Epic independence — PASS.** Epic 1 stands alone; Epic 2 needs only Epic 1's pipeline; Epic 3 reuses Epic 1+2 and explicitly *reuses (never re-implements)* Epic 2's resumability machinery (with a stated story-review guardrail). No Epic N → Epic N+1 forward dependency. Dependency flow declared: 1 → 2 → 3.

### 5.2 Story Quality

- **Sizing:** every story is a coherent, independently completable unit; none is epic-sized. ✅
- **Within-epic dependencies:** all backward-only (1.1→1.2→1.4→1.5→1.6; 1.3 independent; 2.1→2.2→2.3→2.4; 3.1→3.2, 3.3 thin). **No forward dependencies found.** ✅
- **Acceptance criteria:** uniform Given/When/Then BDD, testable, specific (cite concrete commands `fintin status` / `catch-up` / `recover --cik X` and AD numbers). Error/edge paths covered — malformed config (1.1), throttle failure (1.3), unresolvable ticker (2.1), per-company fetch failure non-fatal (2.3), crash-holding-lease reclaim (3.2). ✅
- **Greenfield setup:** Story 1.1 is the project-scaffold story (uv, docker-compose, config, CLI) — correct, and architecture confirms "no starter template," so no starter-clone story is required. ✅

**Justified deviation (reviewed, not a defect):** Story 1.2 creates *all* schema (Tier 0, Tier 1, MV, mart) upfront rather than table-per-story. The generic best-practice prefers "create tables when needed," **but AD-18 mandates the MV + mart exist before any insert** (ClickHouse MVs do not backfill pre-existing rows). The upfront-DDL design is architecture-required and correctly assigned to the single DDL owner. ✅

### 5.3 Traceability (spot-audited)

- **FRs:** 14/14 mapped to stories (Step 3). ✅
- **ADs:** all **18 ADs** trace to at least one story AC (e.g. AD-6→1.2/1.4/1.5, AD-7→1.6, AD-12→3.2, AD-16→2.2/2.3, AD-18→1.2). ✅
- **NFRs:** 9 of 10 have a testable home (NFR-1→2.3/3.2, NFR-2→2.1/2.3, NFR-4→2.4/3.x, NFR-5→1.1, NFR-7→1.3/3.2, NFR-8→1.6 fixture, NFR-9→1.3, NFR-10→1.5; NFR-6 is a $0 fact with nothing to test). **NFR-3 (performance) is the sole NFR with no verifying AC** — see findings. 

### 5.4 Findings by Severity

#### 🔴 Critical Violations
**None.** No technical epics, no forward dependencies, no un-completable stories, no coverage gaps.

#### 🟠 Major Issues
**None that block implementation.** (NFR-3 below is borderline; classified Minor given a deliberately qualitative target on a personal tool.)

#### 🟡 Minor Concerns
1. **NFR-3 performance has no verifying acceptance criterion.** The target ("single-unattended-session backfill; single-digit-second screens") is qualitative and was explicitly deferred from the PRD to architecture, but the architecture also left it unquantified and no story asserts it. *Impact:* low for a personal tool, but there is nothing to catch a performance regression. *Recommendation:* add a lightweight sanity AC — e.g. on Story 1.6 ("a representative screen returns in < N s on the dev laptop") and/or Story 2.3 ("full S&P 500 backfill completes in one unattended session") — even if N stays soft. Escalate to 🟠 if screen latency turns out to matter in use.
2. **FR-2 is mislabeled in the FR Coverage Map.** It points to Epic 1 / Story 1.3, but 1.3's ACs enforce the *rate* (FR-1). FR-2's real substance — index-based discovery — lives in Story 2.2 (bulk-artifact minimization is deliberately deferred). *Recommendation:* re-point FR-2 → Story 2.2 in the coverage map for accurate traceability. No functional gap.
3. **No CI/CD setup story.** The Testing convention references CI ("never hit live EDGAR in tests/CI"), but no story establishes a CI pipeline. Likely intentional for a solo builder. *Recommendation:* either add a one-line note that CI is out of v1 scope, or a small story if automated test runs are wanted.

### 5.5 Quality Verdict

Epic/story quality is **high**. The planning demonstrates mature practice: vertical-slice epics, complete FR/AD/NFR traceability, BDD ACs with error paths, a product-defining restatement fixture as the linchpin test, and an explicit reuse guardrail. The three findings are all Minor and none blocks starting Epic 1.

## 6. Summary and Recommendations

### Overall Readiness Status

## ✅ READY

fin-tin is cleared to enter Phase 4 implementation. Planning is complete, internally consistent, and unusually well-traced. No critical or blocking issues surfaced across document discovery, requirements extraction, FR coverage, UX alignment, or epic/story quality.

### Evidence Backing the Verdict

- **100% FR coverage** — all 14 PRD FRs map to a specific epic, story, and acceptance criterion.
- **Full architecture traceability** — all 18 architecture decisions (AD-1…AD-18) are reflected in story ACs.
- **NFR coverage** — 9 of 10 NFRs have a testable home; only NFR-3 (performance) lacks a verifying AC.
- **Epic design maturity** — vertical value-slice epics (walking skeleton → market → currency) that deliberately avoid the horizontal technical-epic anti-pattern the raw 6-unit build-split would have produced; no forward dependencies; clean independence.
- **AC quality** — uniform BDD, testable, with error/edge paths and a product-defining restatement fixture as the linchpin correctness test.
- **UX** — correctly N/A (data pipeline, no UI in v1), documented and consistent across PRD and epics.

### Critical Issues Requiring Immediate Action

**None.** There is nothing that must be fixed before writing code.

### Recommended Next Steps (all optional polish — do not gate implementation)

1. **Add a lightweight performance AC for NFR-3.** Put a soft target on Story 1.6 (screen returns in single-digit seconds on the dev laptop) and/or Story 2.3 (S&P 500 backfill completes in one unattended session), so there's a tripwire for regressions. *(Minor #1)*
2. **Re-point FR-2 in the epics FR Coverage Map** from Story 1.3 → Story 2.2 (index-based discovery), since 1.3 enforces the rate (FR-1), not request-count minimization. *(Minor #2)*
3. **State CI intent explicitly** — either note "CI out of v1 scope (solo builder)" or add a small CI story, since the Testing convention references CI without a story establishing it. *(Minor #3)*
4. **Begin implementation with Epic 1 (walking skeleton).** It de-risks the ClickHouse mutation/resolution mechanics (AD-6, AD-8) and edgartools integration before any scale — the correct first move.

### Final Note

This assessment identified **3 issues, all Minor, across 2 categories** (traceability labeling, NFR test coverage) — **0 critical, 0 blocking-major**. The artifacts are in strong shape; you may address the minor items opportunistically or proceed as-is. Recommended path: proceed to implementation starting with Epic 1, folding the three polish items in as you go.

---

**Assessor:** Implementation Readiness reviewer (Product Manager role)
**For:** kboss
**Date:** 2026-07-23
**Documents assessed:** PRD, Architecture Spine + Build-Split, Epics & Stories (UX: N/A)
**Verdict:** ✅ READY for Phase 4 implementation

---

## 7. Post-Assessment Remediation (2026-07-23)

kboss elected to apply all three Minor findings immediately. Edits made to `epics.md`:

- **Minor #1 (NFR-3 performance AC) — RESOLVED.** Added a soft-target AC to **Story 1.6** (representative screen returns in single-digit seconds on the dev laptop — regression tripwire, not a hard SLA) and to **Story 2.3** (full S&P 500 backfill completes in one unattended session, never traded against rate-limit compliance per SM-C1). NFR-3 now has a testable home; all 10 NFRs covered.
- **Minor #2 (FR-2 mislabel) — RESOLVED.** Re-pointed FR-2 from Epic 1 → **Epic 2 (Story 2.2)** in the FR Coverage Map; moved FR-2 between the two epics' "FRs covered" lists; removed the FR-2 citation from Story 1.3 AC1 (it enforces the rate = FR-1); added an explicit FR-2 citation to Story 2.2 AC1 (index-based discovery).
- **Minor #3 (CI intent) — RESOLVED.** Added an explicit "CI/CD (out of v1 scope)" bullet to the epics' Additional Requirements — single-operator local tool, fixture-based suite runs locally via `uv run pytest`, CI a trivial future add that must never touch live EDGAR.

**Post-remediation status:** ✅ READY — 0 open findings. All 14 FRs, all 18 ADs, and all 10 NFRs now have a testable story home.
