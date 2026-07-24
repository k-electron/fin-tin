"""fin-tin CLI — a dumb trigger over the engine core (AD-2).

The CLI parses arguments, invokes inward, and renders results. No business
logic lives here.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import typer

from fintin.adapters.store.client import StoreConnectionError, check_connection
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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
