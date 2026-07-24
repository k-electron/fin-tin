# Prose Editorial Review — prd.md

**Document:** `prd-fin-tin-2026-07-23/prd.md`
**Review type:** Prose / copy-edit (clarity, grammar, terminology, parse-ability). Structure is out of scope (separate reviewer).
**Method:** `bmad-editorial-review-prose` (Microsoft Writing Style Guide baseline; minimal intervention; content sacrosanct — only how ideas are expressed, never the ideas).
**Reviewer note:** The Glossary (§3) explicitly states *"Downstream workflows and readers must use these terms exactly; no synonyms elsewhere in the PRD."* That instruction makes capitalization drift and synonym use of defined terms genuine defects, not style preferences — most findings below stem from it.

**Overall prose health: strong.** The PRD is dense, precise, and deliberately voiced; sentences are mostly well-formed and the technical register is consistent. There are almost no true grammar errors. The dominant issue is capitalization/synonym drift against the PRD's own Glossary, plus a few readability snags. All findings are low-to-moderate severity.

---

## Findings

| Location | Original Text | Revised Text | Changes |
|----------|---------------|--------------|---------|
| §1 line 17; §2.3 line 40 | "picking up new filings and restatements" / "new filings + any restatements" | "picking up new Filings and Restatements" / "new Filings + any Restatements" | Glossary defines **Filing** and **Restatement** as capitalized terms; these are the only noun uses left lowercase (contrast FR-2 line 88 "new Filings", FR-7 line 137, §4.3 line 122 "a Restatement"). Capitalize for consistency. |
| §5 line 211 | "It stores what screening needs, not filings wholesale." | "It stores what screening needs, not Filings wholesale." | Same capitalization fix for the Glossary term **Filing**. |
| §0 line 13; §1 line 21; §6.1 line 218 | "reconciler internals" / "a stateless reconciler" / "Backfill + Catch-up reconciler" | "Reconciler internals" / "a stateless Reconciler" / "Backfill + Catch-up Reconciler" | **Reconciler** is a Glossary term (capitalized at §3, FR-8, §5); these three uses are lowercase. Align capitalization. |
| §2.3 line 40 | "runs a cross-sectional screen against the wide mart" | "runs a cross-sectional screen against the Screening Mart" | "wide mart" is a synonym for the Glossary term **Screening Mart** (defined as "the wide … materialized view"). Use the defined term. |
| §2.3 line 40 | "a ranked, normalized result set across the configured company universe" | "a ranked, normalized result set across the configured Universe" | "configured company universe" is a lowercase synonym for the Glossary term **Universe**; elsewhere the PRD uses "configured Universe" (§4.3 line 127, §4.6 line 197). Align. |
| §4.1 line 87; §NFR line 245; §NFR line 248; §9 line 266 | "small curated subsets" / "Curated-subset Backfill" / "bounded by the curated subset" / "acceptable for small subsets" | Use **Universe** consistently, e.g. "the v1 (S&P 500) Universe" | "curated subset" is used as an undefined synonym for the v1 **Universe** (the S&P 500 constituents). Worse, it drifts in meaning: at line 87 "small curated subsets" reads as a hypothetical *smaller-than-S&P-500* set contrasted with "large/full Universe," while at lines 245/248 it clearly means the v1 S&P 500 Universe. Pick one term and one meaning; if a sub-S&P-500 set is genuinely a distinct concept, name and define it. |
| §8 line 258 | "v1 Universe = S&P 500 (confirmed); remaining is how the constituent list is sourced and refreshed as membership changes over time" | "…(confirmed); what remains open is how the constituent list is sourced and refreshed as membership changes over time" | "remaining is how" is ungrammatical as a clause opener (dangling gerund). Rephrase to "what remains open is how …". |
| §4.2 FR-5 line 113 | "For any (Canonical Concept, unit, period) the queryable value is the most-recently-filed; all filed versions are retained and distinguishable." | "For any (Canonical Concept, unit, period) the queryable value is the one from the most-recently-filed Filing; all filed versions are retained and distinguishable." | "the most-recently-filed" is an adjective with no noun; the intended object is missing. The Glossary's own definition of **Latest-filed-wins** ("the value … from the most-recently-filed Filing") supplies the fix. |
| §4.1 line 70 | "makes ban-avoidance structural rather than a tuned afterthought" | "makes ban-avoidance a structural property rather than a tuned afterthought" | Faulty parallelism: predicate adjective ("structural") contrasted with a noun phrase ("a tuned afterthought"). Make both noun phrases so the "rather than" pairing reads cleanly. |
| §4.1 line 79 | "a blank/undeclared UA (which EDGAR rejects as an \"Undeclared Automated Tool\") is never sent" | "a blank/undeclared User-Agent (which EDGAR rejects as an \"Undeclared Automated Tool\") is never sent" | "UA" is an unexpanded abbreviation used exactly once; every other reference (FR-1, Constraints §, Assumptions §) spells out "User-Agent." Spell it out here too for consistency. |
| §4.2 FR-4 line 105 | "The system maps Tier 0 Facts to Canonical Concepts via the Taxonomy into Tier 1, keyed by Raw-Fact Identity…" | "The system maps Tier 0 Facts into Tier 1 — keyed by Raw-Fact Identity — resolving Canonical Concepts via the Taxonomy…" | Consider: the chain "maps X to Y via Z into W" forces the reader to hold three prepositional targets at once. Reordering so the source→destination ("Tier 0 … into Tier 1") is adjacent, with the mapping mechanism trailing, is easier to parse. (Query, not a required change — meaning is recoverable as written.) |
| §0 line 13 | "features are grouped with globally-numbered FRs nested" | "features are grouped, with globally-numbered FRs nested beneath them" | Terse to the point of ambiguity ("nested" in what?). A comma plus "beneath them" clarifies the relationship without adding length. |
| §4.5 line 178 vs FR-12 line 183 | "pivots the long canonical Facts" (178) vs "pivoting Canonical Facts" (183) | Use one capitalization, e.g. "canonical Facts" in both | Minor: "canonical Facts" is capitalized inconsistently across two adjacent references. ("Canonical Fact" is not itself a Glossary term — **Fact** and **Canonical Concept** are — so lowercase "canonical" as a descriptor is acceptable, but pick one form.) |
| §4.1 line 75 / line 79 / Constraints line 252 | "a mandatory declared, identifying User-Agent" (75) vs "a declared, identifying User-Agent" (79) vs "mandatory declared identifying User-Agent" (252) | Standardize punctuation, e.g. "a mandatory, declared, identifying User-Agent" | Minor: the same three-adjective phrase is punctuated three different ways. Choose one comma pattern and apply it in all three places. |

---

## Notes on what was deliberately NOT flagged

- **"the store" / "local store" / "corpus"** — used pervasively as informal cover terms for the two-tier fact store as a whole. There is no single Glossary term for "the whole store" (only Tier 0 / Tier 1), so this is acceptable generic usage, not a synonym violation.
- **"the mart" (FR-12 lines 187–188, NFR line 245)** — short back-reference to **Screening Mart** *after* it has been introduced in the same section; acceptable shorthand (parallel to "the store"). Only the pre-introduction "wide mart" in the User Journey (line 40) is flagged above.
- **Sentence fragments** ("Realizes UJ-1." FR-7; "Single operator role, so one lightweight journey…" §2.3) — intentional terse PRD register; preserved per author-voice principle.
- **Coinages** ("always-current-enough," "un-ingested," "latest-filed") — deliberate, meaning clear in context; register respected.
- **`=` in prose** (Constraints line 252, §8 line 258, §9 line 270) — terse notation, an intentional style choice consistent throughout; not a comprehension barrier.
- **Dense semicolon lists** (§6.2 lines 224–226) — these are list/structure constructs; deferred to the structural reviewer.
