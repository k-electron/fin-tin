# Story 1.6: Resolve latest-filed-wins and screen via the wide mart

Status: ready-for-dev

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the builder,
I want the wide screening mart to return the latest-filed value per company-period for well-known concepts,
so that a SQL screen returns trustworthy (post-revision), cross-company-comparable numbers.

## Acceptance Criteria

1. **Given** multiple filed versions of the same (element, unit, period) **When** I query the mart **Then** it returns the most-recently-filed value, tiebreak prefer `/A` then greatest accession (FR-5, AD-7).
2. **Given** the restatement fixture (two filings of one period, different `filed_date`, differing values) **When** resolved **Then** the newer value wins — **this AC is required; it is the product-defining test (SM-1).**
3. **Given** a versioned **concept dictionary** (each screening concept = an ordered list of standard elements: FASB-primary + observed-frequency-ranked fallbacks) **When** the mart resolves a concept for a `(cik, period)` **Then** it returns the **latest-filed** value across the *union* of that concept's elements, breaking ties deterministically by element list-position (then the AD-7 filing tiebreak) — recency is respected AND multiple elements collapsing to one concept never produce a nondeterministic value (AD-8, AD-9, AD-7).
4. **Given** Tier 1 receives inserts **When** I query **Then** the mart reflects them (auto-populated) presented **wide**: one row per `(cik, period)` with screening concepts as columns (FR-13, AD-8).
5. **Given** a SQL screen (concept > threshold for a period) **When** run against the mart **Then** it returns the matching company-period rows.
6. **Given** a representative cross-sectional screen over the mart **When** run on the developer laptop **Then** it returns in single-digit seconds — a soft NFR-3 sanity target (regression tripwire), not a hard SLA.

## 🔑 Key design decision — recency-aware concept resolution (resolve BEFORE dev)

The Story 1.5 re-review found the current mart is **position-first** (`multiIf` picks the first-present element's value), which **ignores recency across elements** — a period restated under a *different* element in a *newer* filing, or a filing reporting both a subtotal and a total, can return the wrong value. AC-3 (corrected) now requires: **latest-filed across the union of a concept's elements, with element list-position as the deterministic tiebreak** (then the AD-7 filing tiebreak).

Why the current substrate can't just do it: `resolved_fact` stores `argMaxState(value, rank)` **per element** — it resolves recency *within* one element but **discards the winning `filed_date`/rank**, so the mart cannot compare recency *across* a concept's elements. A plain `argMaxMergeIf(value_state, element IN list)` in the mart *would* give latest-filed-across-union (fixing recency), but ties on the full rank `(filed_date, is_amendment, accession, version)` — i.e. two elements reported in the **same filing** — resolve nondeterministically (no element-position term). Full correctness needs recency **and** a position tiebreak.

**Two viable designs — pick one (I recommend B):**

- **Approach A — keep the MV central; retain the winning rank.** Add `rank_state = maxState((filed_date, is_amendment, accession, version))` to `resolved_fact` (alongside `value_state`). The wide mart, per concept, computes each element's `(winning_value = argMaxMerge, winning_rank = maxMerge)` then picks across elements by `argMax(value, (winning_rank, position_priority))`. Honors AD-8 (mart layers over the resolution MV). Cost: `resolved_fact` schema change (drop/recreate — it's derived, cheap) + a dictionary-generated cross-element argMax in the mart (more SQL).
- **Approach B (recommended) — resolve the concept in one `argMax` over Tier 1.** The wide mart is a view over `canonical_fact FINAL` where each concept column = `argMaxIf(value, (filed_date, toUInt8(endsWith(form,'/A')), accession, version, position_weight), canonical_concept IN (E1…En) AND unit='USD')`, grouped by `(cik, period_start, period_end)`. `position_weight` is a dictionary-derived per-element priority (earlier in list → higher). **One expression per column; fully correct** (recency across the union + deterministic position tiebreak in a single rank tuple). Simpler and exact.
  - **AD-8 implication (confirm):** Approach B has the mart resolve directly from Tier 1, leaving `resolved_fact`/`resolved_fact_mv` as an element-grained resolved layer for ad-hoc/element-level queries rather than the mart's source. AD-8 says the mart layers over the Resolution MV. Options: (i) accept B and demote `resolved_fact` to an element-level convenience (light AD-8 revision), or (ii) drop `resolved_fact`/MV entirely if nothing else needs it (bigger AD-8 revision), or (iii) take Approach A to keep AD-8 verbatim. **This is the decision to confirm before dev.** Performance: a cross-sectional screen scans `canonical_fact FINAL` (S&P 500 ≈ low-millions of rows) — expected well under NFR-3's single-digit seconds; AC-6 is the tripwire.

The dev cannot correctly build the mart without this decision. Everything else in the story is settled.

## Tasks / Subtasks

- [ ] **Task 0 — Confirm the resolution-design decision** (blocks Task 3) — A vs B above; record the choice + any AD-8 revision in the story Dev Notes and, if AD-8 changes, in `ARCHITECTURE-SPINE.md`.
- [ ] **Task 1 — Formalize the concept dictionary** (AC: 3) — `fintin/adapters/store/` (promote the Story 1.5 seed)
  - [ ] Move `CONCEPT_DICTIONARY` to a first-class, **versioned** artifact (a `concept_dictionary.py` module or a tracked data file owned by `adapters/store`, AD-18) — each concept = an ordered tuple of standard element local names + a `dictionary_version` string.
  - [ ] Seed each list from **observed frequency** (`SELECT canonical_concept, count(DISTINCT cik) FROM canonical_fact GROUP BY 1 ORDER BY 2 DESC`) and FASB-primary knowledge; **verify** each list (research aid, not authority — AD-9). Expand beyond the 4 seed columns to a reasonable v1 set (e.g. revenue, cost_of_revenue, gross_profit(if directly reported), operating_income, net_income, assets, current_assets, liabilities, current_liabilities, stockholders_equity, cash_and_equivalents, shares_outstanding) — keep it bounded; coverage grows by the FR-14 gap report.
  - [ ] Element names validated `^[A-Za-z0-9]+$` before any DDL interpolation (Story 1.5 guard); empty list → NULL column.
- [ ] **Task 2 — Latest-filed resolution correctness** (AC: 1, 2) — `fintin/adapters/store/schema.py`
  - [ ] Ensure per-(element, unit, period) resolution is latest-filed with the AD-7 tiebreak `(filed_date, is_amendment=/A, accession, version)` — this exists in `resolved_fact_mv`; keep/verify it (Approach A extends it with `rank_state`; Approach B relies on the same rank tuple inline).
- [ ] **Task 3 — Recency-aware wide mart** (AC: 3, 4) — `fintin/adapters/store/schema.py`
  - [ ] Rebuild `screening_mart` per the chosen approach so each concept column = latest-filed across its element union with position tiebreak (replaces the 1.5 position-first `multiIf`). Generated from the versioned dictionary. `CREATE OR REPLACE VIEW` (re-run `schema-init` to apply).
  - [ ] Preserve: NULL (not 0.0) when no element present; `unit='USD'` pin; one row per `(cik, period_start, period_end)`.
- [ ] **Task 4 — Tests (never live EDGAR; NFR-7; throwaway-DB pattern)** — `tests/`
  - [ ] **Restatement / SM-1 (REQUIRED, AC-2):** insert two filings of one period (different `filed_date`, differing values), assert the mart returns the newer value. Include the /A-amendment equal-`filed_date` tiebreak.
  - [ ] **Recency-across-union (AC-3):** a period reported under element E2 (list pos 2) in a NEWER filing and E1 (pos 1) in an OLDER filing → mart returns E2's newer value (proves recency beats position). AND a same-filing tie (E1+E2 same `filed_date`/accession, different values) → mart deterministically returns the position-1 element (proves the tiebreak).
  - [ ] Auto-population (AC-4), wide shape (one row per (cik,period)), NULL-not-zero, unit pin.
  - [ ] A representative SQL screen (concept > threshold) returns the expected company-period rows (AC-5).
  - [ ] NFR-3 tripwire (AC-6): a cross-sectional screen over a seeded multi-company set returns quickly (assert it completes; optionally log elapsed — keep it a soft check, not a flaky hard-time assert).
- [ ] **Task 5 — Validate & document**
  - [ ] `uv run pytest` green (unit + integration with ClickHouse up).
  - [ ] Update `README.md`: the mart now resolves latest-filed across each concept's element union; document the concept dictionary + how to extend it.
  - [ ] Reconcile the resolved deferred-work items (recency-aware resolution; the resolved_fact namespace-stripped-grouping discriminator per Finding 2 — address or explicitly re-defer with reason). Update `ARCHITECTURE-SPINE.md` if AD-8 changed (Task 0).

## Dev Notes

### Current substrate (Story 1.2 + 1.5, on main)

- `raw_fact` (Tier 0) → `canonical_fact` (Tier 1, `canonical_concept` = the standard element, 1:1 lossless, AD-9). Both `ReplacingMergeTree(version)` on the AD-5 identity key `(accession, raw_tag, period_start, period_end, unit)`; read via `FINAL`.
- `resolved_fact` = `AggregatingMergeTree`, `value_state = argMaxState(value, (filed_date, toUInt8(endsWith(form,'/A')), accession, version))` per `(cik, canonical_concept, unit, period_start, period_end)`. `resolved_fact_mv` auto-populates it on `canonical_fact` insert. [schema.py]
- `screening_mart` = wide view; the 1.5 **seed** `CONCEPT_DICTIONARY` (4 columns) resolves each via **first-present `multiIf`** — the position-first stopgap this story replaces. `_mart_column` already validates element names + guards empty lists.
- Live-verified: Apple's revenue spans 3 elements (`SalesRevenueNet`, `RevenueFromContractWithCustomerExcludingAssessedTax`, `Revenues`) across 2009→2026 — the exact multi-element-per-concept reality AC-3 must resolve correctly.

### Finding 2 (from the 1.5 re-review) to address here

`resolved_fact_mv` groups by the namespace-stripped `canonical_concept`, so `us-gaap:X`/`dei:X`/`srt:X` with the same local name (same unit+period, one filing) would collapse into one resolution key with no distinguishing rank term. Latent today (no dei/srt local-name twin among the dictionary columns; the seed is us-gaap-only). If Approach A is chosen, keep this in mind for the MV; if Approach B, the mart's `argMax` over `canonical_fact` already carries `raw_tag` context per row so add `raw_tag`/namespace to the tiebreak if a cross-namespace concept is ever curated. Re-defer with a note if not exercised in v1.

### Architecture constraints (authoritative)

- **AD-7** — resolved value = `argMax(value, filed_date)` on actual period dates; equal-`filed_date` tiebreak `/A` then greatest accession; deterministic. All filed versions retained. [ARCHITECTURE-SPINE.md#AD-7]
- **AD-8** — Resolution MV (`AggregatingMergeTree`, `argMaxState`) + wide mart (one row per `(cik, period)`, screening concepts as columns via the concept dictionary, first-present → **now recency-first with position tiebreak**); created before backfill inserts (AD-18); the concept dictionary is a versioned `adapters/store` artifact. [#AD-8] — **may be lightly revised by Task 0.**
- **AD-9** — `canonical_concept` = the standard element; screening concepts unified by the curated dictionary. [#AD-9]
- **AD-18** — `adapters/store` owns all DDL + the dictionary artifact; mart is `CREATE OR REPLACE VIEW` (re-run `schema-init` to apply on an existing DB — a documented manual step until the migration mechanism lands). [#AD-18]

### Project Structure Notes

- Pure DDL/query story — lives entirely in `fintin/adapters/store/` (schema.py + the new dictionary artifact). No `core`/`edgar`/`cli` changes required (a `screen` CLI is out of scope; screens are raw SQL against `screening_mart` in v1). No network anywhere.
- Reuse the `test_schema.py` throwaway-DB integration pattern; synthetic `canonical_fact` inserts (the `_COLS` VALUES pattern) — never live EDGAR.

### Previous Story Intelligence (1.5)

- `canonical_concept` is the element verbatim; dictionary element lists compare directly. `_mart_column` (element-name validation + empty-list guard) is the builder to extend/replace. The seed `CONCEPT_DICTIONARY` ordering (ASC-606 primary first) is a curation choice to verify/expand here.
- Mart-view changes need `schema-init` re-run on an existing DB (the 1.5 live lesson); integration tests build fresh so they don't catch a stale view — call this out in README.
- Idempotency/`FINAL`/version-monotonic patterns established; `OPTIMIZE … FINAL` used in tests to prove correctness survives a merge.

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story-1.6]
- [Source: ARCHITECTURE-SPINE.md#AD-7, #AD-8, #AD-9, #AD-18]
- [Source: fintin/adapters/store/schema.py#resolved_fact, #resolved_fact_mv, #screening_mart/CONCEPT_DICTIONARY/_mart_column]
- [Source: _bmad-output/implementation-artifacts/deferred-work.md#re-review-of-story-1.5 — recency-aware resolution; namespace discriminator]
- [Source: _bmad-output/implementation-artifacts/1-5-map-canonical-tier1.md — element-keyed Tier 1]

## Dev Agent Record

### Agent Model Used

### Debug Log References

### Completion Notes List

### File List
