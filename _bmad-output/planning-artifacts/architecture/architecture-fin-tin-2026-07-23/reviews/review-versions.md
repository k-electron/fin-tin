# Reality-Check Review — Technology & Version Decisions

**Artifact reviewed:** `ARCHITECTURE-SPINE.md` (fin-tin, created 2026-07-23)
**Reviewer role:** Reality-check reviewer (verify every committed technology decision was web-researched / reality-checked, not asserted from training data)
**Review date:** 2026-07-23
**Method:** Authoritative PyPI JSON API (`info.version` field, not the WebFetch summarizer, which proved unreliable and echoed prompt values); the edgartools 5.43.0 **source distribution was downloaded and its source grepped directly**; ClickHouse feature/behavior claims cross-checked against official docs and current release/EOL trackers.

---

## Overall Verdict

**PASS with one caution.** Every named technology is real, current, actively maintained, and supports the exact way the spine uses it. All four Python package versions match the current PyPI `latest` **exactly**, and all load-bearing technical claims (ReplacingMergeTree/FINAL/argMax, AggregatingMergeTree MV with argMaxState/argMaxMerge + no-backfill caveat, and the entire edgartools API surface including `to_dataframe(pit_mode=True)`) are **confirmed against primary sources**. This is clearly web-researched, not asserted from training data — the strongest tell being `clickhouse-connect 1.6.0` (training-era knowledge would have said 0.8.x) and `edgartools 5.43.0`, both of which are correct for the current registry.

The **one caution** is the ClickHouse server version: `25.8 LTS` is real and (barely) still supported, but it reaches **end-of-support on 2026-08-29 — roughly 5 weeks after this spine was written** — and a newer `26.3 LTS` has been available since March 2026. See item below.

---

## Stack Table — Version-by-Version

| Technology | Spine says | Verified (2026-07-23) | Verdict |
| --- | --- | --- | --- |
| Python | `>= 3.12` | Compatible with every pinned dep (see below) | **PASS** |
| ClickHouse (server) | `25.8 (LTS)` | Real LTS; **EOS 2026-08-29**; newer `26.3 LTS` exists | **PASS (caution — near-EOL LTS)** |
| edgartools | `5.43.0` | PyPI `latest = 5.43.0`, uploaded 2026-07-19 | **PASS (exact match)** |
| clickhouse-connect | `1.6.0` | PyPI `latest = 1.6.0`, uploaded 2026-07-23 | **PASS (exact match)** |
| Typer | `0.27.0` | PyPI `latest = 0.27.0`, uploaded 2026-07-15 | **PASS (exact match)** |
| uv (tooling) | `0.11.31` | PyPI `latest = 0.11.32` (0.11.31 real, one patch behind) | **PASS (trivially behind)** |
| Docker Compose | host-provided | n/a (no version committed) | n/a |

### Python version compatibility (cross-checked against each dep's `requires_python`)
- edgartools 5.43.0 → `>=3.10` (wheels advertise 3.10–3.14)
- clickhouse-connect 1.6.0 → `>=3.10,<3.15`
- Typer 0.27.0 → `>=3.10`
- uv 0.11.31/0.11.32 → `>=3.8`

`Python >=3.12` sits inside every window. Note clickhouse-connect caps at `<3.15`; 3.12/3.13/3.14 are fine, so the floor of 3.12 is safe. **No conflict.**

---

## Per-Item Findings

### 1. Python `>=3.12` — PASS
Supported by all four pinned packages. No upper-bound collision within the 3.12–3.14 range (clickhouse-connect's `<3.15` ceiling is the tightest and does not bite).

### 2. ClickHouse `25.8 (LTS)` — PASS, with caution
- **Real and LTS:** Confirmed. 25.8 was designated LTS (released Aug 2025); ClickHouse cuts LTS twice a year (≈March and August), supported ~1 year.
- **All features the spine relies on ship in 25.8:** ReplacingMergeTree, AggregatingMergeTree, `argMax`/`argMaxState`/`argMaxMerge`, `FINAL`, materialized views — all are long-standing core engine features, not 25.8-new. No feature risk.
- **Caution — longevity:** As of the review date, ClickHouse's actively-supported set is **26.5, 26.4, 26.3, 25.8**, and **25.8 LTS reaches end-of-support on 2026-08-29** — about 5 weeks after this spine was authored. A newer **26.3 LTS** (released March 2026, headline feature: materialized CTEs) is already available and carries ~1 year more runway. Choosing 25.8 LTS in late July 2026 pins to an LTS that is essentially stale on arrival. This is **not a correctness failure** (it works, it's real, it's LTS, features supported) but is a forward-looking hygiene concern worth a one-line note in the spine or a bump to 26.3 LTS.

### 3. edgartools `5.43.0` — PASS (exact, and API surface verified in-source)
`5.43.0` is the current PyPI `latest` (uploaded 2026-07-19), actively maintained. I downloaded the 5.43.0 sdist and grepped the actual source. Every claimed call exists:

| Claim | Verified in 5.43.0 source | Evidence |
| --- | --- | --- |
| `set_identity` | Yes | `def set_identity(...)` at `edgar/core.py:169` |
| `get_filings(filing_date=...)` | Yes | `get_filings(...)` accepts `filing_date: Optional[str]=None`; supports single date **and range** (`'2022-01-17:2022-02-28'`) — matches AD-10's `edgar.get_filings(filing_date=…)` |
| ticker→CIK via `Company("AAPL")` | Yes | `class Company(Entity)` `__init__(self, cik_or_ticker: Union[str,int])`; Entity resolves "If it's a ticker, convert to CIK first" via `find_cik` (`edgar/reference/tickers.py`) |
| `company.get_facts()` → `EntityFacts` | Yes | `Company.get_facts(...) -> Optional['EntityFacts']` at `edgar/entity/core.py`; `class EntityFacts` at `edgar/entity/entity_facts.py:136` |
| `EntityFacts.to_dataframe(pit_mode=True)` | **Yes** | `def to_dataframe(self, include_metadata=False, columns=None, pit_mode: bool = False)` at `edgar/entity/entity_facts.py:246`. Docstring: *"Point-in-Time mode for backtesting. When True, includes filing_date and form_type ... preserves all fact versions (no period deduplication), enabling lookahead-bias-free analysis. Sort order becomes (concept, period_end, filing_date)."* Documented example: `>>> df = facts.to_dataframe(pit_mode=True)` |

Notes:
- `pit_mode` is a **keyword-only-in-practice** boolean defaulting to `False`, so `to_dataframe(pit_mode=True)` is valid. Its documented semantics (preserve all filed versions, no dedup, expose `filing_date`) align precisely with the spine's latest-filed-wins / restatement-history model (AD-7) — even though the spine correctly lists `pit_mode` under **Deferred** (point-in-time surface is a Could). The API exists in v1's pinned version; deferral is a scope choice, not a capability gap.
- Minor: two `EntityFacts` classes exist in the package (`edgar/entity/entity_facts.py:136` — the modern one carrying `to_dataframe(pit_mode=...)` — and a legacy `edgar/entity/filings.py:612`). `Company.get_facts()` returns the modern one. No action needed, just be aware not to import the legacy symbol.
- The AD-9 "edgartools standardization taxonomy" for canonical concepts is real (the library exposes concept standardization / `facts.query().by_concept(...)`), consistent with the spine's use.

### 4. clickhouse-connect `1.6.0` — PASS (exact) + is the official client — CONFIRMED
- `1.6.0` is the current PyPI `latest` (uploaded 2026-07-23). Real, Production/Stable, actively maintained (the 0.x → 1.x line is genuine: `...1.4.2, 1.5.0, 1.6.0`).
- **Official client:** Confirmed. ClickHouse's own Python integration docs name **ClickHouse Connect** as the ClickHouse-Inc-supported official Python driver (HTTP interface, `clickhouse_connect.driver.Client`). The spine's deployment diagram using `clickhouse-connect` as the CLI↔ClickHouse transport is correct. (The older `clickhouse-driver` by mymarilyn is the community native-protocol alternative, not the official one.)

### 5. Typer `0.27.0` — PASS (exact)
Current PyPI `latest` (uploaded 2026-07-15). Real, actively maintained, `requires_python >=3.10`. Fits the CLI/trigger role in AD-2. No concerns. (The WebFetch summarizer earlier reported contradictory dated 0.2x versions — that was summarizer noise; the authoritative `info.version` is 0.27.0.)

### 6. uv `0.11.31` — PASS (trivially one patch behind)
`0.11.31` is real. Current `latest` is `0.11.32` (uploaded 2026-07-23, i.e. same day/just after the spine was written). Being one patch behind a same-day release is not a defect. Tooling version; unpin or float if desired.

---

## Load-Bearing Technical Claims — Pressure Test

### Claim A — ReplacingMergeTree(version) for insert-only idempotent upsert; reads need `FINAL`/`argMax` because dedup is merge-time only. **CONFIRMED**
Official ClickHouse docs + multiple corroborating sources: ReplacingMergeTree deduplicates rows sharing the ORDER BY key **only during background merges** (non-deterministic timing), keeping the row with the max `version` column. Therefore a reader **cannot assume a merge has happened** and must apply `FINAL` or an `argMax`-style GROUP BY to see deduplicated/latest state. This is exactly what AD-6 mandates ("Readers **must** use `FINAL` or an `argMax` aggregation and must never assume a background merge has run"). The `ReplacingMergeTree(version)` single-version-column form is the correct, supported signature. **Accurate.**

### Claim B — AggregatingMergeTree + argMaxState/argMaxMerge for a latest-by-max(filed_date) MV; MVs don't auto-backfill pre-existing rows (must be created before backfill). **CONFIRMED**
- `AggregatingMergeTree` columns are written with `-State` aggregate functions and read with `-Merge` functions; `argMaxState(value, filed_date)` written / `argMaxMerge(...)` read is the canonical pattern for "latest value by max timestamp." **Accurate** (AD-8).
- **No backfill of pre-existing rows:** Confirmed by official docs and corroborating guides — a ClickHouse materialized view is an insert trigger; **it only processes rows inserted after the MV is created**. Historical rows require a manual `INSERT ... SELECT ... argMaxState(...)` backfill. The spine's imperative — *"The MV must be created before any backfill insert (ClickHouse MVs do not backfill pre-existing rows)"* — is **correct and is a genuinely important, easy-to-get-wrong caveat.** Well captured.

### Claim C — edgartools API surface. **CONFIRMED** (see item 3 table; verified directly in 5.43.0 source, not just docs)

### Claim D — clickhouse-connect is the current official Python client. **CONFIRMED** (see item 4)

---

## Flags / Cautions Summary

| # | Item | Severity | Note |
| --- | --- | --- | --- |
| 1 | ClickHouse `25.8 LTS` | Caution (longevity, not correctness) | EOS **2026-08-29** (~5 weeks post-authoring); newer `26.3 LTS` available since March 2026 with ~1yr more support. Consider bumping to 26.3 LTS or explicitly acknowledging the short runway. All spine-required engine features exist in both. |
| 2 | uv `0.11.31` | Informational | One patch behind current `0.11.32` (same-day release). No action needed. |

No items **failed**. No misnamed, deprecated, non-existent, or misused technology was found.

---

## Sources
- PyPI JSON API (`info.version`, `requires_python`, `upload_time_iso_8601`) for edgartools, clickhouse-connect, typer, uv — https://pypi.org/pypi/<pkg>/json
- edgartools 5.43.0 source distribution (downloaded, extracted, grepped): `edgar/core.py`, `edgar/_filings.py`, `edgar/entity/core.py`, `edgar/entity/entity_facts.py`
- ClickHouse ReplacingMergeTree docs — https://clickhouse.com/docs/engines/table-engines/mergetree-family/replacingmergetree
- ClickHouse Python integration (official client) — https://clickhouse.com/docs/integrations/python
- ClickHouse materialized views (no-backfill behavior) — https://clickhouse.com/docs and corroborating guides
- ClickHouse 26.3 LTS release — https://clickhouse.com/blog/clickhouse-release-26-03
- ClickHouse EOL/support trackers — https://endoflife.date/clickhouse ; https://yandex.cloud/en/docs/managed-clickhouse/concepts/update-policy
- Altinity Stable Build for ClickHouse 25.8 — https://altinity.com/blog/altinity-stable-build-for-clickhouse-25-8
