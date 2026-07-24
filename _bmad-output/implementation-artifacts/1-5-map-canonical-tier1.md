---
baseline_commit: f411adb111726141df6683909689b43de98ab2f4
---
# Story 1.5: Map raw facts to canonical Tier 1 (standard-element concepts)

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the builder,
I want Tier 0 facts projected into Tier 1 keyed by their standard XBRL element, with zero network,
so that every fact is addressable by an exact, unambiguous standard concept.

## Acceptance Criteria

1. **Given** Tier 0 has a company's facts **When** I run the projection **Then** `canonical_fact` is populated with `canonical_concept` = the fact's standard element local name (e.g. `Assets`, `RevenueFromContractWithCustomerExcludingAssessedTax`) — a 1:1, lossless projection of `raw_tag` (namespace stripped), keyed by raw-fact identity, **issuing zero EDGAR/network requests** (FR-4, AD-4, AD-9).
2. **Given** every ingested fact is already `us-gaap`/`dei`/`srt` scope (AD-9/AD-15) **Then** every Tier 0 fact projects to exactly one Tier 1 row — no statistical standardization, no "unmappable" drop; `canonical_concept` is exact and unambiguous by construction, and each row carries `taxonomy_version` (carried over from Tier 0).
3. **Given** the projection is re-run **Then** it is an in-place upsert with no orphaned/duplicate rows on read (ingest-monotonic version; ReplacingMergeTree; FINAL) (AD-5, AD-6).

## Tasks / Subtasks

- [x] **Task 1 — Pure projection transform + orchestrator** (AC: 1, 2, 3) — `fintin/core/canonical.py` (NEW; edgar-free)
  - [x] `local_name(raw_tag)` — strip the namespace prefix (`us-gaap:Assets` → `Assets`); pure string logic.
  - [x] `CanonicalFactRow` NamedTuple in exact `canonical_fact` column order (`canonical_concept` after `raw_tag`; NO `taxonomy`); `ProjectResult` (`cik, raw_seen, projected, version`).
  - [x] `to_canonical_fact_rows(raw_rows, *, cik, version)` — project each `RawFactRow` 1:1: `canonical_concept = local_name(raw_tag)`; carry over `content_hash` and `taxonomy_version` from Tier 0; stamp `version`. Build by NAMED field (RawFactRow's `taxonomy` at idx 4 vs CanonicalFactRow's `canonical_concept` at idx 3 — a positional copy would corrupt).
  - [x] `map_company(cik, *, read_raw_facts, insert_rows, version)` — inject store ports; no adapter/edgar imports in core. AST import-guard test asserts no `edgar` in `core/canonical.py`.
- [x] **Task 2 — Store: read Tier 0, write Tier 1** (AC: 1, 3)
  - [x] `raw_fact_repo.read_raw_facts(client, cik) -> list[RawFactRow]` — `SELECT <RAW_FACT_COLUMNS in field order> FROM raw_fact FINAL WHERE cik = %(cik)s`, parameterized.
  - [x] `canonical_fact_repo` (NEW): `CANONICAL_FACT_COLUMNS`, `insert_canonical_facts`, `next_canonical_version` (`max(version)+1`, Tier 1's own ingest-monotonic sequence, AD-6).
- [x] **Task 3 — CLI trigger** (AC: 1, 2, 3) — `fintin/cli/app.py`: `map-canonical CIK`
  - [x] Lazy imports; validate CIK range (exit 2); `check_connection`; `next_canonical_version`; `map_company`; `raw_seen == 0` → yellow "run ingest-company first" (exit 1); close client in `finally`.
  - [x] **Imports NO `edgar`** — the projection path has no EdgarClient, no identity, no fetch. "Zero network" (AC-1) is structural by total absence.
- [x] **Task 4 — Seed concept dictionary in the mart** (supporting; unblocks the pipeline + Story 1.6) — `fintin/adapters/store/schema.py`
  - [x] `canonical_concept` is now the element, so the mart resolves each screening column via a `CONCEPT_DICTIONARY` (ordered element list) with **first-present `multiIf` precedence** (deterministic across synonymous elements — retires the nondeterministic-collision review finding). Seed lists: revenues `[RevenueFromContractWithCustomerExcludingAssessedTax, Revenues, SalesRevenueNet, RevenueFromContractWithCustomerIncludingAssessedTax]`, net_income `[NetIncomeLoss, ProfitLoss]`, assets `[Assets]`, liabilities `[Liabilities]`. Story 1.6 formalizes this into a versioned artifact + expands coverage.
- [x] **Task 5 — Tests (never hit live EDGAR; NFR-7)**
  - [x] `tests/test_canonical.py` (pure/offline): `local_name` strip; 1:1 projection + provenance carry-over; every-fact-projects-no-drops; named-construction guard; `map_company` wiring; AST import guard (no `edgar`).
  - [x] `tests/test_canonical_fact_repo.py` (integration): `read_raw_facts` round-trip; insert/read-back; `next_canonical_version` monotonic; **re-projection idempotency on read** (AC-3, across `OPTIMIZE … FINAL`); **end-to-end mart seam**; **first-present precedence determinism** (both `…ExcludingAssessedTax` and `Revenues` present → position-1 wins).
  - [x] `tests/test_schema.py` — `canonical_concept` synthetic inserts use element names; mart columns union elements.
  - [x] `tests/test_cli.py` — `map-canonical` help/invalid-CIK/missing-config error paths.
- [x] **Task 6 — Validate & document**
  - [x] `uv run pytest` green (unit + integration): **113 passed**.
  - [x] `README.md` pipeline section (`schema-init` → `ingest-company` → `map-canonical`, offline).
  - [x] `deferred-work.md` updated.

## Dev Notes

### The pivot (AD-9 correct-course, 2026-07-24)

The Story 1.5 code review found that edgartools' standardization taxonomy is a **learned/statistical** map (its own metadata: avg confidence ≈ 0.5 over 32,240 filings; 4% of mappings at confidence 1.0; ~80% of concepts fed by many tags) — it cannot be 100% accurate, and collapsing many us-gaap tags onto one fuzzy concept created a **nondeterministic mart value** (confirmed live: 1,229 Apple concept/period groups fed by >1 tag). Per the user's requirement (exact, universally-runnable standard concepts), AD-9 was revised:

- **`canonical_concept` = the standard XBRL element itself** (local name of `raw_tag`). Exact and unambiguous by construction — each element is FASB-defined and identical across filers. 1:1 lossless projection; no `edgar` in the path; every fact projects.
- **Cross-company screening concepts** = a **versioned concept dictionary** (AD-8): each concept is an ordered element list resolved by **first-present precedence** in the mart. This is where synonymous elements unify — deterministically. Story 1.5 ships an inline seed (the 4 headline concepts); Story 1.6 formalizes/expands it.
- edgartools standardization is demoted to a research aid (may seed candidate element lists), never the stored concept. `standardize.py` was removed.

Live proof on Apple: 24,852 Tier 0 → 24,852 Tier 1 (1:1), **505 exact element concepts**. Apple reports revenue under **three** elements across 2009→2026 (`SalesRevenueNet` 210, `RevenueFromContractWithCustomerExcludingAssessedTax` 113, `Revenues` 11) — the ordered dictionary unifies them; mart returns FY2025 revenue $416.16B / net income $112.01B, assets $371.08B / liabilities $264.59B.

### Architecture constraints (authoritative)

- **AD-9** — concept dimension = the standard element (1:1 lossless); comparability via the versioned concept dictionary. [Source: ARCHITECTURE-SPINE.md#AD-9]
- **AD-4** — Tier 1 rebuildable from Tier 0 with zero network; one-way derivation. The `map-canonical` path imports no `edgar`. [#AD-4]
- **AD-5** — Tier 1 identity key = `(accession, raw_tag, period_start, period_end, unit)`; `canonical_concept` is an attribute. `raw_tag` stays in the key, so projection is 1:1 with no dedup. [#AD-5]
- **AD-6** — `canonical_fact` is `ReplacingMergeTree(version)`; re-projection inserts at a higher `next_canonical_version`; readers use `FINAL`. [#AD-6]
- **AD-8** — mart columns = concept dictionary, first-present precedence; MV auto-fires on Tier 1 insert. Because `canonical_concept` is the element verbatim, there is no re-map column-retraction hazard. [#AD-8]
- **AD-18** — the mart seed dictionary lives in `adapters/store/schema.py` (sole DDL owner). [#AD-18]

### Project Structure Notes

- `core/canonical.py` is pure and edgar-free (projection is string logic). The only Tier-1 store code is `canonical_fact_repo` + `read_raw_facts`. `map-canonical` is a dumb CLI trigger with no `edgar` import.
- `canonical_fact` DDL (Story 1.2) unchanged; this story writes to it and issues no DDL except the mart `CREATE OR REPLACE VIEW` seed dictionary.

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story-1.5]
- [Source: ARCHITECTURE-SPINE.md#AD-4, #AD-5, #AD-6, #AD-8, #AD-9, #AD-18]
- [Source: fintin/adapters/store/schema.py#canonical_fact, #resolved_fact_mv, #screening_mart/CONCEPT_DICTIONARY]

## Review Findings

Code review of story-1.5 (2026-07-24). Blind Hunter (adversarial) completed; Edge Case Hunter + Acceptance Auditor subagents failed on infra and were run in-session. The review drove the AD-9 pivot below.

- [x] [Review][Resolved-by-pivot] Nondeterministic mart when several raw tags map to one concept — **retired**: `canonical_concept` is now the element (no many-to-one collapse); the mart resolves synonymous elements by deterministic **first-present precedence** over an ordered list. Verified by `test_first_present_precedence_deterministic`.
- [x] [Review][Resolved-by-pivot] `standardize.py` strips `srt:` and mis-maps srt line-items edgartools declines — **dissolved**: `standardize.py` was removed; no `get_standard_concept` call remains. Projection is `raw_tag` namespace-strip, and Tier 0 scope (AD-9/AD-15) already bounds the taxonomies.
- [x] [Review][Defer] `next_canonical_version` read-then-increment is not atomic [fintin/adapters/store/canonical_fact_repo.py] — deferred, single-writer v1 assumption (ties to AD-12 single-flight); recorded in deferred-work.md.
- [x] [Review][Defer] Ambiguous / manual-mart-refresh items — the ambiguous-tag finding is moot post-pivot (no statistical mapping); the mart-view-refresh limitation remains (existing DB needs `schema-init` re-run), recorded in deferred-work.md.

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Opus 4.8, 1M context)

### Debug Log References

- Full suite green after the pivot rework: `uv run pytest -q` → **113 passed** (0 skipped; ClickHouse up so integration ran).
- Live re-derivation on the real `default` DB (Tier 0 kept, derived tiers rebuilt): `map-canonical 320193` → 24,852 facts projected 1:1; 505 distinct element concepts; mart returns Apple FY2025 revenue $416.16B / net income $112.01B; assets $371.08B / liabilities $264.59B via the first-present dictionary.

### Completion Notes List

- **AC-1 (zero network):** the projection path imports no `edgar` at all — structural, not just behavioral. `canonical_concept = local_name(raw_tag)` (pure string). CLI builds no EdgarClient and needs no contact email.
- **AC-2 (every fact projects, exactly):** 1:1 lossless — no standardization, no drop; `canonical_concept` is the FASB element, exact by construction. `taxonomy_version` + `content_hash` carried over from Tier 0.
- **AC-3 (re-run upsert):** re-projection inserts at a higher `next_canonical_version` on the same identity key; `ReplacingMergeTree(version)` + `FINAL` collapse to one row, higher version wins — verified across `OPTIMIZE … FINAL`.
- **Pivot:** replaced edgartools standardization with element-identity canonical + a seed concept dictionary (first-present precedence) in the mart. Retired the nondeterministic-collision and srt-stripping review findings. `standardize.py` and `test_standardize.py` removed.

### File List

- `fintin/core/canonical.py` (REWRITTEN) — pure element projection (`local_name`, `to_canonical_fact_rows`, `map_company`); `CanonicalFactRow`, `ProjectResult`.
- `fintin/adapters/edgar/standardize.py` (DELETED) — no longer needed.
- `fintin/adapters/store/canonical_fact_repo.py` (NEW) — `insert_canonical_facts`, `next_canonical_version`.
- `fintin/adapters/store/raw_fact_repo.py` (MOD) — `read_raw_facts`.
- `fintin/adapters/store/schema.py` (MOD) — `CONCEPT_DICTIONARY` + first-present `multiIf` mart builder.
- `fintin/cli/app.py` (MOD) — `map-canonical` (no `edgar` import).
- `tests/test_canonical.py` (REWRITTEN), `tests/test_canonical_fact_repo.py` (REWRITTEN), `tests/test_standardize.py` (DELETED), `tests/test_schema.py` (MOD), `tests/test_cli.py` (MOD).
- `README.md` (MOD), `_bmad-output/implementation-artifacts/deferred-work.md` (MOD).
- Planning (separate commit): `ARCHITECTURE-SPINE.md`, `epics.md` (AD-9 pivot).

## Change Log

- 2026-07-24 — Story 1.5 implemented (edgartools standardization), reviewed, then **pivoted** (AD-9 correct-course): canonical concept = the standard element (1:1 lossless), cross-company comparability via a first-present concept dictionary in the mart. Removed `standardize.py`. 113 tests pass; verified live on Apple through the full mart. Status → review.
