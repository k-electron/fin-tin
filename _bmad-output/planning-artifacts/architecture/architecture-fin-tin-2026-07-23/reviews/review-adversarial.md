---
name: review-adversarial
type: architecture-review
target: ARCHITECTURE-SPINE.md (fin-tin)
stance: adversarial / integration-hazard hunt
reviewer: adversarial architecture reviewer
created: 2026-07-23
---

# Adversarial Architecture Review — fin-tin Spine

## Method & Verdict

I took the spine at its word and tried to build it twice. For each shared entity,
owner, and mutation path I constructed **two concrete units one level down** (two
modules / two stories) that each satisfy **every AD to the letter** — then checked
whether the two halves link. Where they silently produce different bytes, a
different schema, or lost data, the spine has a hole: the ADs under-determine the
build.

**Verdict: NOT integration-safe as written.** The dependency and derivation
*directions* are sound (Hexagonal + one-way tier pipeline are well chosen and
un-attackable at this altitude). But the spine specifies **directions and identities
without specifying the physical shapes and single owners those identities imply.**
Six holes are load-bearing: two independent, AD-compliant builders will produce
stores that cannot share data, and in three cases one of the two builds **silently
loses or corrupts data** while passing every stated rule. Twelve findings total,
ordered by blast radius. Each ends with the new or tightened AD that closes it.

A note on fairness: I checked the PRD (FR-1..FR-14) and the memlog before flagging
anything. Every hole below is unresolved at PRD level too — none is a case of the
spine deliberately deferring detail to a lower artifact that already pins it.

---

## CRITICAL — data-shape / data-loss holes

### H1 — Tier 0 has no identity key, and dimensional (segmented) facts collapse

**Units.**
- *Module A — `adapters/store` schema author (story: "create `raw_fact` table").*
  AD-4 says Tier 0 is "keyed by accession"; AD-6 says Tier 0 is a
  `ReplacingMergeTree(version)` "sorted by their identity key." The only identity
  key the spine ever writes down is Tier 1's (AD-5). So A mirrors it and sets the
  `raw_fact` sort key = `(accession, raw_tag, period_start, period_end, unit)`.
- *Module B — `adapters/edgar` ingestion writer (story: "land companyfacts into Tier 0").*
  B reads AD-4's mandate to hoard **all** standard-taxonomy facts (FR-3: "retaining
  all such Facts, not only those currently mappable") and knows that a single 10-K
  reports `us-gaap:Revenues` many times in one period under different segment axes
  (geography, product, member). To preserve them, B's row identity must include a
  `segment`/`dimensions` discriminator.

**Collision.** With A's key, every segmented instance of Revenue for one period
collapses to a **single** `ReplacingMergeTree` row — the store keeps one arbitrary
segment and drops the rest, directly violating "hoarding all standard-taxonomy facts"
while fully complying with the only key the spine wrote down. With B's key, Tier 0
has a column A's schema never created; the reader/mapper joining Tier 0 → Tier 1
sees a different table shape. Worse, the collapse propagates: AD-5's Tier 1 identity
`(accession, raw_tag, period_start, period_end, unit)` **also** has no segment
dimension, so even a correct Tier 0 cannot map segmented facts into Tier 1 without
key collision — two builders resolve this oppositely (map only the consolidated/
undimensioned fact vs. map every member and let them argMax-collide into one
nondeterministic "Revenue").

**Close it.** New **AD-15 — Fact dimensionality is explicit and consolidated-only in
v1.** Define Tier 0's physical identity/sort key explicitly (it is *not* accession
alone). State that v1 ingests **only the consolidated / undimensioned fact** (no
segment/axis members) and that both Tier 0 and Tier 1 identity keys therefore need
no dimension column — OR, if members are retained, add an explicit `dimensions`
(canonicalized member string) term to **both** AD-4's Tier 0 key and AD-5's Tier 1
key. Pick one; the spine currently implies neither and permits both.

---

### H2 — `ReplacingMergeTree(version)`: the `version` column is undefined, and recovery vs. latest-filed impose opposite requirements on it

**Units.**
- *Story A — "latest-filed-wins dedup" (store).* Reads AD-7 ("latest-**filed**-wins")
  and AD-6 together and sets the ReplacingMergeTree version expression =
  `filed_date`. Rationale: the newest filing should win the merge. Fully AD-6/AD-7
  compliant on its face.
- *Story B — "Tier 0 recovery re-ingest" (`adapters/edgar` + core, FR-6/AD-14).*
  Assumes AD-14's guarantee that re-ingesting a corrupted accession "supersedes a
  corrupted copy" (idempotent). For supersession on merge, the fresh copy must carry
  a **higher** version than the corrupt one. It sets version = an ingest-monotonic
  value (wall-clock ingest time or a counter).

**Collision.** A re-ingested accession has the **same** `filed_date` (filed_date is
an immutable property of the filing, AD-5 lists it as an attribute; a restatement is
a *new* accession, not a re-file — PRD Restatement definition). So under Story A's
choice (`version = filed_date`), the recovered row has a version **equal** to the
corrupt row → `ReplacingMergeTree` keeps an arbitrary one of the two → **recovery
silently fails to overwrite corruption.** AD-14's core promise is unbuildable. Under
Story B's choice, recovery works, and latest-filed-wins is still correct because
AD-7 does it at **read** time via `argMax(value, filed_date)` — the merge version is
orthogonal to it. The two stories cannot coexist: they need different version
columns, and the spine names none. (Note the spine's own text conflates the two —
AD-6 pairs "insert-only … ReplacingMergeTree" with no version definition, while AD-7
says "latest-filed-wins," inviting exactly Story A's wrong inference.)

**Close it.** Tighten **AD-6**: the `version` expression is an **ingest-monotonic
value** (e.g. an ingest-sequence / `insert_time`), never `filed_date`. State
explicitly that ReplacingMergeTree collapses *identical-key re-ingests* (idempotency
+ recovery supersession), while latest-**filed**-wins is a **read-time** `argMax`
(AD-7) and is *not* the merge's job. One sentence removes the contradiction.

---

### H3 — AD-10 global `max(filed)` high-water mark + AD-11 per-company commit = silent data loss on resume (and it collides with FR-2)

**Units.**
- *Story A — "derive delta from the high-water mark" (core reconciler).* AD-10:
  "Work = filings filed since `SELECT max(filed)` up to today … The high-water mark
  is a query." A issues one index query `get_filings(filing_date = max(filed)…today)`
  over the whole EDGAR index, filters to the Universe, and processes. This is also
  the **request-minimal** reading demanded by FR-2 (one index call, not 500).
- *Story B — "per-company idempotent commit" (core, AD-11/FR-10).* Commits at
  per-company grain: finishes company A (whose newest new filing was filed day *D*),
  then crashes before company B (whose new filing was filed day *D−5*).

**Collision.** After B's crash, `SELECT max(filed)` returns **D** (from committed
company A). Story A's next run queries `get_filings(D…today)` and **never sees**
company B's day *D−5* filing — it is earlier than the advanced high-water mark. The
"minus accession membership" subtraction can't rescue it because the candidate
window never includes the missing accession. Result: a permanent silent gap until
some unrelated future filing drags the window back — a direct violation of SM-3
("no Filing filed at or before the run's start remains un-ingested"), produced by
two units that each obey AD-10 and AD-11 exactly. The safe alternative (per-company
`max(filed)` + per-company `get_filings`) is resume-correct but issues ~500 index
calls, colliding with **FR-2 / AD-3** request minimization — so the two builders
pick opposite sides of an unstated tradeoff.

**Close it.** New **AD-16 — The delta boundary is per-accession membership over a
reordering-safe window; `max(filed)` is only a narrowing hint.** Specify that the
candidate window is `[max(filed) − LOOKBACK, today]` (with `LOOKBACK` a config value
≥ the largest plausible cross-company filed-date reordering, or effectively per-CIK
`max(filed)`), and that **correctness comes from subtracting per-accession Tier 0
membership over that whole window** — never from `max(filed)` as a bare cursor.
Alternatively adopt an explicit **per-CIK high-water mark** and accept the request
cost. State which, and give `LOOKBACK` a default.

---

### H4 — "Accession stored as its canonical string" — canonical form undefined; the two SEC forms produce non-matching keys

**Units.**
- *Story A — Tier 0 writer.* edgartools' filing object exposes the dashed accession
  `0000320193-24-000123`; A stores that (it is "the canonical string").
- *Story B — membership / work-derivation (AD-10 "minus accession membership") and
  recovery scoping (AD-14 "re-ingest the target accession").* Builds accession
  values from an 18-char undashed form `000032019324000123` (also common in SEC
  URLs / paths) and compares against Tier 0.

**Collision.** Accession is part of Tier 0's key (AD-4) and Tier 1's identity (AD-5),
and is the join between them and the unit of recovery. Dashed vs. undashed are
different strings → the membership check (AD-10) never matches → catch-up
**re-ingests filings it already has** (or, symmetrically, recovery targets a key
that doesn't exist and no-ops). Both units satisfy the convention "Accession stored
as its canonical string" because "canonical" is never pinned.

**Close it.** Tighten the **Identity convention**: accession canonical form = the
dashed 20-character `nnnnnnnnnn-nn-nnnnnn` exactly as edgartools returns it;
normalize on ingest. One line.

---

### H5 — Instant vs. duration facts: no rule for representing an instant period; keys and mart grain diverge

**Units.**
- *Story A — mapper (Tier 0 → Tier 1).* Balance-sheet facts (Assets, cash) are XBRL
  **instant** facts — a single date, no start. A represents them as
  `period_start = period_end = instant_date`.
- *Story B — mart / reader (AD-7, AD-8).* Represents instants as `period_start` =
  a sentinel (epoch `1970-01-01`, since the convention forbids `Nullable(Date)` —
  it says `period_start … = Date`) and `period_end = instant_date`.

**Collision.** `period_start`/`period_end` are in **both** the Tier 1 identity
(AD-5) and the mart grain (AD-8) and the AD-7 read key. The same balance-sheet fact
lands under two different keys depending on which unit wrote it → the mart cannot
join Tier 1 to itself; latest-filed-wins for balance-sheet concepts silently splits
into two period buckets that never argMax against each other. Both comply: the spine
says periods are `Date` and gives no instant/duration rule.

**Close it.** New **AD-17 — Instant facts are represented as
`period_start = period_end = instant_date`** (duration facts keep distinct
start/end). State it once; it fixes the key, the mart grain, and the AD-7 read key
simultaneously. (If NULL-start is ever preferred, the convention must switch to
`Nullable(Date)` — but pick one now.)

---

### H6 — Two raw tags → one canonical concept in one filing → mart `argMax` tie is nondeterministic

**Units.**
- *Story A — mapper (AD-9).* edgartools standardizes both `us-gaap:Revenues` and
  `us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax` to the canonical
  "Revenue." A dutifully writes **both** Tier 1 rows — they are distinct identities
  under AD-5 (different `raw_tag`), fully compliant.
- *Story B — mart MV (AD-8).* Aggregates `argMaxState(value, filed_date)` per
  `(cik, canonical_concept, period_start, period_end, unit)`. Both of A's rows share
  the same cik, concept, period, unit — **and the same `filed_date`** (one filing).

**Collision.** `argMax(value, filed_date)` with a **tied** `filed_date` is
nondeterministic in ClickHouse → the mart returns one of two different Revenue
numbers unpredictably, and can flip between merges. AD-7's read key
`(cik, canonical_concept, unit, period_start, period_end)` has the identical defect.
Neither AD provides a within-filing tie-break, and AD-5 explicitly *permits* multiple
raw tags per concept (that is the whole point of standardization). This is a
correctness bug that passes every rule.

**Close it.** New **AD-18 — Deterministic tie-break when multiple facts share the
resolution key.** Define the total order for both the AD-7 read and the AD-8 mart:
`argMax` on `(filed_date, <tie-break>)` where the tie-break is an explicit,
documented rule — e.g. a raw-tag precedence list from the taxonomy, or "prefer the
fact whose `raw_tag` equals the taxonomy's canonical source tag," else largest
ingest-version. Without it the mart's "one comparable row per company-period" is a
coin flip.

---

## HIGH — ownership / ordering holes

### H7 — Nobody owns re-deriving the mart when Tier 1 is rebuilt or recovered

**Units.**
- *Story A — recovery / Tier 1 rebuild (AD-4 "rebuildable from Tier 0 with zero
  network"; AD-14).* Re-derives Tier 1 by re-inserting corrected rows (insert-only,
  AD-6).
- *Story B — mart owner (AD-8).* The mart is an `AggregatingMergeTree` MV that
  "auto-populates on Tier 1 insert."

**Collision.** The MV fires on Tier 1 **inserts**, so a recovery insert with a
*corrected* value at the **same `filed_date`** as the corrupt value adds a second
`argMaxState` cell with a tied filed_date → `argMaxMerge` may still surface the
**corrupt** value. The mart does **not** self-heal from a Tier 1 fix, yet AD-8
declares it merely "auto-populates" and AD-4/AD-14 assume rebuild is sufficient. No
unit owns rebuilding/truncating the mart on recovery. Two builders: A assumes the
mart self-heals; B assumes Tier 1 is append-only-correct. Neither rebuilds the mart.

**Close it.** Tighten **AD-8 + AD-14**: state that Tier 0/Tier 1 recovery **must
also re-derive the affected mart partition** (mart is not self-healing against
same-`filed_date` supersession), and name the owner (`adapters/store`) and the
mechanism (drop/rebuild the affected `(cik, period)` mart partition, or route the
correction through H2's ingest-version tie-break so `argMax` prefers the recovered
cell).

---

### H8 — Schema-creation ownership and the AD-8 "MV before backfill" ordering are unowned across two entry points

**Units.**
- *Story A — `adapters/store` migration (creates `raw_fact`, `canonical_fact`,
  `screening_mart` MV).*
- *Story B — `fintin backfill` command (`cli` + `adapters/edgar`).* On a fresh DB,
  B lazily `CREATE TABLE IF NOT EXISTS` the tables it writes to (`raw_fact`,
  `canonical_fact`) and starts inserting.

**Collision.** AD-8: "The MV must be created **before any backfill insert**
(ClickHouse MVs do not backfill pre-existing rows)." If B runs before A (a user runs
`backfill` on a clean DB) and B only ensures the tables *it* writes, the mart MV is
created **after** the backfill rows exist → the mart is **permanently empty for all
backfilled history** — exactly the failure AD-8 warns about, reached by two units
that each comply (A creates the MV first *when it runs*; B never claimed to own the
MV). Ownership of "MV exists before first insert" is split and therefore held by
no one.

**Close it.** New **AD-19 — Single owned schema-migration gate.** `adapters/store`
owns one idempotent migration that creates **all** objects (including the mart MV)
and stamps a schema version; **every** ingestion command asserts the schema gate has
run before its first insert. Backfill never creates tables lazily.

---

### H9 — `content_hash`: algorithm, byte-input, and grain all undefined

**Units.**
- *Story A — Tier 0 writer (AD-14: "every fact carries … `content_hash`";
  `content_hash` detects at-rest corruption).* Computes a **per-fact** hash over
  the normalized `(value, unit, period)` tuple.
- *Story B — recovery / scrub reader (AD-14; Deferred integrity scrub).* Expects a
  **per-accession** hash over the raw filing bytes, to detect that a stored
  accession's landed content diverged from EDGAR.

**Collision.** AD-5 lists `content_hash` as a Tier 1 **attribute**; AD-14 lists it
as a per-fact field but says it detects **at-rest** (document-level) corruption —
the two ADs already point at two grains. A per-fact hash and a per-accession hash
can never be compared; the corruption-detection promise is unbuildable across the
two units. Algorithm (sha256 vs md5) and canonical byte serialization
(float formatting! `1.0` vs `1`) are also unspecified — even two per-fact
implementations won't agree.

**Close it.** Tighten **AD-14**: define `content_hash` = `<algo>` over a
**canonically-serialized** input at a **named grain** (recommend per-accession raw
bytes for at-rest detection, stored denormalized on each fact), including the exact
value/number normalization. Without the byte-level spec, the hash is decorative.

---

### H10 — `taxonomy_version`: the edgartools standardization version, or the filing's source us-gaap year?

**Units.**
- *Story A — mapper.* AD-9: "canonical concepts come solely from the edgartools
  standardization taxonomy … stamped with its `taxonomy_version`." A stamps the
  **edgartools package/standardization version** (e.g. `5.43.0`) — the thing that
  produced the mapping.
- *Story B — provenance / re-map (AD-5 "re-map (taxonomy vX→vY)"; AD-14).* Stamps the
  filing's **source XBRL taxonomy year** (e.g. `us-gaap-2023`) — a property of the
  raw fact.

**Collision.** "Its" in AD-9 is ambiguous (the standardization's version vs. the
fact's source taxonomy). The two builders write different strings into the same
column; the deferred re-map command (AD-5) means opposite things (re-run the mapper
vs. reconcile source taxonomies), and cross-company comparability provenance points
at the wrong thing. Both comply.

**Close it.** Tighten **AD-9**: `taxonomy_version` = the **edgartools
standardization taxonomy version** that produced the canonical concept (re-map =
re-running standardization at a new edgartools version). If the source us-gaap year
is also wanted, it is a *separate* named column.

---

### H11 — The lease file path is undefined → single-flight is defeated

**Units.**
- *Story A — `adapters/lease`.* AD-12: "filesystem lease file with a TTL/heartbeat."
  Places it at `./fintin.lock` (relative to cwd).
- *Story B — a second invocation / a cron wrapper (AD-2 dumb trigger)* launched from
  a different working directory, or a builder who places the lease at
  `$XDG_RUNTIME_DIR/fintin.lease`.

**Collision.** Two invocations resolve **different lease paths** → both acquire
"the" lease → two concurrent runs → **2× the EDGAR rate → ban**, precisely the
outcome AD-12 exists to prevent. The config convention lists Universe/rate/identity/
CH connection but **not** the lease path, so it's builder's choice.

**Close it.** Tighten **AD-12 + config convention**: the lease is a single
**absolute** path from config (default e.g. `~/.fintin/fintin.lease`), resolved
independently of cwd. Add it to the TOML schema.

---

### H12 — Lease TTL and heartbeat interval (and their ratio) undefined → premature reclaim → concurrent runs

**Units.**
- *Story A — lease holder (engine).* Heartbeats every 20 s; TTL 60 s.
- *Story B — lease reclaimer (a second invocation checking for an expired lease,
  AD-12: "an expired lease is reclaimed and its partial work resumed").* Considers a
  lease dead after its own notion of TTL.

**Collision.** If A's TTL is short (60 s) but a per-company commit + ClickHouse
insert (or a 10-minute AD-3 throttle cool-down!) stalls the heartbeat past TTL, B
reclaims a **live** lease → two concurrent runs. If B's TTL is long (1 h), a genuinely
crashed run blocks all work for an hour. AD-12 names "TTL/heartbeat" but no values
and no relationship between them — and critically doesn't reconcile the TTL against
AD-3's **≥10-minute cool-down**, during which the holder legitimately makes no
progress.

**Close it.** Tighten **AD-12**: define TTL and heartbeat interval in config with
defaults and the invariant `TTL ≥ 3 × heartbeat`, and `TTL` **>** the AD-3 cool-down
(≥ ~15 min) so a throttled-but-alive holder is never reclaimed. Name what counts as a
heartbeat during a long per-company loop.

---

## MEDIUM — resolvable ambiguities worth pinning

### H13 — Ticker→CIK resolution: through the rate-limited client? before or after the lease?

AD-13: "tickers resolve to CIKs via edgartools **at load**." AD-3: **all** EDGAR
access goes through the one rate-limited client. AD-12: single-flight guards the run.
Two units: (A) the config loader resolves tickers via a bare `Company('AAPL')`
edgartools call **at config load, before the lease is acquired** — plausibly a
network hit to `company_tickers.json`, outside the throttled client and outside
single-flight; (B) resolution is routed through the client, inside the lease.
Under A, two simultaneous invocations both resolve before either acquires the lease
→ uncoordinated EDGAR traffic (AD-3 breach) that single-flight was supposed to
prevent. **Close it:** tighten AD-3/AD-13 — ticker→CIK resolution either uses a
cached/local mapping, or goes through the rate-limited client *after* the lease is
held; declare whether `company_tickers.json` counts as "EDGAR access" (it does).

### H14 — "Throttle-failure" detection is undefined (AD-3 assumes no status code)

AD-3: "≥10-minute cool-down on a throttle-failure (**no SEC status code /
`Retry-After` is assumed**)." Then *what* triggers the cool-down? Unit A treats any
error (timeout, connection reset, 403, 503) as throttle → cools down 10 min on every
transient blip → backfill crawls. Unit B only cools down on a specific signal → never
detects a real soft-ban → gets banned. **Close it:** tighten AD-3 with the explicit
trigger condition (e.g. HTTP 403/429 *or* N consecutive failures within a window) and
distinguish it from ordinary transient-retry.

### H15 — Config Universe schema: mixed ticker/CIK types are unparseable

Convention: "Universe (CIK/ticker list)"; AD-13: "config list of CIKs; tickers
resolve … at load." So the list may mix `"AAPL"` and `320193`. Unit A treats every
entry as a ticker; unit B treats numeric-looking entries as CIKs — and disagree on
whether `"320193"` is a CIK or a ticker, and whether a CIK is an int or a
zero-padded string. **Close it:** define the TOML Universe schema explicitly —
separate `tickers = [...]` and `ciks = [...]` keys (or typed table entries), CIK as
bare integer, with a stated resolution/merge rule.

### H16 — `cik` placement: denormalized column vs. a filing dimension table

AD-5's Tier 1 identity has **no `cik`**, yet AD-7 reads and AD-8's mart key both
require `cik`. The Structural Seed's ER shows `COMPANY`/`FILING`/`RAW_FACT` entities,
but the source tree's schema names only three tables (`raw_fact`, `canonical_fact`,
`screening_mart`) — no filing/company table. Unit A denormalizes `cik` as a column on
`raw_fact`/`canonical_fact`; unit B expects a `filing`→`company` join to supply `cik`
to the MV. The MV can't be built against B's missing table. **Close it:** state that
`cik` is a **denormalized column** on `raw_fact` and `canonical_fact` (v1 has no
separate filing/company table); the mart MV reads it directly.

### H17 — "One client" — one instance, or one shared limiter? (parallel backfill)

AD-3 says "the single EDGAR client" enforces 10 req/s. To use the budget, a backfill
builder may spin a thread pool. Unit A shares **one client instance** (one token
bucket) across threads → correct 10 req/s. Unit B instantiates a client per worker,
each "enforcing 10 req/s" → aggregate 10×N → ban. Both read "one rate-limited
client" as satisfied. **Close it:** tighten AD-3 — the rate limiter is a single
**process-global shared token bucket** (one client instance, injected), and the
10 req/s ceiling is enforced across all concurrency, not per-instance.

---

## What is solid (not attackable at this altitude)

- The **dependency direction** (Hexagonal: adapters/triggers → core ports; core →
  nothing) and the **one-way derivation** (EDGAR → T0 → T1 → mart, recovery only
  leftward) are internally consistent and leave no room for two units to point
  dependencies opposite ways.
- **AD-1** (derive, don't store cursors) + **AD-10** (HWM is a query) are a coherent
  pair *in principle* — the hole (H3) is in the physical boundary, not the principle.
- **AD-2** (pure engine, throttle+single-flight inside) cleanly prevents triggers
  from diverging on policy.
- The **stack pins** (ClickHouse 25.8, edgartools 5.43.0, clickhouse-connect 1.6.0,
  Typer 0.27.0, Python ≥3.12) are exact — no version drift between units.

The pattern across all twelve findings is one thing: **the spine specifies
identities, directions, and ownership *intentions*, but not the physical byte-shapes,
the merge/version semantics, or the *single named owner* those identities imply.**
Closing H1–H6 (six new/tightened ADs: AD-15 dimensionality, AD-6 version semantics,
AD-16 delta boundary, accession canonical form, AD-17 instant representation, AD-18
resolution tie-break) is the minimum to make two independent builders produce a
store that links.
