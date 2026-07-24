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
    from fintin.adapters.edgar.facts import edgartools_version, fetch_company_facts
    from fintin.adapters.store.raw_fact_repo import insert_raw_facts
    from fintin.core.ingest import ingest_company

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
        result = ingest_company(
            cik,
            fetch_facts=lambda c: fetch_company_facts(edgar_client, c),
            insert_rows=lambda rows: insert_raw_facts(client, rows),
            taxonomy_version=edgartools_version(),
        )
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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
