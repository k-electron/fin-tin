# PRD ↔ Sources Reconciliation — Gap Report

**PRD:** `prd.md` (fin-tin, 2026-07-23)
**Sources:** `brainstorm-intent.md`, `architecture-brief.md`, `implementation-tasks.md`, `.memlog.md`
**Reviewer note:** Deep technical mechanics correctly deferred to `architecture-brief.md` are NOT flagged. Only genuinely missing/distorted/contradicted PRODUCT intent or requirements appear below, ranked by materiality.

---

## HIGH materiality

### 1. Settled "bulk companyfacts.zip backfill, never a crawl" MUST demoted to an open question
- **Sources say (settled MUST, all four docs):**
  - intent `brainstorm-intent.md` MoSCoW **Must**: "Bulk `companyfacts.zip` backfill + stateless reconciler / 'catch up to today' delta."
  - `architecture-brief.md` §4: "Backfill is the only expensive diff → handled as a one-time offline bootstrap via the **bulk `companyfacts.zip`**, never a crawl." Repeated in §4 fetch table and Appendix MUST.
  - `.memlog.md` (lines 45, 80): "Backfill is the ONLY expensive diff -> one-time offline bootstrap via bulk companyfacts.zip, **never a crawl**."
  - `implementation-tasks.md` Phase 3: "Acquire bulk companyfacts.zip — one download for the whole corpus **instead of a crawl**."
- **PRD diverges:** FR-2 carries `[ASSUMPTION: bulk-artifact path used for large/full Universe; small curated subsets may use per-company calls — strategy selection deferred to architecture]`; Open Question #2 reopens it as "bulk-artifact-and-filter **vs.** per-company API for the S&P 500 subset; deferred to architecture"; §9 Assumptions Index repeats it. A decision the sources treated as closed (bulk zip, never per-company crawl) is presented as unresolved. This weakens a MUST.

### 2. The S&P 500 Universe scope is not grounded in any source
- **Sources say:** No source mentions "S&P 500" or any universe narrowing. All assume the **full corpus / all public companies**: intent Product ("query financial statement concepts across many public companies"); `architecture-brief.md` §1 & §4 ("across public companies", bulk zip "for the whole corpus", "full-Universe Backfill"); `implementation-tasks.md` Phase 3 ("one download for the whole corpus"). The `.memlog.md` — described as the canonical decision record and marked `status: complete` — records no such scoping decision.
- **PRD diverges:** Glossary defines Universe as "v1: the **S&P 500 constituents**; full-market is deferred"; FR-6, §6.1, and §7 SM-2 all bind to S&P 500; §6.2 pushes full-market to "Could"; Open Question #1 asserts "v1 Universe = S&P 500 (**confirmed**)." The PRD presents an unsourced scope narrowing as a confirmed decision. This is also the root cause of gap #1 — the subset is what makes per-company backfill even a question, since the sources only ever contemplated full-corpus backfill via the bulk zip.

---

## MEDIUM materiality

### 3. us-gaap-only / no-IFRS narrowing contradicts the "hoard all standard-taxonomy facts" design
- **Sources say:** Standard taxonomies to hoard = **us-gaap / ifrs-full / dei / srt** consistently: `architecture-brief.md` §3.1 table and §3.4 ("Standard taxonomies only"); `.memlog.md` lines 37, 40 ("us-gaap/ifrs/dei/srt"); intent Key Product Decision ("us-gaap/ifrs-full/dei/srt only"). Tier 0 is meant to "hoard **ALL** standard-taxonomy numerical facts."
- **PRD diverges:** Glossary redefines "Standard-taxonomy Fact" as "`us-gaap`/`dei`/`srt`" — silently dropping **ifrs-full** — and Non-Goals adds "**Not covering foreign / IFRS filers in v1. US-domestic `us-gaap` only.**" No source states any IFRS/foreign-filer exclusion. This both adds an unsourced constraint and contradicts the sources' explicit inclusion of ifrs-full in the standard-taxonomy hoard (undermining the "future-proof, network-free" rationale for hoarding everything standard).

### 4. "EDGAR re-fetch / recovery capability is required regardless" is absent as a requirement
- **Sources say:** This is an explicit **user decision**, not just prose. `.memlog.md` line 23 (decision by user): "Keep all numerical facts in Tier0 (hoard), **AND require an EDGAR refetch/recovery capability regardless**"; line 24: "Tier0 is a cache, NOT ground truth… need refetch to recover from Tier0 corruption you can't debug locally." `architecture-brief.md` §3.1: "An explicit EDGAR **re-fetch/recovery capability is required regardless**."
- **PRD diverges:** FR-3 and §4.2 assert Tier 0 is "recoverable from EDGAR at throttled cost" as a *property*, but no FR requires **building** a re-fetch/recovery operation. Corruption handling in scope covers only at-rest hash + proactive scrub (SHOULD, detection-only) and ad-hoc reactive repair (WON'T). The re-fetch capability that actually *repairs* corrupted/lost Tier 0 data — a stated user requirement — is neither an FR, a MUST, nor listed as deferred. It exists only as an adjective ("recoverable").

---

## LOW / borderline materiality

### 5. Freshness-on-demand (push→pull) trade-off softened from a stated product stance to an implicit behavior
- **Sources say (Key Product Decision):** intent "On-demand / pull-based freshness. *Why:* freshness flips from push to pull — **data is fresh when you ask, not always current**… tie the trigger to use." `.memlog.md` line 62: "freshness flips PUSH->PULL (fresh when you ask, not always ~current; **your responsibility**)… tie the trigger to USE." `architecture-brief.md` §6.1.
- **PRD diverges:** Vision says "always-current-enough"; UJ-1 embodies the catch-up-then-screen behavior, so the *behavior* is present. But the explicit trade-off framing — that the store is stale-by-default and currency is the operator's responsibility — is not stated as a principle, constraint, or assumption. "Catch up before query" appears only in §6.2 Could scope. The qualitative stance behind the design is muted.

### 6. Taxonomy coupling/drift risk not surfaced as a product-level standing dependency
- **Sources say:** `architecture-brief.md` §3.4 (Risk), §9, §10: "Coupling / drift from outsourcing the taxonomy to edgartools (custom/uncommon tags fall through; mappings shift across versions)… **remains the standing external dependency to watch**." `.memlog.md` line 18.
- **PRD diverges:** FR-4 records `Taxonomy version` per Fact (the *enabler* of the mitigation), but the PRD nowhere surfaces edgartools taxonomy drift as a risk or standing dependency, and the mitigating re-map capability sits in Could scope. (Partly technical — noted at low priority since the mitigation mechanics belong in the architecture brief, but the *existence of the dependency* is product-relevant and currently invisible in the PRD.)

---

## Checked and OK (not gaps)
- Guiding principle ("never maintain a second copy of state…") — faithfully preserved in PRD §1.
- Latest-filed-wins, group on actual period dates not fiscal labels — FR-5. ✓
- Restatements as new filings (incl. comparatives in later filings) — Glossary + FR-7. ✓
- Corruption (within-accession byte change → overwrite) vs restatement (new accession → keep both) — Constraints + FR-3 content hash. ✓
- Hoard even currently-unmappable facts — FR-3 ("not only those currently mappable"). ✓
- Single-flight + self-expiring lease + exit-0 status vocabulary — FR-10. ✓
- Pure engine / dumb trigger split; throttle+single-flight in engine — FR-11. ✓
- Ad-hoc reactive repair = the one deliberate exception / ephemeral suspect-accession queue — Non-Goals + §6.2 Won't. ✓
- Rate limit "verify before hardcoding" open question — resolved to 10 req/s + mandatory UA + 10-min cool-down (FR-1). ✓ (Legitimate resolution, not a gap.)
- Derived-metrics as a stated secondary goal to revisit early — §6.2 Should `[NOTE FOR PM]`. ✓
