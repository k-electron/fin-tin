"""fin-tin CLI — a dumb trigger over the engine core (AD-2).

The CLI parses arguments, invokes inward, and renders results. No business
logic lives here.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import typer

import contextlib

from fintin.adapters.store import schema as store_schema
from fintin.adapters.store.client import (
    StoreConnectionError,
    check_connection,
    get_client,
)
from fintin.config import ConfigError, load_config

app = typer.Typer(
    name="fintin",
    help="fin-tin — local EDGAR financial-statement query tool.",
    no_args_is_help=True,
    add_completion=False,
)

logger = logging.getLogger("fintin")


@app.callback()
def _root() -> None:
    """fin-tin — local EDGAR financial-statement query tool.

    A no-op root callback so the CLI stays a multi-command group even while
    only one command exists (catch-up, backfill, status arrive in later
    stories).
    """


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


@app.command("check-connection")
def check_connection_command(
    config: Path = typer.Option(
        Path("fintin.toml"),
        "--config",
        "-c",
        help="Path to the fintin.toml config file.",
    ),
) -> None:
    """Verify the app can connect to ClickHouse using fintin.toml."""
    _configure_logging()
    try:
        cfg = load_config(config)
    except ConfigError as exc:
        typer.secho(f"Config error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    logger.info(
        "Checking ClickHouse connection at %s:%s (database=%s)",
        cfg.clickhouse.host,
        cfg.clickhouse.port,
        cfg.clickhouse.database,
    )
    try:
        version = check_connection(cfg.clickhouse)
    except StoreConnectionError as exc:
        typer.secho(f"Connection failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    typer.secho(
        f"Connected to ClickHouse {version} at "
        f"{cfg.clickhouse.host}:{cfg.clickhouse.port} "
        f"(database={cfg.clickhouse.database}).",
        fg=typer.colors.GREEN,
    )


@app.command("schema-init")
def schema_init_command(
    config: Path = typer.Option(
        Path("fintin.toml"),
        "--config",
        "-c",
        help="Path to the fintin.toml config file.",
    ),
) -> None:
    """Create the store schema (Tier 0/1, resolution MV, wide mart) — idempotent."""
    _configure_logging()
    try:
        cfg = load_config(config)
    except ConfigError as exc:
        typer.secho(f"Config error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    logger.info(
        "Initializing store schema in ClickHouse %s:%s (database=%s)",
        cfg.clickhouse.host,
        cfg.clickhouse.port,
        cfg.clickhouse.database,
    )

    # Surface connection/auth/missing-database problems clearly (get_client itself
    # does not wrap driver errors), so a DDL failure below can't be mislabelled.
    try:
        check_connection(cfg.clickhouse)
    except StoreConnectionError as exc:
        typer.secho(f"Connection failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    client = None
    try:
        client = get_client(cfg.clickhouse)
        created = store_schema.create_schema(client)
    except Exception as exc:  # DDL error (connection already verified above)
        typer.secho(f"Schema init failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()

    typer.secho(
        f"Store schema ready in database '{cfg.clickhouse.database}': "
        f"{', '.join(created)}.",
        fg=typer.colors.GREEN,
    )


@app.command("ingest-company")
def ingest_company_command(
    cik: int = typer.Argument(..., help="SEC CIK of the company to ingest (e.g. 320193)."),
    config: Path = typer.Option(
        Path("fintin.toml"),
        "--config",
        "-c",
        help="Path to the fintin.toml config file.",
    ),
) -> None:
    """Ingest one company's standard-taxonomy facts into Tier 0 (raw_fact)."""
    _configure_logging()
    # edgartools is a heavy import — defer it so --help / check-connection /
    # schema-init stay fast and only pay for it when actually ingesting.
    from fintin.adapters.edgar.client import (
        EdgarClient,
        EdgarConfigError,
        EdgarThrottleError,
    )
    from fintin.adapters.edgar.facts import (
        NoCompanyFactsError,
        edgartools_version,
        fetch_company_facts,
    )
    from fintin.adapters.store.raw_fact_repo import insert_raw_facts, next_ingest_version
    from fintin.core.ingest import ingest_company

    # Validate the CIK before any work — cik is UInt32 in raw_fact, and a bad
    # value would otherwise waste an EDGAR fetch before failing at insert.
    if not (1 <= cik <= 4_294_967_295):
        typer.secho(
            f"Invalid CIK {cik}: must be between 1 and 4294967295.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        cfg = load_config(config)
    except ConfigError as exc:
        typer.secho(f"Config error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    # Build the rate-limited EDGAR client — its gate refuses a blank/placeholder
    # contact email before any request is made (ban-safety, FR-1).
    try:
        edgar_client = EdgarClient(cfg)
    except EdgarConfigError as exc:
        typer.secho(f"EDGAR config error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    logger.info(
        "Ingesting CIK %s into Tier 0 (database=%s)", cik, cfg.clickhouse.database
    )

    # Surface connection/auth/missing-database problems clearly before fetching.
    try:
        check_connection(cfg.clickhouse)
    except StoreConnectionError as exc:
        typer.secho(f"Connection failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    client = None
    try:
        client = get_client(cfg.clickhouse)
        # Ingest-monotonic version from the store (AD-6), not a wall clock.
        version = next_ingest_version(client)
        result = ingest_company(
            cik,
            fetch_facts=lambda c: fetch_company_facts(edgar_client, c),
            insert_rows=lambda rows: insert_raw_facts(client, rows),
            taxonomy_version=edgartools_version(),
            version=version,
        )
    except NoCompanyFactsError as exc:
        typer.secho(str(exc), fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=1)
    except EdgarThrottleError as exc:
        typer.secho(f"EDGAR throttled, gave up: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    except Exception as exc:  # fetch/insert error (connection already verified)
        typer.secho(f"Ingest failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()

    typer.secho(
        f"Ingested CIK {cik}: {result.rows_landed} facts landed "
        f"({result.dropped} dropped of {result.facts_seen} seen) "
        f"into database '{cfg.clickhouse.database}'.",
        fg=typer.colors.GREEN,
    )


@app.command("map-canonical")
def map_canonical_command(
    cik: int = typer.Argument(
        ..., help="SEC CIK whose Tier 0 facts to map into canonical Tier 1 (e.g. 320193)."
    ),
    config: Path = typer.Option(
        Path("fintin.toml"),
        "--config",
        "-c",
        help="Path to the fintin.toml config file.",
    ),
) -> None:
    """Project a company's Tier 0 raw facts into canonical Tier 1 (offline; zero EDGAR requests)."""
    _configure_logging()
    # Deferred imports keep --help / check-connection / schema-init fast. Note:
    # the projection path imports NO `edgar` at all — "zero network" is structural.
    from fintin.adapters.store.canonical_fact_repo import (
        insert_canonical_facts,
        next_canonical_version,
    )
    from fintin.adapters.store.raw_fact_repo import read_raw_facts
    from fintin.core.canonical import map_company

    # Validate the CIK before any work — cik is UInt32 in canonical_fact.
    if not (1 <= cik <= 4_294_967_295):
        typer.secho(
            f"Invalid CIK {cik}: must be between 1 and 4294967295.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        cfg = load_config(config)
    except ConfigError as exc:
        typer.secho(f"Config error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    logger.info(
        "Mapping CIK %s to canonical Tier 1 (database=%s)", cik, cfg.clickhouse.database
    )

    # Projection is zero-network (AC-1): NO EdgarClient, NO contact email, NO edgar
    # import — only ClickHouse. Surface connection/auth/missing-db problems clearly.
    try:
        check_connection(cfg.clickhouse)
    except StoreConnectionError as exc:
        typer.secho(f"Connection failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    client = None
    try:
        client = get_client(cfg.clickhouse)
        # Ingest-monotonic Tier 1 version from the store (AD-6), not a wall clock.
        version = next_canonical_version(client)
        result = map_company(
            cik,
            read_raw_facts=lambda c: read_raw_facts(client, c),
            insert_rows=lambda rows: insert_canonical_facts(client, rows),
            version=version,
        )
    except Exception as exc:  # read/project/insert error (connection already verified)
        typer.secho(f"Projection failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()

    if result.raw_seen == 0:
        typer.secho(
            f"No Tier 0 facts for CIK {cik} — run `fintin ingest-company {cik}` first.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(code=1)

    typer.secho(
        f"Mapped CIK {cik}: {result.projected} facts projected to canonical Tier 1 "
        f"(standard-element concepts) into database '{cfg.clickhouse.database}'.",
        fg=typer.colors.GREEN,
    )


@app.command("universe")
def universe_command(
    config: Path = typer.Option(
        Path("fintin.toml"),
        "--config",
        "-c",
        help="Path to the fintin.toml config file.",
    ),
    show_ciks: bool = typer.Option(
        False,
        "--show-ciks",
        help="Also print the full sorted list of resolved CIKs.",
    ),
) -> None:
    """Resolve the configured [universe] to CIKs and report scope + explained gaps.

    Offline: tickers resolve via edgartools' bundled reference table (no EDGAR
    request, no contact email needed). Unresolvable tickers are reported as
    explained gaps, never silently dropped."""
    _configure_logging()
    try:
        cfg = load_config(config)
    except ConfigError as exc:
        typer.secho(f"Config error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    if cfg.universe is None:
        typer.secho(
            "Config error: no [universe] section in "
            f"{config} — list tickers and/or ciks to define the Universe.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    # Resolution is offline (bundled reference table) — no ClickHouse, no
    # EdgarClient. Defer the heavy `edgar` import to keep --help / config-error
    # paths fast (it only loads when a ticker actually needs resolving).
    from fintin.adapters.edgar.universe import resolve_tickers
    from fintin.core.universe import resolve_universe

    # Resolution can fail on a broken edgartools install (unreadable bundled
    # table) or an import error — render it cleanly, never as a traceback.
    try:
        resolved = resolve_universe(cfg.universe, resolve_tickers=resolve_tickers)
    except Exception as exc:
        typer.secho(f"Universe resolution failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    n = len(resolved.ciks)
    noun = "company" if n == 1 else "companies"
    typer.secho(
        f"Universe: {n} {noun} "
        f"({resolved.explicit_ciks} from CIKs, {resolved.tickers_resolved} from tickers).",
        fg=typer.colors.GREEN if n else typer.colors.RED,
        err=not n,
    )
    if resolved.gaps:
        typer.secho(
            f"{len(resolved.gaps)} unresolved (explained gaps):",
            fg=typer.colors.YELLOW,
        )
        for gap in resolved.gaps:
            typer.secho(f"  - {gap.identifier}: {gap.reason}", fg=typer.colors.YELLOW)
    # An empty resolved Universe is a hard misconfiguration for a screening tool:
    # fail loudly (exit 1) so a downstream backfill/CI trigger can't proceed over
    # an empty scope silently. Gaps alongside a NON-empty Universe stay non-fatal.
    if not n:
        typer.secho(
            "Resolved Universe is empty — check the [universe] tickers/ciks.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    if show_ciks:
        typer.echo(" ".join(str(c) for c in resolved.ciks))


@app.command("work-list")
def work_list_command(
    config: Path = typer.Option(
        Path("fintin.toml"),
        "--config",
        "-c",
        help="Path to the fintin.toml config file.",
    ),
    show_items: bool = typer.Option(
        False,
        "--show-items",
        help="Print each outstanding filing (accession, cik, form, filed_date).",
    ),
) -> None:
    """Preview outstanding ingestion work: EDGAR-index filings over the lookback
    window (for the Universe) that aren't yet in the store. Read-only dry-run of
    catch-up — hits EDGAR's index (needs a real contact email), ingests nothing."""
    _configure_logging()
    # Heavy `edgar` imports deferred so --help / config-error paths stay fast.
    from datetime import date

    from fintin.adapters.edgar.client import (
        EdgarClient,
        EdgarConfigError,
        EdgarThrottleError,
    )
    from fintin.adapters.edgar.filings_index import fetch_work_candidates
    from fintin.adapters.edgar.universe import resolve_tickers
    from fintin.adapters.store.raw_fact_repo import high_water_mark, present_accessions
    from fintin.core.reconcile import compute_work_list, resolve_window
    from fintin.core.universe import resolve_universe

    try:
        cfg = load_config(config)
    except ConfigError as exc:
        typer.secho(f"Config error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    if cfg.universe is None:
        typer.secho(
            f"Config error: no [universe] section in {config} — define the Universe first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    # Discovery hits EDGAR's index — build the rate-limited client (its gate
    # rejects a blank/placeholder email before any request; ban-safety, FR-1).
    try:
        edgar_client = EdgarClient(cfg)
    except EdgarConfigError as exc:
        typer.secho(f"EDGAR config error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    # Universe resolution is offline (Story 2.1) — an empty resolved Universe is a
    # hard misconfiguration for a screening scope.
    resolved = resolve_universe(cfg.universe, resolve_tickers=resolve_tickers)
    if not resolved.ciks:
        typer.secho(
            "Resolved Universe is empty — check the [universe] tickers/ciks.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        check_connection(cfg.clickhouse)
    except StoreConnectionError as exc:
        typer.secho(f"Connection failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    client = None
    try:
        client = get_client(cfg.clickhouse)
        # HWM sizes the index scan window (a hint, AD-16) — never the done-ness test.
        hwm = high_water_mark(client)
        window_start, window_end = resolve_window(
            hwm, cfg.reconcile.lookback_days, date.today()
        )
        # Discover candidates from EDGAR's index, THEN check membership by their
        # exact accessions (AD-16 authority — decoupled from any date).
        candidates = fetch_work_candidates(
            edgar_client,
            filing_date=f"{window_start.isoformat()}:{window_end.isoformat()}",
            ciks=resolved.ciks,
        )
        present = present_accessions(
            client, accessions={c.accession for c in candidates}
        )
        work = compute_work_list(candidates, present)
    except EdgarThrottleError as exc:
        typer.secho(f"EDGAR throttled, gave up: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    except Exception as exc:  # discovery/query error (connection already verified)
        typer.secho(f"Work-list failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()

    n = len(work.items)
    companies = len({item.cik for item in work.items})
    typer.secho(
        f"Work list over {window_start.isoformat()}..{window_end.isoformat()} "
        f"({cfg.reconcile.lookback_days}-day lookback): "
        f"{n} outstanding filing(s) across {companies} company(ies) "
        f"[{work.scanned} scanned, {work.already_present} already present].",
        fg=typer.colors.GREEN,
    )
    if hwm is None:
        typer.secho(
            "Store is empty — this shows only the recent lookback window; "
            "run a full backfill for complete history.",
            fg=typer.colors.YELLOW,
        )
    if show_items:
        for item in work.items:
            typer.echo(
                f"  {item.accession}  {item.cik}  {item.form}  {item.filed_date.isoformat()}"
            )


@app.command("backfill")
def backfill_command(
    config: Path = typer.Option(
        Path("fintin.toml"),
        "--config",
        "-c",
        help="Path to the fintin.toml config file.",
    ),
    refresh: bool = typer.Option(
        False,
        "--refresh",
        help="Re-ingest companies already present (bypass the resume-skip); "
        "supersedes prior values on read (cannot retract facts removed since).",
    ),
    show_gaps: bool = typer.Option(
        False,
        "--show-gaps",
        help="List each company recorded as an explained gap (cik, reason).",
    ),
) -> None:
    """Backfill the Universe's full history into Tier 0, resumably (per-company).

    Ingests each in-scope company's entire `companyfacts` history through the one
    rate-limited client, committing per company. Re-running skips companies
    already in the store (no checkpoint file), so an interrupted backfill resumes.
    A per-company failure is a recorded explained gap, not fatal; only an EDGAR
    throttle aborts the run (ban-safety). Run `fintin schema-init` first."""
    _configure_logging()
    # Heavy `edgar` imports deferred so --help / config-error paths stay fast.
    from fintin.adapters.edgar.backfill import CompanyFactsStrategy
    from fintin.adapters.edgar.client import (
        EdgarClient,
        EdgarConfigError,
        EdgarThrottleError,
    )
    from fintin.adapters.edgar.facts import edgartools_version
    from fintin.adapters.edgar.universe import resolve_tickers
    from fintin.adapters.store.raw_fact_repo import (
        insert_raw_facts,
        next_ingest_version,
        present_ciks,
    )
    from fintin.core.backfill import BackfillAborted, backfill_universe
    from fintin.core.universe import resolve_universe

    # Abort the run if this many companies fail in an unbroken row — a systemic
    # failure (e.g. the store dropped mid-run) must not be laundered into per-
    # company gaps while still spending EDGAR requests (SM-C1).
    max_consecutive_failures = 10

    try:
        cfg = load_config(config)
    except ConfigError as exc:
        typer.secho(f"Config error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    if cfg.universe is None:
        typer.secho(
            f"Config error: no [universe] section in {config} — define the Universe first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    # Backfill hits EDGAR — build the rate-limited client ONCE (reused across every
    # company; a second construction would reset process-global edgar rate state).
    # Its gate rejects a blank/placeholder email before any request (ban-safety, FR-1).
    try:
        edgar_client = EdgarClient(cfg)
    except EdgarConfigError as exc:
        typer.secho(f"EDGAR config error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    # Universe resolution is offline (Story 2.1). It can still fail on a degraded
    # edgartools install (unreadable bundled reference table) — render it cleanly,
    # never as a traceback.
    try:
        resolved = resolve_universe(cfg.universe, resolve_tickers=resolve_tickers)
    except Exception as exc:
        typer.secho(f"Universe resolution failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    # An empty resolved Universe is a hard misconfiguration for a backfill scope.
    if not resolved.ciks:
        typer.secho(
            "Resolved Universe is empty — check the [universe] tickers/ciks.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        check_connection(cfg.clickhouse)
    except StoreConnectionError as exc:
        typer.secho(f"Connection failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    def _log_company(event) -> None:
        logger.info(
            "[%d/%d] CIK %s %s", event.index, event.total, event.cik, event.outcome
        )

    client = None
    try:
        client = get_client(cfg.clickhouse)
        # One ingest-monotonic version base per run (AD-6); the engine offsets it
        # per company so a shared cross-company accession resolves deterministically.
        version = next_ingest_version(client)
        # Resume: skip companies already present (derived from the store, not a
        # checkpoint — AD-1/AD-11/AD-16). --refresh re-ingests all (supersedes).
        present = set() if refresh else present_ciks(client, ciks=resolved.ciks)
        report = backfill_universe(
            resolved.ciks,
            strategy=CompanyFactsStrategy(edgar_client),
            insert_rows=lambda rows: insert_raw_facts(client, rows),
            taxonomy_version=edgartools_version(),
            version=version,
            already_present=present,
            fatal_errors=(EdgarThrottleError,),  # throttle exhausted → abort (SM-C1)
            max_consecutive_failures=max_consecutive_failures,
            on_company=_log_company,
        )
    except EdgarThrottleError as exc:
        typer.secho(
            f"EDGAR throttled, backfill aborted: {exc}", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(code=1)
    except BackfillAborted as exc:  # systemic failure (e.g. store down) — stop
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    except Exception as exc:  # setup/query error (connection already verified)
        typer.secho(f"Backfill failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()

    noun = "company" if report.companies_ingested == 1 else "companies"
    typer.secho(
        f"Backfill complete: {report.companies_ingested} {noun} ingested "
        f"({report.rows_landed} facts landed), "
        f"{report.companies_skipped} already present, "
        f"{report.companies_failed} failed "
        f"into database '{cfg.clickhouse.database}'.",
        fg=typer.colors.GREEN,
    )
    if report.failures:
        typer.secho(
            f"{report.companies_failed} company(ies) recorded as explained gaps.",
            fg=typer.colors.YELLOW,
        )
        if show_gaps:
            for gap in report.failures:
                typer.secho(f"  - CIK {gap.cik}: {gap.reason}", fg=typer.colors.YELLOW)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
