---
title: Rubric-Walk Review — ARCHITECTURE-SPINE.md (fin-tin)
reviewer: rubric walker (good-spine checklist)
target: ../ARCHITECTURE-SPINE.md
altitude: feature / build-substrate (SOLO local tool)
date: 2026-07-23
verdict: adequate
---

# Rubric-Walk Review — fin-tin Architecture Spine

## Verdict: ADEQUATE (strong core, a few open operational-envelope seams)

The load-bearing invariants are genuinely strong. The hard divergence points a
story author would otherwise guess at — the write model, the Tier 1 key, the
latest-filed-wins computation, the mart-before-backfill ClickHouse gotcha, the
resumability grain, and the engine/trigger split — are all fixed crisply and
enforceably (AD-5, AD-6, AD-7, AD-8, AD-11, AD-2/AD-12). Named tech matches the
verified-current ground truth exactly. All 14 FRs are mapped. Deferred items are
mostly well-contained.

It does not reach **strong** because three real divergence points on the
*operational edge* are left open — the error/failure convention contradicts the
coverage-completeness success metric, the most ban-critical AD (AD-3) has an
enforceability soft-spot against the actual HTTP library, and there is no test /
fixture seam for EDGAR-touching code — plus one over-optimistic Deferred claim.
None are *broken*: nothing would make two units build fundamentally incompatible
things on the core. But they should be closed before epics/stories.

Calibrated to solo/local stakes: coordination-divergence risk is lower with one
dev, but the ban-critical items (AD-3 enforcement, AD-12 atomicity) still matter
regardless of team size — SM-C1 declares a ban "catastrophic."

Finding counts: **0 High / 4 Medium / 4 Low / 4 Nit.**

---

## Checklist walk

### 1. Does it fix the real divergence points for epics/stories, and miss none?

**Mostly yes — the data-model spine is excellent.** A story author has
unambiguous answers to: how writes happen (AD-6: insert-only
`ReplacingMergeTree(version)`, reads via `FINAL`/`argMax`, "must never assume a
background merge has run"); the Tier 1 key (AD-5:
`(accession, raw_tag, period_start, period_end, unit)`, concept/version as
attributes); resolution (AD-7: `argMax(value, filed_date)`, "Group on **actual
period dates**, never `fy`/`fp` labels"); mart shape and its critical build
ordering (AD-8: AggregatingMergeTree MV, "**The MV must be created before any
backfill insert**"); commit grain (AD-11: "**per-company** grain (never one
final write)"); and code placement (paradigm + source tree). These are the
decisions that most often diverge across units, and they are nailed.

**Missed / open divergence points** — see findings M1 (error vs coverage), L1
(test seam), L4 (coverage tier semantics). These are the ones an epic author
would still have to invent.

### 2. Is every AD's Rule enforceable, and does it prevent its stated divergence?

Most are. AD-5, AD-6, AD-7, AD-8, AD-11 are enforceable by construction and by
read/write review, and each genuinely blocks its "Prevents" clause. **AD-3 is
the exception** (finding M2): the rule can be satisfied on paper — every call
routed through `adapters/edgar` — while still exceeding 10 req/s, because
edgartools is the component that actually owns the socket. The rule as written
does not fully guarantee the outcome it exists for. **AD-12** has a smaller
enforceability gap (finding L2): it never mandates *atomic* lease acquisition,
so its "Prevents: concurrent runs doubling the EDGAR rate" can be defeated by a
TOCTOU race.

### 3. Could anything under Deferred let two units diverge?

Mostly well-contained. Re-map is Deferred and the spine correctly observes the
key/resumability machinery already supports it — **but the claim that it is
fully covered is over-optimistic** (finding M4): the AD-8 AMV mart cannot retract
a re-mapped fact's prior-concept contribution. Bulk-backfill (AD-13 interface),
cron/RSS/HTTP triggers (AD-2 wrappers), dynamic-membership (static config), and
the ad-hoc-repair Won't (AD-1 exception) are all cleanly bounded — no divergence.

### 4. Is named tech verified-current?

**Yes — exact match to the provided ground truth.** Python ≥ 3.12; ClickHouse
25.8 LTS; edgartools 5.43.0; clickhouse-connect 1.6.0; Typer 0.27.0; uv 0.11.31.
The Stack table, source-tree comments, and deployment mermaid all agree. No
stale or contradictory version appears anywhere in the doc. (One nit, N4: the
ClickHouse Docker image tag is described in prose as "25.8" but no pin
convention is stated for the compose file.)

### 5. Does it cover FR-1..FR-14?

**Yes — all 14 present** in the Capability→Architecture Map and in the `binds`
frontmatter, each with a home and a governing AD. Spot-checks: FR-5→AD-7,
FR-13→AD-8/AD-7, FR-10/11→AD-11/AD-12, FR-12→AD-1/AD-2. Coverage is complete at
the mapping level; two FRs have under-specified *semantics* (FR-2 tension in M3,
FR-14 in L4) rather than missing homes.

### 6. Is every dimension the altitude owns decided/deferred/open — especially the operational/environmental envelope?

Strong on deployment/environment: single-node laptop, ClickHouse 25.8 in Docker
Compose on a mounted volume, clickhouse-connect, filesystem lease not in
ClickHouse, single TOML config, "No secrets store (local)", structured stdout
logging, coverage report as a CLI command — all explicit in the deployment
mermaid and Consistency Conventions. **Two ops dimensions are under-owned:** the
failure/error envelope (M1) and the test/CI envelope (L1). Mart-rebuild
procedure and ClickHouse resource sizing are implied but unstated (N3) — fine to
defer for solo/local.

---

## Findings (severity-ranked)

### M1 — "Fail loudly" error convention contradicts FR-14 / SM-2 coverage-completeness `[MEDIUM]`
**Where:** Consistency Conventions → "Errors & status" ("Runs fail loudly except:
throttle … active run …"); vs PRD FR-14 ("any in-scope companies with zero
Facts") and SM-2 ("either successfully ingested or listed as an **explained
gap** … no silent omissions").

The error convention gives exactly two non-fatal cases (throttle cool-down,
`ALREADY_RUNNING`); **everything else fails loudly.** But a 500-company backfill
will meet companies that error transiently, error permanently, or legitimately
have zero mappable `us-gaap` facts. FR-14/SM-2 *require* the run to tolerate
those and record them as explained gaps — yet the spine gives no rule
distinguishing "company has no Facts" (report it) from "company fetch failed"
(fail? skip? record?). Two story authors will diverge: one aborts the whole run
on any per-company error (blocking completion over 500 companies), another
silently skips (violating "no silent omissions"). This also affects whether SM-2
is even achievable. **Fix:** add a per-company failure rule to the error
convention — e.g., a company fetch/parse failure is captured, the run continues,
and the company surfaces in the FR-14 coverage report as an explained gap;
distinguish it from a legitimate zero-Facts company. AD-11's per-company grain
already gives the natural seam.

### M2 — AD-3's throttle chokepoint is under-specified against the real HTTP client `[MEDIUM]`
**Where:** AD-3 ("Every EDGAR request goes through the single EDGAR client, which
enforces a configurable ceiling defaulting to 10 req/s … No direct HTTP to EDGAR
exists anywhere else").

AD-3 is the single most ban-critical invariant (SM-C1: a ban is "catastrophic"),
and the rubric test is whether the rule *actually prevents* its divergence
("uncoordinated requests → ban"). It does not fully guarantee that. edgartools —
not fintin — is the component that owns the socket; `get_filings(...)`,
`company.get_facts()`, and per-company backfill each may fan out into multiple
HTTP sub-requests (paginated index reads, per-accession fetches). A fintin-side
"client" that counts *high-level calls* can satisfy "all access routes through
one client" while the true request rate exceeds 10 req/s. The rule is satisfiable
on paper and still bannable. **Fix:** state that the ceiling is enforced at the
layer that owns the socket — i.e., configure edgartools' own throttle / identity
(`edgar.set_identity` is already cited) — rather than a wrapper that counts
method calls, and make "one rate-limited client" mean "one configured edgartools
session," not a hand-rolled counter above it.

### M3 — Backfill default is silently inverted vs PRD FR-7, with a live tension against FR-2 `[MEDIUM]`
**Where:** AD-13 ("v1 implements the **per-company `companyfacts` API** strategy …
The bulk `companyfacts.zip` strategy is … deferred"); vs PRD FR-7 ("The **default
path is the SEC bulk `companyfacts.zip` artifact**") and the architecture brief
§4 ("handled … via the bulk `companyfacts.zip`, never a crawl").

Choosing per-company for the S&P 500 subset is a *defensible* resolution of PRD
Open Question #2 ("bulk-artifact-and-filter vs per-company API for the S&P 500
subset") and is within the architect's remit — but the spine never says it is
resolving that open question, and it directly inverts FR-7's stated default and
the brief's "never a crawl" without acknowledgement. A story author
cross-referencing FR-7 sees "default = bulk zip" while AD-13 says "v1 =
per-company." Worse, FR-2's own testable consequence — "A full-Universe Backfill
does **not** issue one API call per company when a single bulk artifact covers
them" — reads as an argument *against* AD-13's v1 path (~500 per-company calls),
yet the Capability map still lists FR-2 as governed by AD-13. The rule itself is
unambiguous for implementation (AD-13 wins), so this is a traceability/consistency
defect, not an execution one. **Fix:** add one line to AD-13 noting it resolves
PRD Open Question #2 and supersedes FR-7's "default = bulk" for the S&P 500 scale,
and reconcile FR-2 (per-company at 500 CIKs is still request-minimizing vs a
per-*filing* crawl, and discovery still reads the index).

### M4 — Deferred re-map is claimed "already covered," but the AD-8 mart cannot retract a re-mapped contribution `[MEDIUM]`
**Where:** Deferred ("Re-map … the mechanism already exists (AD-5, AD-11), only
the CLI surface is deferred"); vs AD-8 (AggregatingMergeTree MV keyed by
`(cik, canonical_concept, period_start, period_end, unit)`, "auto-populates on
Tier 1 insert").

AD-5/AD-11 correctly make a re-map an idempotent in-place upsert *in Tier 1*. But
a re-map changes a raw fact's `canonical_concept` — which is part of the **mart
key**. A ClickHouse MV fires only on INSERT and aggregates incrementally; it
never retracts a source row that a later insert supersedes. So after a re-map,
the new contribution lands under the new concept while the old concept's
`argMaxState` row **retains the phantom contribution forever** (and, if it was
the sole fact, a mart row with no Tier 1 backing survives). Ordinary restatement
is fine (same key, `argMax` by `filed_date` self-heals) — the break is specific
to concept-changing re-map. Since re-map is Deferred (Could) and v1 does not
re-map, v1 is correct; the risk is that the "already covered" framing invites a
future author to build re-map on the existing mechanism and ship a silently-wrong
mart. **Fix:** note in Deferred (or AD-8) that re-map additionally requires the
mart to be rebuilt (drop + re-derive from Tier 1), because the AMV cannot retract
prior-concept aggregates.

### L1 — No test / fixture seam for EDGAR-touching code; SM-1 fixture set undefined `[LOW]`
**Where:** spine has no testing convention; PRD FR-1 ("verified by
construction/tests"), SM-1 ("a **fixed test set** of periods known to have been
restated"), SM-2.

For a build-substrate spine feeding stories whose acceptance is testable, the
absence of a test seam is a real (if solo-softened) divergence point: one story
tests EDGAR-touching code against live EDGAR (a ban risk in any loop/CI, directly
against SM-C1), another uses recorded fixtures — with no shared convention for
where the seam is or how the SM-1 restated-period fixtures are captured. The
hexagonal ports (AD-2) already give the natural seam (fake the EDGAR/store/lease
ports). **Fix:** one convention line — EDGAR is faked at the port in tests;
SM-1/SM-2 run against recorded fixtures, never live EDGAR.

### L2 — AD-12 does not require atomic lease acquisition (TOCTOU → double-run → ban) `[LOW]`
**Where:** AD-12 ("a **filesystem lease file with a TTL/heartbeat**"; "An expired
lease is reclaimed").

AD-12's stated purpose is preventing "concurrent runs doubling the EDGAR rate
(ban)." A naive check-then-write on a filesystem lease (or two triggers both
observing an *expired* lease and both reclaiming) reintroduces exactly that,
because the acquire is not atomic. Solo/local makes near-simultaneous triggers
rare, but the consequence is the catastrophic outcome AD-12 exists to prevent.
**Fix:** mandate atomic acquisition (`O_EXCL` create or atomic rename) so acquire
and reclaim are race-free.

### L3 — Inconsistent `[ADOPTED]` tagging with no legend `[LOW]`
**Where:** AD-6, AD-8, AD-12, AD-13 lack the `[ADOPTED]` marker that AD-1,2,3,4,5,
7,9,10,11,14 all carry; doc `status: draft`.

The tag tracks the memlog's constraint-vs-decision split, but the spine provides
no legend, so a reader cannot tell whether the four untagged ADs — which contain
the most operationally critical ClickHouse rules (insert-only RMT + `FINAL`
reads, AMV-before-backfill, the lease, the backfill strategy) — are binding or
still soft. In context they read as binding (all sit under "Invariants & Rules"
with Binds/Prevents/Rule), so impact is low, but the inconsistency invites doubt
about the firmest rules. **Fix:** tag all 14 consistently or add a one-line
legend for what the tag means.

### L4 — FR-14 coverage semantics not disambiguated across tiers `[LOW]`
**Where:** Capability map (FR-14 → "AD-10 (derived from DB)"); PRD FR-14 ("count
of in-scope companies present … any in-scope companies with **zero Facts**").

"Present" / "zero Facts" is ambiguous between Tier 0 presence, Tier 1 (canonical)
presence, and mart presence — a company can have Tier 0 facts but zero mappable
Tier 1 canonical facts (AD-9: "unmappable tags remain in Tier 0 and never enter
Tier 1"). Two authors will compute coverage against different tiers. **Fix:**
pin which tier defines "present" for the coverage report (likely Tier 0 presence
for ingestion coverage, with a separate canonical-coverage count).

### N1 — `cik UInt32` vs "zero-padded to 10 digits" `[NIT]`
Consistency Conventions: `cik = UInt32` but "zero-padded to 10 digits … for SEC
URLs." A 10-digit space (up to 10^10−1) exceeds UInt32's ceiling (~4.29e9).
Harmless today — assigned CIKs are ~7 digits — but the framing overstates the
range the type can hold; worth a note that UInt32 is safe because real CIKs are
far below the ceiling.

### N2 — AD-3 drops FR-1's "Retry-After honored if present" nuance `[NIT]`
AD-3 states flatly "no SEC status code / `Retry-After` is assumed," whereas PRD
FR-1 says a "`Retry-After` header is honored if present, but the client does not
depend on one." Minor loss of nuance; no divergence risk, but the "honor if
present" behavior silently drops out of the binding artifact.

### N3 — Mart-rebuild procedure and CH resource sizing unstated `[NIT]`
The mart is derivable from Tier 1 (AD-4/AD-8) but no drop-and-re-derive procedure
is written; ClickHouse memory/volume sizing is unspecified. Acceptable to defer
for solo/local; worth a one-liner given the mart is an MV that only sees
post-creation inserts.

### N4 — Backfill strategy-selection location not pinned in hexagonal terms `[NIT]`
AD-13 puts "backfill strategies" in `adapters/edgar` while selection is "by
Universe size"; the spine does not say whether strategy *selection* is a core
policy or an adapter detail. Minor for one dev.

---

## What's strong (keep)

- **The derivation invariants** (AD-4→AD-8) are a coherent, enforceable spine:
  two-tier one-way recovery, raw-fact-identity key, insert-only + `FINAL`/`argMax`
  reads, latest-filed-wins on actual dates, AMV mart. This is the hard part and
  it is done well.
- **The two ClickHouse gotchas that most often burn teams are captured as
  rules:** "Readers **must** use `FINAL` or an `argMax`" (AD-6) and "**The MV must
  be created before any backfill insert**" (AD-8). Excellent altitude judgment.
- **AD-1 + AD-11's "no checkpoint file; the DB is self-checkpointing"** is a
  clean, testable through-line, with the single documented exception (ad-hoc
  repair) correctly quarantined to Won't.
- **Version hygiene is exact** and consistent across Stack table, source tree,
  and deployment diagram.
- **The hexagonal dependency diagram is well-formed** (all adapters + triggers
  depend inward on core), and the source tree matches it 1:1.
- **Correct tightening vs the brief:** the brief hoards `ifrs-full`; the spine
  (AD-9) drops it to `us-gaap/dei/srt` to match the PRD's IFRS non-goal.

## To close before epics/stories

1. Add a per-company failure rule reconciling "fail loudly" with FR-14/SM-2 (M1).
2. Specify AD-3's ceiling is enforced at edgartools' layer, not a call-count
   wrapper (M2).
3. Note AD-13 resolves PRD Open Q#2 / supersedes FR-7's default, and reconcile
   FR-2 (M3).
4. Flag that deferred re-map also requires a mart rebuild (M4).
5. Add a one-line test-seam convention (EDGAR faked at the port; fixtures for
   SM-1/SM-2) (L1).

These are additive clarifications; none require re-architecting. The spine's core
is sound.
