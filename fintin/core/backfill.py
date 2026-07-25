"""Universe backfill — the pure engine + the pluggable strategy interface (AD-13).

Backfill populates Tier 0 from empty by ingesting each in-scope company's full
available history. It is pure `core`: it depends on nothing outward (no `edgar`,
no ClickHouse). The concrete per-company fetch is injected as a
:class:`BackfillStrategy`; the store insert is an injected callable — so the
adapters stay outside `core` (AD-2 pure engine; AD-3 import guard).

Resumability (AD-1/AD-11/AD-16): the engine skips companies already present in
the store (``already_present``, re-derived each run — never a checkpoint file)
and commits per company (one company = one :func:`ingest_company` call = one
insert). A killed run resumes by re-deriving the remaining gap. Per-company
idempotency (``ReplacingMergeTree(version)``, AD-6) already makes a re-touched
company a read no-op; the skip merely avoids re-fetching it (SM-C1: fewer
requests over speed).

Failure policy (SM-2 vs SM-C1): a per-company failure is recorded as an explained
gap and the run continues — EXCEPT any exception type in ``fatal_errors`` (the CLI
passes the EDGAR throttle error) propagates and aborts the whole run, because
continuing to hammer a throttling EDGAR risks a ban.
"""

from __future__ import annotations

from collections.abc import Callable, Container, Iterable, Sequence
from typing import NamedTuple, Protocol, runtime_checkable

from fintin.core.ingest import FactLike, IngestResult, RawFactRow, ingest_company


@runtime_checkable
class BackfillStrategy(Protocol):
    """How to fetch one company's full-history facts — the pluggable seam (AD-13).

    v1 is the per-company `companyfacts` API strategy; a bulk `companyfacts.zip`
    strategy is a *future implementation of this same interface*, so the engine
    needs no redesign when the Universe outgrows per-company scale. The engine
    depends only on this Protocol, never on a concrete strategy."""

    name: str

    def company_facts(self, cik: int) -> Iterable[FactLike]:
        """Return one company's facts (an iterable of :class:`FactLike`)."""
        ...


class BackfillAborted(Exception):
    """The run aborted mid-loop because too many companies failed in a row — a
    systemic problem (e.g. the store went down after the pre-flight check), not a
    per-company data gap. Raised rather than laundering every remaining company
    into a recorded gap while still hitting EDGAR (SM-C1) and exiting 0."""


class BackfillFailure(NamedTuple):
    """A company that could not be ingested — a recorded explained gap (SM-2),
    never a silent omission. ``reason`` is a human-readable cause."""

    cik: int
    reason: str


class BackfillEvent(NamedTuple):
    """One per-company progress signal for an injected observer (the CLI logs it),
    so `core` stays I/O-free. ``outcome`` is ``"ingested"`` | ``"skipped"`` |
    ``"failed"``; ``index`` is the 1-based position within the run."""

    cik: int
    outcome: str
    index: int
    total: int


class BackfillReport(NamedTuple):
    ingested: tuple[IngestResult, ...]
    skipped: tuple[int, ...]
    failures: tuple[BackfillFailure, ...]
    version: int

    @property
    def attempted(self) -> int:
        """Companies actually fetched (ingested + failed) — excludes skipped."""
        return len(self.ingested) + len(self.failures)

    @property
    def companies_ingested(self) -> int:
        return len(self.ingested)

    @property
    def companies_skipped(self) -> int:
        return len(self.skipped)

    @property
    def companies_failed(self) -> int:
        return len(self.failures)

    @property
    def rows_landed(self) -> int:
        return sum(r.rows_landed for r in self.ingested)


def _emit(
    on_company: Callable[[BackfillEvent], None] | None,
    cik: int,
    outcome: str,
    index: int,
    total: int,
) -> None:
    if on_company is None:
        return
    try:
        on_company(BackfillEvent(cik=cik, outcome=outcome, index=index, total=total))
    except Exception:
        # A passive progress observer must never sink an in-flight run whose
        # earlier companies are already committed. Swallow its errors.
        pass


def backfill_universe(
    ciks: Sequence[int],
    *,
    strategy: BackfillStrategy,
    insert_rows: Callable[[Sequence[RawFactRow]], int],
    taxonomy_version: str,
    version: int,
    already_present: Container[int] = frozenset(),
    fatal_errors: tuple[type[BaseException], ...] = (),
    max_consecutive_failures: int | None = None,
    on_company: Callable[[BackfillEvent], None] | None = None,
) -> BackfillReport:
    """Ingest each in-scope company's full history, committing per company (AD-11).

    Iterates the de-duplicated CIKs in sorted order (deterministic — kboss). A CIK
    already in ``already_present`` is skipped **without calling the strategy** (no
    fetch — SM-C1). Each remaining company is ingested via the injected
    ``strategy`` + ``insert_rows`` (reusing :func:`ingest_company`). The stamped
    ``version`` is ``version + <sorted position>`` — one base per run, offset per
    company so that if two co-registrants share a globally-identical accession
    (the dedup key excludes CIK) the collision resolves *deterministically*
    (higher position wins) rather than by ReplacingMergeTree's arbitrary
    equal-version tie-break. A later resume run derives a strictly greater base,
    so supersession (AD-6) still holds.

    A per-company error is recorded as a :class:`BackfillFailure` and the run
    continues (SM-2) — except (a) any type in ``fatal_errors`` propagates and
    aborts immediately (e.g. EDGAR throttle exhausted; ban-safety, SM-C1), and
    (b) if ``max_consecutive_failures`` is set and that many companies fail in an
    unbroken row, :class:`BackfillAborted` is raised — a systemic failure (e.g.
    the store dropped) must not be laundered into hundreds of per-company gaps
    while still spending EDGAR requests. ``on_company``, if given, is called once
    per company with a :class:`BackfillEvent` (the engine does no I/O itself; an
    observer error can't sink the run).
    """
    ordered = sorted({int(c) for c in ciks})
    total = len(ordered)
    ingested: list[IngestResult] = []
    skipped: list[int] = []
    failures: list[BackfillFailure] = []
    consecutive = 0  # unbroken run of failures (skips are neutral)

    for position, cik in enumerate(ordered):
        index = position + 1  # 1-based, for the progress event
        if cik in already_present:
            skipped.append(cik)
            _emit(on_company, cik, "skipped", index, total)
            continue
        try:
            result = ingest_company(
                cik,
                fetch_facts=strategy.company_facts,
                insert_rows=insert_rows,
                taxonomy_version=taxonomy_version,
                version=version + position,  # deterministic per-company version
            )
        except fatal_errors:  # e.g. EDGAR throttle exhausted → abort the whole run
            raise
        except Exception as exc:  # per-company failure: recorded, not fatal (SM-2)
            reason = f"{type(exc).__name__}: {exc}"
            failures.append(BackfillFailure(cik, reason))
            _emit(on_company, cik, "failed", index, total)
            consecutive += 1
            if (
                max_consecutive_failures is not None
                and consecutive >= max_consecutive_failures
            ):
                raise BackfillAborted(
                    f"backfill aborted after {consecutive} consecutive failures "
                    f"(last: CIK {cik} — {reason}); likely a systemic problem "
                    f"(e.g. the store) rather than per-company data gaps"
                )
            continue
        ingested.append(result)
        _emit(on_company, cik, "ingested", index, total)
        consecutive = 0

    return BackfillReport(
        ingested=tuple(ingested),
        skipped=tuple(skipped),
        failures=tuple(failures),
        version=version,
    )
