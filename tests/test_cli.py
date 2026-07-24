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
