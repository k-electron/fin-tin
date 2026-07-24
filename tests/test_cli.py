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
