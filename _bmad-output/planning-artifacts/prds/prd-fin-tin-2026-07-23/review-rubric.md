# PRD Quality Review — fin-tin — Local EDGAR Financial-Statement Query Tool

## Overall verdict

This is a strong, disciplined capability-spec PRD that knows exactly what it is. It rests on a single genuine thesis — *never maintain a second copy of state that can drift from reality* (§1) — and every feature is visibly derived from it (stateless Reconciler, DB-derived work list, latest-filed-wins, engine/trigger separation), so it reads as an argument rather than a backlog. Done-ness is well-served: all thirteen FRs carry concrete testable consequences with real values (10 req/s, 10-minute cool-down, `NOTHING_TO_DO`, exit-0), and scope honesty is exemplary. The only real risk is a broken locator to the companion architecture brief (§0), which the downstream architecture workflow depends on; the substantive content is green-light-ready.

## Decision-readiness — strong

A single operator is both decision-maker and builder here, and the PRD gives that reader everything needed to act. The core architectural bet is stated as a decision, not smuggled in as a "consideration": §1's "single principle" and its explicitly-named corollaries. Trade-offs name what was surrendered, not just what was chosen — §5 excludes custom company-extension Concepts as an "accepted data-completeness cost for a lean, consistent corpus," and defers ad-hoc reactive corruption repair *because* it "would reintroduce the one piece of genuinely maintained state." Counter-metrics SM-C1 and SM-C2 encode the two live tensions honestly ("a broad-but-wrong store is worse than a narrow-but-trustworthy one"). Open Questions (§8) are genuinely open and routed to architecture, not rhetorical. The `[NOTE FOR PM]` at §6.2 lands on a real tension — derived metrics are "a *stated secondary goal*" being deferred — exactly where the rubric wants it, not at a safe checkpoint.

## Substance over theater — strong

Nothing here reads as furniture. There is one User Journey, not a padded persona cast, and its single-ness is argued (§2.3: "Single operator role... no multi-persona narrative warranted"). The Vision (§1) is unswappable — it names the specific pain (`us-gaap:Revenues` vs. `RevenueFromContractWithCustomerExcludingAssessedTax` vs. custom extensions, silent restatements) and would not drop into any other PRD. The NFR section avoids boilerplate: Reliability is tied to `kill -9` and FR-9, Cost states "$0 external," Portability commits to macOS + containerized ClickHouse + Python CLI. No innovation-theater differentiation section exists because the PRD didn't need to manufacture one.

## Strategic coherence — strong

The PRD has a thesis and bets on it. Feature prioritization follows from correctness-first, not ease-first: the whole two-tier store, latest-filed-wins, and universal resumability exist to make cross-sectional screens trustworthy, which is the stated primary Job To Be Done (§2.1). Success Metrics validate the thesis rather than measure activity — SM-1 (restatement correctness, 100% on a fixed test set) and SM-2 (no silent coverage omissions) directly test the correctness bet; the one adoption metric (SM-4) is explicitly framed as the builder-value proxy, not vanity engagement. Counter-metrics are present and counterbalance named SMs. MVP scope kind is coherently problem-solving (correctness of a normalized corpus), and the scope logic in §6 matches it.

## Done-ness clarity — strong

Every FR (FR-1 through FR-13) carries a "Consequences (testable)" block, and the consequences are largely verifiable conditions with concrete values, not adjectives: FR-1's UA form and 10-minute cool-down, FR-7's `NOTHING_TO_DO` on a no-op re-run, FR-10's enumerated status vocabulary (`STARTED`/`ALREADY_RUNNING`/`NOTHING_TO_DO`/`COMPLETED`) and exit-0 on a concurrent trigger, FR-5's "groups on actual reporting-period dates, not fiscal-period labels." An engine team could source acceptance criteria straight from these. Two soft edges remain, both minor and both honestly flagged rather than hidden.

### Findings
- **low** Performance NFR bounds are soft (§ Cross-Cutting NFRs) — "completes within a single unattended session on a developer laptop" is not a threshold, and "single-digit seconds" is a range. This is tagged `[ASSUMPTION]` with "Concrete budgets deferred to architecture," which is honest deferral appropriate for a solo tool, not hand-waving. *Fix:* ensure the architecture brief picks up a concrete latency/throughput budget so the deferral doesn't silently evaporate downstream.
- **low** FR-12 mart-refresh mechanism is ambiguous (§4.5, FR-12) — "refreshed automatically/incrementally on ingest" conflates two different mechanisms with a slash. The testable consequence ("The mart reflects newly-ingested Facts after a Catch-up") is clear, so done-ness holds. *Fix:* let architecture pick one mechanism; leave the FR stating only the observable outcome.

## Scope honesty — strong

Among the strongest dimensions. §5 Non-Goals does real work — seven explicit exclusions, each with a rationale, plus a `[NON-GOAL for MVP]` callout. §6.2 partitions deferred work MoSCoW-style (Should/Could/Won't) so nothing omitted is left to inference. Inferences are tagged inline `[ASSUMPTION: …]` and indexed in §9, and §9 carries a roundtrip discipline most PRDs skip — a "Confirmed — no longer assumptions" list separating settled facts (rate limit, S&P 500 Universe, per-company commit granularity) from live assumptions. Open-items density is low (3 Open Questions, 3 live assumptions) and entirely appropriate for a green-light-to-build solo tool.

## Downstream usability — strong

This is a chain-top PRD feeding architecture and epics/stories, so this dimension carries weight, and it mostly holds. The Glossary (§3) is rich and its terms are used consistently across FRs, SMs, and the UJ. IDs are contiguous and unique (FR-1..FR-13, SM-1..SM-4, SM-C1..SM-C2, UJ-1); cross-references largely resolve (FR-7 "Realizes UJ-1"; each SM cites the FRs it validates). One reference is actually broken and it is the load-bearing one for the next workflow in the chain.

### Findings
- **medium** Companion architecture brief locator is wrong (§0) — §0 says the technical design "lives in the companion **`architecture-brief.md`** (same folder)," but the file is not in the PRD folder; it is at `_bmad-output/brainstorming/brainstorm-edgar-financials-query-tool-2026-07-23/architecture-brief.md`. A downstream architecture workflow told "same folder" will not find it where it looks. (The sibling `brainstorm-intent.md` relative path in §0 does resolve correctly.) *Fix:* correct the §0 pointer to the actual relative path, or copy/move the brief into the PRD folder to match the claim.
- **low** "Curated subset" is used as a de facto synonym for the S&P 500 Universe but is not a Glossary term (§6.2 "expansion from the curated subset to the full-market Universe"; NFR Cost "bounded by the curated subset"; §NFR/§9 "Curated-subset Backfill"). Meaning is inferable but a downstream reader source-extracting on Glossary terms won't find it. *Fix:* either add it to the Glossary or replace it with "v1 Universe (S&P 500)".

## Shape fit — strong

Near-exemplary. The PRD correctly adopts capability-spec shape for a single-operator technical tool: §0 declares itself "deliberately lean and technical," §2.3 justifies exactly one lightweight UJ instead of forcing multi-persona formalism, and Success Metrics are appropriately a mix of operational (SM-2 coverage, SM-3 currency) and correctness (SM-1) rather than user-facing engagement — precisely what the rubric prescribes for this shape. It is neither over-formalized (no UJ padding) nor under-formalized (FRs are fully specified). Handoff discipline is good: detailed technical design is deferred to the companion brief and Open Questions rather than being invented here.

## Mechanical notes

- **Cross-ref (companion brief):** §0's "same folder" locator for `architecture-brief.md` is inaccurate — file lives in the brainstorming folder (raised as a medium finding under Downstream usability because it affects the architecture handoff).
- **Cross-ref (Open Questions numbering):** FR-2's `[ASSUMPTION]` and §9 point to "§8.2" / "§8", but §8's items are an unnumbered list (1/2/3), so "§8.2" is loose but resolvable to Open Question #2. Consider numbering §8 items §8.1/§8.2/§8.3 for clean pointers.
- **Glossary drift (minor):** the defined term **Screening Mart** appears informally shortened to "the mart" (UJ-1, NFR) and "wide mart" (UJ-1). Formal term is used at first/authoritative use; drift is cosmetic. "Curated subset" is undefined (raised as low finding above).
- **ID continuity:** clean — FR-1..FR-13 contiguous and unique; SM and UJ IDs unique; no gaps or duplicates.
- **Assumptions Index roundtrip:** clean — the three inline `[ASSUMPTION]` tags (FR-2 bulk-artifact path, SM-4 weekly, NFR performance) all appear in §9; §9 entries all trace back inline. The "Confirmed — no longer assumptions" list is a good addition.
- **UJ protagonist:** UJ-1 has a named protagonist (kboss) carrying context inline; no floating UJs.
- **Required sections:** all present and appropriate for the agreed stakes and solo-tool shape (Vision, Target User, Glossary, Features/FRs, Non-Goals, MVP Scope, Success Metrics, NFRs, Constraints, Open Questions, Assumptions Index).
