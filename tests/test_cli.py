"""CLI skeleton tests (no container required unless marked integration)."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from fintin.cli.app import app

runner = CliRunner()


def test_help_exit_zero_and_lists_command():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "check-connection" in result.output


def test_missing_config_reports_clean_error():
    result = runner.invoke(
        app, ["check-connection", "--config", "does-not-exist.toml"]
    )
    assert result.exit_code != 0
    assert "Config error" in result.output
    # AC-3: a clear config error, not a Python stack trace.
    assert "Traceback" not in result.output


def test_malformed_config_reports_clean_error(tmp_path):
    bad = tmp_path / "fintin.toml"
    bad.write_text("this is = = not valid toml [[[")
    result = runner.invoke(app, ["check-connection", "--config", str(bad)])
    assert result.exit_code != 0
    assert "Config error" in result.output
    assert "Traceback" not in result.output


def test_help_lists_schema_init():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "schema-init" in result.output


def test_schema_init_missing_config_reports_clean_error():
    result = runner.invoke(app, ["schema-init", "--config", "does-not-exist.toml"])
    assert result.exit_code == 2
    assert "Config error" in result.output
    assert "Traceback" not in result.output


def test_help_lists_ingest_company():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "ingest-company" in result.output


_CH_ONLY = (
    '[clickhouse]\nhost = "localhost"\nport = 8123\n'
    'username = "default"\npassword = ""\ndatabase = "default"\n'
)


def test_ingest_company_missing_config_reports_clean_error():
    result = runner.invoke(app, ["ingest-company", "320193", "--config", "does-not-exist.toml"])
    assert result.exit_code == 2
    assert "Config error" in result.output
    assert "Traceback" not in result.output


def test_ingest_company_missing_edgar_block_reports_clean_error(tmp_path):
    # Valid [clickhouse] but no [edgar] — the EdgarClient gate must fail loudly
    # BEFORE any EDGAR/ClickHouse access (offline, ban-safe).
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY)
    result = runner.invoke(app, ["ingest-company", "320193", "--config", str(p)])
    assert result.exit_code == 2
    assert "EDGAR config error" in result.output
    assert "Traceback" not in result.output


def test_ingest_company_placeholder_email_reports_clean_error(tmp_path):
    p = tmp_path / "fintin.toml"
    p.write_text(
        _CH_ONLY
        + '\n[edgar]\nuser_agent_name = "fin-tin"\ncontact_email = "you@example.com"\n'
    )
    result = runner.invoke(app, ["ingest-company", "320193", "--config", str(p)])
    assert result.exit_code == 2
    assert "EDGAR config error" in result.output
    assert "Traceback" not in result.output


def test_ingest_company_invalid_cik_reports_clean_error():
    # CIK 0 is out of the UInt32 range [1, ...]; must fail fast before any fetch.
    result = runner.invoke(app, ["ingest-company", "0"])
    assert result.exit_code == 2
    assert "Invalid CIK" in result.output
    assert "Traceback" not in result.output


def test_help_lists_map_canonical():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "map-canonical" in result.output


def test_map_canonical_missing_config_reports_clean_error():
    result = runner.invoke(app, ["map-canonical", "320193", "--config", "does-not-exist.toml"])
    assert result.exit_code == 2
    assert "Config error" in result.output
    assert "Traceback" not in result.output


def test_map_canonical_invalid_cik_reports_clean_error():
    # Must fail fast on an out-of-range CIK before touching ClickHouse.
    result = runner.invoke(app, ["map-canonical", "0"])
    assert result.exit_code == 2
    assert "Invalid CIK" in result.output
    assert "Traceback" not in result.output


# --- universe (Story 2.1) ------------------------------------------------------
# Offline: resolution reads edgartools' bundled table (no ClickHouse, no network).


def test_help_lists_universe():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "universe" in result.output


def test_universe_missing_config_reports_clean_error():
    result = runner.invoke(app, ["universe", "--config", "does-not-exist.toml"])
    assert result.exit_code == 2
    assert "Config error" in result.output
    assert "Traceback" not in result.output


def test_universe_missing_section_reports_clean_error(tmp_path):
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY)  # valid [clickhouse], no [universe]
    result = runner.invoke(app, ["universe", "--config", str(p)])
    assert result.exit_code == 2
    assert "Config error" in result.output
    assert "[universe]" in result.output
    assert "Traceback" not in result.output


def test_universe_resolves_and_reports_gap(tmp_path):
    p = tmp_path / "fintin.toml"
    p.write_text(
        _CH_ONLY + '\n[universe]\ntickers = ["AAPL", "ZZZZINVALID"]\n'
    )
    result = runner.invoke(app, ["universe", "--config", str(p)])
    assert result.exit_code == 0  # a NON-empty Universe with gaps is non-fatal
    assert "1 company " in result.output  # AAPL resolved (singular, not "1 companies")
    assert "ZZZZINVALID" in result.output  # the gap is surfaced, not silently dropped
    assert "Traceback" not in result.output


def test_universe_all_unresolved_exits_1(tmp_path):
    # A Universe that resolves to zero companies is a hard misconfiguration —
    # fail loudly so a downstream trigger can't proceed over an empty scope.
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY + '\n[universe]\ntickers = ["ZZZZINVALID"]\n')
    result = runner.invoke(app, ["universe", "--config", str(p)])
    assert result.exit_code == 1
    assert "empty" in result.output
    assert "ZZZZINVALID" in result.output  # still lists the gap
    assert "Traceback" not in result.output


def test_universe_show_ciks_prints_resolved_cik(tmp_path):
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY + "\n[universe]\nciks = [320193]\n")
    result = runner.invoke(app, ["universe", "--config", str(p), "--show-ciks"])
    assert result.exit_code == 0
    assert "320193" in result.output


# --- work-list (Story 2.2) -----------------------------------------------------
# Error paths only — the happy path hits EDGAR's index (covered offline by
# test_reconcile / test_filings_index / test_raw_fact_repo), never live in tests.

_EDGAR_PLACEHOLDER = '\n[edgar]\nuser_agent_name = "fin-tin"\ncontact_email = "you@example.com"\n'


def test_help_lists_work_list():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "work-list" in result.output


def test_work_list_missing_config_reports_clean_error():
    result = runner.invoke(app, ["work-list", "--config", "does-not-exist.toml"])
    assert result.exit_code == 2
    assert "Config error" in result.output
    assert "Traceback" not in result.output


def test_work_list_missing_universe_reports_clean_error(tmp_path):
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY)  # no [universe]
    result = runner.invoke(app, ["work-list", "--config", str(p)])
    assert result.exit_code == 2
    assert "[universe]" in result.output
    assert "Traceback" not in result.output


def test_work_list_missing_edgar_reports_clean_error(tmp_path):
    # [universe] present but no [edgar] — the EdgarClient gate must fail loudly
    # (exit 2) BEFORE any EDGAR/ClickHouse access (offline, ban-safe).
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY + '\n[universe]\ntickers = ["AAPL"]\n')
    result = runner.invoke(app, ["work-list", "--config", str(p)])
    assert result.exit_code == 2
    assert "EDGAR config error" in result.output
    assert "Traceback" not in result.output


def test_work_list_placeholder_email_reports_clean_error(tmp_path):
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY + '\n[universe]\ntickers = ["AAPL"]\n' + _EDGAR_PLACEHOLDER)
    result = runner.invoke(app, ["work-list", "--config", str(p)])
    assert result.exit_code == 2
    assert "EDGAR config error" in result.output
    assert "Traceback" not in result.output


# --- backfill (Story 2.3) ------------------------------------------------------
# Error paths only — the happy path hits EDGAR's companyfacts API (covered offline
# by test_backfill / test_edgar_backfill / test_raw_fact_repo), never live in tests.


def test_help_lists_backfill():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "backfill" in result.output


def test_backfill_missing_config_reports_clean_error():
    result = runner.invoke(app, ["backfill", "--config", "does-not-exist.toml"])
    assert result.exit_code == 2
    assert "Config error" in result.output
    assert "Traceback" not in result.output


def test_backfill_missing_universe_reports_clean_error(tmp_path):
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY)  # no [universe]
    result = runner.invoke(app, ["backfill", "--config", str(p)])
    assert result.exit_code == 2
    assert "[universe]" in result.output
    assert "Traceback" not in result.output


def test_backfill_missing_edgar_reports_clean_error(tmp_path):
    # [universe] present but no [edgar] — the EdgarClient gate must fail loudly
    # (exit 2) BEFORE any EDGAR/ClickHouse access (offline, ban-safe).
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY + '\n[universe]\ntickers = ["AAPL"]\n')
    result = runner.invoke(app, ["backfill", "--config", str(p)])
    assert result.exit_code == 2
    assert "EDGAR config error" in result.output
    assert "Traceback" not in result.output


def test_backfill_placeholder_email_reports_clean_error(tmp_path):
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY + '\n[universe]\ntickers = ["AAPL"]\n' + _EDGAR_PLACEHOLDER)
    result = runner.invoke(app, ["backfill", "--config", str(p)])
    assert result.exit_code == 2
    assert "EDGAR config error" in result.output
    assert "Traceback" not in result.output


# A syntactically valid, non-placeholder email passes the EdgarClient ban-safety
# gate (so we can reach the post-construction exit-1 paths) without being anyone's
# real address — safe for a public repo. Mirrors test_config.py's a@b.co.
_EDGAR_VALID = '\n[edgar]\nuser_agent_name = "fin-tin"\ncontact_email = "a@b.co"\n'


def test_backfill_empty_universe_exits_1(tmp_path):
    # A Universe that resolves to zero companies is a hard misconfiguration →
    # exit 1, offline: resolution reads the bundled table (no network) and the
    # empty check precedes any ClickHouse access.
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY + '\n[universe]\ntickers = ["ZZZZINVALID"]\n' + _EDGAR_VALID)
    result = runner.invoke(app, ["backfill", "--config", str(p)])
    assert result.exit_code == 1
    assert "empty" in result.output
    assert "Traceback" not in result.output


def _stub_store(monkeypatch):
    """Stub the store client so a backfill CLI test can reach backfill_universe
    without a live ClickHouse (offline). `query` feeds next_ingest_version /
    present_ciks a trivial result; nothing here hits the network."""
    import fintin.cli.app as app_mod

    class _DummyCH:
        def query(self, *a, **k):
            class _R:
                result_rows = [[0]]

            return _R()

        def close(self):
            pass

    monkeypatch.setattr(app_mod, "check_connection", lambda cfg, **k: "test-version")
    monkeypatch.setattr(app_mod, "get_client", lambda cfg, **k: _DummyCH())


def _raise(exc):
    def _f(*a, **k):
        raise exc

    return _f


def test_backfill_throttle_aborts_with_exit_1(tmp_path, monkeypatch):
    # AC-4 / SM-C1: an EDGAR throttle propagated from the engine maps to a loud
    # exit 1 at the CLI boundary — the ban-safety wiring, asserted offline.
    import fintin.core.backfill as bf_mod
    from fintin.adapters.edgar.client import EdgarThrottleError

    _stub_store(monkeypatch)
    monkeypatch.setattr(
        bf_mod, "backfill_universe", _raise(EdgarThrottleError("throttled after retries"))
    )
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY + "\n[universe]\nciks = [320193]\n" + _EDGAR_VALID)
    result = runner.invoke(app, ["backfill", "--config", str(p)])
    assert result.exit_code == 1
    assert "throttled" in result.output.lower()
    assert "Traceback" not in result.output


def test_backfill_systemic_abort_exits_1(tmp_path, monkeypatch):
    # A BackfillAborted (too many consecutive failures — e.g. store down) maps to
    # exit 1, not a green "complete" with hundreds of gaps.
    import fintin.core.backfill as bf_mod
    from fintin.core.backfill import BackfillAborted

    _stub_store(monkeypatch)
    monkeypatch.setattr(
        bf_mod,
        "backfill_universe",
        _raise(BackfillAborted("backfill aborted after 10 consecutive failures")),
    )
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY + "\n[universe]\nciks = [320193]\n" + _EDGAR_VALID)
    result = runner.invoke(app, ["backfill", "--config", str(p)])
    assert result.exit_code == 1
    assert "consecutive failures" in result.output
    assert "Traceback" not in result.output


# --- status (Story 2.4) --------------------------------------------------------
# Offline command (ClickHouse + bundled-parquet resolution only, NO EdgarClient).
# Error paths are pure-offline; the happy path is integration-tested end to end
# (status is the one command whose happy path needs no live EDGAR).


def test_help_lists_status():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "status" in result.output


def test_status_missing_config_reports_clean_error():
    result = runner.invoke(app, ["status", "--config", "does-not-exist.toml"])
    assert result.exit_code == 2
    assert "Config error" in result.output
    assert "Traceback" not in result.output


def test_status_missing_universe_reports_clean_error(tmp_path):
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY)  # no [universe]
    result = runner.invoke(app, ["status", "--config", str(p)])
    assert result.exit_code == 2
    assert "[universe]" in result.output
    assert "Traceback" not in result.output


def test_status_empty_universe_exits_1(tmp_path):
    # Unresolvable-only tickers → empty resolved Universe → exit 1, fully offline
    # (resolution reads the bundled table; no [edgar] block / EdgarClient needed).
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY + '\n[universe]\ntickers = ["ZZZZINVALID"]\n')
    result = runner.invoke(app, ["status", "--config", str(p)])
    assert result.exit_code == 1
    assert "empty" in result.output
    assert "Traceback" not in result.output


@pytest.mark.integration
def test_status_happy_path_reports_coverage_and_gaps(tmp_path, local_clickhouse_config):
    # End-to-end, offline: seed one in-scope company; the other in-scope CIK is
    # absent → reported as a zero-fact explained gap. No EDGAR involved.
    import uuid
    from datetime import date

    from fintin.adapters.store import schema as store_schema
    from fintin.adapters.store.client import get_client
    from fintin.adapters.store.raw_fact_repo import insert_raw_facts
    from fintin.core.ingest import RawFactRow

    db = f"fintin_test_{uuid.uuid4().hex[:12]}"
    admin = get_client(local_clickhouse_config)
    try:
        admin.command(f"CREATE DATABASE {db}")
    finally:
        admin.close()

    try:
        client = get_client(local_clickhouse_config, database=db)
        try:
            store_schema.create_schema(client)
            insert_raw_facts(
                client,
                [
                    RawFactRow(
                        cik=320193,
                        accession="0000320193-24-000001",
                        raw_tag="us-gaap:Revenues",
                        raw_label="Revenues",
                        taxonomy="us-gaap",
                        period_start=date(2023, 1, 1),
                        period_end=date(2023, 12, 31),
                        unit="USD",
                        value=1000.0,
                        form="10-K",
                        filed_date=date(2024, 2, 1),
                        content_hash="h",
                        taxonomy_version="5.43.0",
                        version=1,
                    )
                ],
            )
        finally:
            client.close()

        ch = local_clickhouse_config
        p = tmp_path / "fintin.toml"
        p.write_text(
            f'[clickhouse]\nhost = "{ch.host}"\nport = {ch.port}\n'
            f'username = "{ch.username}"\npassword = "{ch.password}"\ndatabase = "{db}"\n'
            "\n[universe]\nciks = [320193, 1652044]\n"  # one present, one absent
        )
        result = runner.invoke(app, ["status", "--config", str(p), "--show-gaps"])
        assert result.exit_code == 0
        assert "1 of 2 in-scope companies present" in result.output
        assert "2024-02-01" in result.output  # the high-water mark
        assert "1 zero-fact company" in result.output
        assert "CIK 1652044: no facts in store" in result.output  # the absent CIK
        assert "Traceback" not in result.output
    finally:
        cleanup = get_client(local_clickhouse_config)
        try:
            cleanup.command(f"DROP DATABASE IF EXISTS {db}")
        finally:
            cleanup.close()
