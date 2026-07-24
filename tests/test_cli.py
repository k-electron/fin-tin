"""CLI skeleton tests (no container required)."""

from __future__ import annotations

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
