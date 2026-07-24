---
title: Structural Editorial Review — fin-tin PRD
type: editorial-review-structure
target: ./prd.md
reviewer: bmad-editorial-review-structure
date: 2026-07-23
---

# Structural Review: fin-tin PRD

## Document Summary
- **Purpose:** Lean technical PRD for a solo-built local EDGAR query tool; hand-off to downstream BMad workflows (architecture, epics/stories).
- **Audience:** The builder (kboss) as sole dev/operator/user, plus downstream BMad automation.
- **Reader type:** humans (with LLM/downstream-workflow consumers — favors precision, one-source-of-truth).
- **Structure model:** Strategic/Context (Pyramid) — Vision → Target User → Glossary → Features/FRs → Scope → Metrics → NFRs/Constraints → Open Questions/Assumptions.
- **Current length:** ~2,700 words across 10 numbered sections + 2 unnumbered (NFRs, Constraints).

**Overall structural health: strong.** The FR-with-testable-Consequences format is disciplined, the Glossary is a genuine single source of truth, and the Vision's "one design principle" framing is well placed. The lean intent is mostly honored. The one systemic weakness: the back half of the document (§6 Scope, NFRs, Constraints, §8, §9) re-states requirements already fully specified in §4's FRs. The findings below are almost all consolidation of that trailing redundancy — no substantive requirement is proposed for removal, only relocation to its single canonical home.

---

## Recommendations

### 1. CONDENSE (near-CUT) — "Constraints & Guardrails" section
**Rationale:** Two of its three bullets are verbatim restatements of existing FRs: "EDGAR fair-access compliance" repeats FR-1 + FR-2 + FR-11 in full (10 req/s aggregate, mandatory declared User-Agent, 10-minute cool-down, minimize-request-count strategy, throttle-and-single-flight-in-engine-not-trigger — every clause already appears in §4.1 and FR-11), and "Data correctness (restatements)" repeats FR-5's consequences (original vs. restated both retained by Accession + filed date, never clobbered). Only "Legal / ToS" carries net-new content, and it is one line.
**Fix:** Cut the two restating bullets. Preserve the sourcing/verification date fact ("verified 2026-07-23 against SEC Internet Security Policy and Accessing EDGAR Data pages") by relocating it into FR-1 (or a one-line citation footnote) since it is the provenance for the FR-1 defaults. Keep "Legal / ToS" — fold it into §5 Non-Goals or the NFR list. Net effect: the section shrinks to ~1 line or disappears.
**Impact:** ~200 words
**Comprehension note:** None lost — every requirement survives in its FR home; this removes a competing second copy that can drift.

### 2. CUT — §6.1 "In Scope"
**Rationale:** It re-lists FR-1 through FR-13 by number with a one-line gloss each. §4 Features already enumerates exactly these FRs with fuller descriptions; the FR set *is* the MVP scope. This is a pure restatement, not a summary that adds grouping or priority.
**Fix:** Cut §6.1 entirely, or reduce to a single sentence ("In scope = all FRs in §4."). Keep §6.2, which carries genuinely new Should/Could/Won't prioritization not found elsewhere.
**Impact:** ~90 words
**Comprehension note:** Minimal; consider retaining the one-line pointer so §6 still reads as a scope gate.

### 3. MERGE — §5 Non-Goals ↔ §6.2 "Won't (this time)"
**Rationale:** All four "Won't (this time)" items (custom company-extension Concepts; ad-hoc reactive corruption repair; UI / multiple surfaces; foreign/IFRS filers) already appear as bullets in §5 Non-Goals. Two exclusion lists with overlapping membership force the reader to reconcile them.
**Fix:** Make one canonical exclusion list. Simplest: drop the §6.2 "Won't" sub-list and reference §5 ("Won't: see §5 Non-Goals"); §6.2 then keeps only the net-new Should/Could tiers.
**Impact:** ~40 words
**Comprehension note:** None — consolidates to a single source of truth for exclusions.

### 4. MERGE — §8 "Resolved" note + §9 "Confirmed — no longer assumptions" list
**Rationale:** Both track decisions that are settled. They overlap directly (the EDGAR rate limit + User-Agent + cool-down appears in both, and a third time in FR-1). Maintaining resolved/confirmed status in two trailing sections is duplicate bookkeeping.
**Fix:** Keep the resolved/confirmed ledger in one place (recommend §9's "Confirmed" list, since it is broader). Remove the §8 italic "Resolved" line, or replace it with a one-line pointer. §8 then holds only genuinely open questions.
**Impact:** ~60 words
**Comprehension note:** None — the confirmed facts remain recorded once.

### 5. CONDENSE — "Cross-Cutting NFRs": trim the two FR-restating bullets
**Rationale:** "Reliability / crash-safety" restates FR-9; "Observability" restates FR-10's status vocabulary + FR-13. Performance, Portability/deployment, and Cost are genuinely cross-cutting (no single FR owns them) and should stay.
**Fix:** Reduce Reliability and Observability to bare pointers ("Crash-safety: see FR-9." / "Observability: see FR-10 status vocabulary + FR-13.") or cut them, leaving the three net-new NFRs.
**Impact:** ~50 words
**Comprehension note:** Low; the pointer form preserves the cross-cutting framing without a second copy of the requirement text.

### 6. QUESTION — Section numbering breaks at the NFR/Constraints block
**Rationale:** §7 Success Metrics is followed by two *unnumbered* sections ("Cross-Cutting NFRs", "Constraints & Guardrails") before §8 Open Questions resumes numbering. Downstream workflows that cite sections by number (as §0 says they will) cannot reference these.
**Fix:** Number them (e.g. §7.5/§8 and renumber) or, if findings #1 and #5 are applied and they shrink to pointers, fold the survivors into adjacent numbered sections. Structural, not content.
**Impact:** ~0 words (numbering only)
**Comprehension note:** Improves citeability for the stated downstream consumers.

### 7. CONDENSE — §0 Document Purpose (optional)
**Rationale:** Serves a real function (tells downstream workflows how to read the doc — conventions, ASSUMPTION tagging, architecture-brief split). It is slightly over-long for a lean PRD and restates the doc structure the headings already convey.
**Fix:** Tighten to the load-bearing meta only: (a) audience = solo builder + downstream BMad, (b) design lives in companion architecture-brief.md (referenced not duplicated), (c) [ASSUMPTION] tag convention indexed in §9. Drop the sentence describing FR numbering/nesting (the headings show it).
**Impact:** ~40 words
**Comprehension note:** Keep the architecture-brief pointer and the ASSUMPTION-tag convention — both are functional for downstream readers.

---

## Preserve (explicitly keep — may look cuttable but earn their place)
- **§3 Glossary** — dense, but it is the single source of truth the rest of the doc and downstream workflows depend on; do not trim terms. (PRESERVE)
- **FR "Consequences (testable)" blocks** — these are the acceptance criteria, not prose padding; keep every one. (PRESERVE)
- **§1 Vision paragraph 3 (the "single design principle")** — the architectural thesis the whole design hangs from; high value density. (PRESERVE)
- **§7 Counter-metrics (SM-C1/SM-C2)** — express negative goals no FR captures; keep. (PRESERVE)
- **UJ-1 (§2.3)** — referenced by FR-7 ("Realizes UJ-1"); keep the anchor even though it overlaps §1/§2.1 in spirit. (PRESERVE)

---

## Summary
- **Total recommendations:** 7 (2 CUT/near-CUT, 3 MERGE/CONDENSE consolidations, 1 numbering QUESTION, 1 optional CONDENSE) + 5 PRESERVE.
- **Estimated reduction:** ~480 words (~18% of ~2,700), concentrated in the trailing scope/NFR/constraints/assumptions block; near-zero cuts in §0–§4.
- **Meets length target:** No explicit target; result is a leaner document that keeps the lean intent without losing any requirement.
- **Comprehension trade-offs:** Negligible. Every proposed cut removes a *second copy* of a requirement already fully specified in an FR; the canonical FR (with its testable Consequences) always survives. No substantive requirement is dropped.
