"""CLI skeleton tests (no container required unless marked integration)."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from fintin.cli import app as app_mod
from fintin.cli.app import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    """Run every CLI test in a throwaway CWD. The ingestion commands create a
    default `fintin.lease` (single-flight, Story 3.2) in the working directory, so
    without this a lease file could land in the repo root. Config files use
    absolute tmp paths, so relocating the CWD is harmless."""
    monkeypatch.chdir(tmp_path)


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
        _raise(BackfillAborted("run aborted after 10 consecutive failures")),
    )
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY + "\n[universe]\nciks = [320193]\n" + _EDGAR_VALID)
    result = runner.invoke(app, ["backfill", "--config", str(p)])
    assert result.exit_code == 1
    assert "consecutive failures" in result.output
    assert "Traceback" not in result.output


# --- catch-up (Story 3.1) ------------------------------------------------------
# Error paths + ban-safety wiring only. The COMPLETED happy path hits EDGAR (index
# AND companyfacts) so it's covered offline by test_catchup / test_backfill, never
# live (NFR-7); only the NOTHING_TO_DO branch is CLI-drivable offline (index stub).


def test_help_lists_catch_up():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "catch-up" in result.output


def test_catch_up_missing_config_reports_clean_error():
    result = runner.invoke(app, ["catch-up", "--config", "does-not-exist.toml"])
    assert result.exit_code == 2
    assert "Config error" in result.output
    assert "Traceback" not in result.output


def test_catch_up_missing_universe_reports_clean_error(tmp_path):
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY)  # no [universe]
    result = runner.invoke(app, ["catch-up", "--config", str(p)])
    assert result.exit_code == 2
    assert "[universe]" in result.output
    assert "Traceback" not in result.output


def test_catch_up_missing_edgar_reports_clean_error(tmp_path):
    # [universe] present but no [edgar] — the EdgarClient gate must fail loudly
    # (exit 2) BEFORE any EDGAR/ClickHouse access (offline, ban-safe).
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY + '\n[universe]\ntickers = ["AAPL"]\n')
    result = runner.invoke(app, ["catch-up", "--config", str(p)])
    assert result.exit_code == 2
    assert "EDGAR config error" in result.output
    assert "Traceback" not in result.output


def test_catch_up_placeholder_email_reports_clean_error(tmp_path):
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY + '\n[universe]\ntickers = ["AAPL"]\n' + _EDGAR_PLACEHOLDER)
    result = runner.invoke(app, ["catch-up", "--config", str(p)])
    assert result.exit_code == 2
    assert "EDGAR config error" in result.output
    assert "Traceback" not in result.output


def test_catch_up_empty_universe_exits_1(tmp_path):
    # A Universe that resolves to zero companies is a hard misconfiguration →
    # exit 1, offline (resolution reads the bundled table; the empty check precedes
    # any ClickHouse/EDGAR access).
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY + '\n[universe]\ntickers = ["ZZZZINVALID"]\n' + _EDGAR_VALID)
    result = runner.invoke(app, ["catch-up", "--config", str(p)])
    assert result.exit_code == 1
    assert "empty" in result.output
    assert "Traceback" not in result.output


def test_catch_up_nothing_to_do_exits_0(tmp_path, monkeypatch):
    # AC-3: the one CLI-drivable happy branch offline — stub the index fetch to
    # return no candidates, so the reused reconciler yields an empty work list and
    # catch_up returns NOTHING_TO_DO (no companyfacts request), exit 0.
    import fintin.adapters.edgar.filings_index as fi_mod

    _stub_store(monkeypatch)
    monkeypatch.setattr(fi_mod, "fetch_work_candidates", lambda *a, **k: [])
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY + "\n[universe]\nciks = [320193]\n" + _EDGAR_VALID)
    result = runner.invoke(app, ["catch-up", "--config", str(p)])
    assert result.exit_code == 0
    assert "Nothing to do" in result.output
    assert "NOTHING_TO_DO" in result.output
    assert "Traceback" not in result.output


def test_catch_up_throttle_aborts_with_exit_1(tmp_path, monkeypatch):
    # AC-4 / SM-C1: an EDGAR throttle propagated from the engine maps to a loud
    # exit 1 at the CLI boundary — the ban-safety wiring, asserted offline. The
    # index fetch is stubbed so no live request happens before the engine runs.
    import fintin.adapters.edgar.filings_index as fi_mod
    import fintin.core.catchup as cu_mod
    from fintin.adapters.edgar.client import EdgarThrottleError

    _stub_store(monkeypatch)
    monkeypatch.setattr(fi_mod, "fetch_work_candidates", lambda *a, **k: [])
    monkeypatch.setattr(
        cu_mod, "catch_up", _raise(EdgarThrottleError("throttled after retries"))
    )
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY + "\n[universe]\nciks = [320193]\n" + _EDGAR_VALID)
    result = runner.invoke(app, ["catch-up", "--config", str(p)])
    assert result.exit_code == 1
    assert "throttled" in result.output.lower()
    assert "Traceback" not in result.output


def test_catch_up_systemic_abort_exits_1(tmp_path, monkeypatch):
    # A BackfillAborted (too many consecutive failures — e.g. store down) maps to
    # exit 1, not a green "complete".
    import fintin.adapters.edgar.filings_index as fi_mod
    import fintin.core.catchup as cu_mod
    from fintin.core.backfill import BackfillAborted

    _stub_store(monkeypatch)
    monkeypatch.setattr(fi_mod, "fetch_work_candidates", lambda *a, **k: [])
    # Use the engine's real command-neutral abort wording (backfill_universe is
    # shared by both commands) so the test reflects production output, not a
    # fabricated "catch-up aborted…"/"backfill aborted…" string.
    monkeypatch.setattr(
        cu_mod,
        "catch_up",
        _raise(
            BackfillAborted(
                "run aborted after 10 consecutive failures (last: CIK 320193 — "
                "RuntimeError: store down); likely a systemic problem "
                "(e.g. the store) rather than per-company data gaps"
            )
        ),
    )
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY + "\n[universe]\nciks = [320193]\n" + _EDGAR_VALID)
    result = runner.invoke(app, ["catch-up", "--config", str(p)])
    assert result.exit_code == 1
    assert "consecutive failures" in result.output
    assert "backfill" not in result.output.lower()  # no wrong-command wording
    assert "Traceback" not in result.output


def test_catch_up_completed_renders_summary_and_gaps_exit_0(tmp_path, monkeypatch):
    # The COMPLETED render branch needs no live EDGAR — it renders purely from a
    # CatchUpReport. Monkeypatch the engine to RETURN a hand-built COMPLETED report
    # (1 ingested, 1 recorded gap) and assert the GREEN summary + YELLOW gap line +
    # --show-gaps enumeration + exit 0. (Guards the success-render f-strings that
    # the throttle/systemic/NOTHING_TO_DO branches don't exercise.)
    import fintin.adapters.edgar.filings_index as fi_mod
    import fintin.core.catchup as cu_mod
    from fintin.core.backfill import BackfillFailure, BackfillReport
    from fintin.core.catchup import CatchUpReport, CatchUpStatus
    from fintin.core.ingest import IngestResult

    _stub_store(monkeypatch)
    monkeypatch.setattr(fi_mod, "fetch_work_candidates", lambda *a, **k: [])
    ingested = IngestResult(
        cik=2,
        facts_seen=3,
        rows_landed=3,
        dropped_dimensional=0,
        dropped_non_standard=0,
        dropped_non_numeric=0,
        dropped_incomplete=0,
        deduped=0,
        version=1,
    )
    report = CatchUpReport(
        status=CatchUpStatus.COMPLETED,
        scanned=4,
        outstanding=2,
        companies=2,
        backfill=BackfillReport(
            ingested=(ingested,),
            skipped=(),
            failures=(BackfillFailure(1, "RuntimeError: boom"),),
            version=1,
        ),
    )
    monkeypatch.setattr(cu_mod, "catch_up", lambda *a, **k: report)
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY + "\n[universe]\nciks = [320193]\n" + _EDGAR_VALID)
    result = runner.invoke(app, ["catch-up", "--config", str(p), "--show-gaps"])
    assert result.exit_code == 0
    assert "Catch-up complete (STARTED→COMPLETED)" in result.output
    assert "1 company ingested" in result.output  # companies_ingested == 1 → singular
    assert "3 facts landed" in result.output
    assert "2 outstanding filing(s)" in result.output
    assert "recorded as explained gaps" in result.output
    assert "CIK 1: RuntimeError: boom" in result.output  # --show-gaps enumeration
    assert "Traceback" not in result.output


# --- single-flight lease (Story 3.2) -------------------------------------------
# A second trigger while a run holds the shared lease must return ALREADY_RUNNING
# (exit-0) and issue NO EDGAR request. We hold a real FileLease on the configured
# path and monkeypatch discovery/engine to RAISE if reached — proving coalesce
# never touches EDGAR (AC-1, offline, NFR-7).


def _boom_if_called(msg):
    def _f(*a, **k):
        raise AssertionError(msg)

    return _f


def test_catch_up_already_running_coalesces_exit_0(tmp_path, monkeypatch):
    import fintin.adapters.edgar.filings_index as fi_mod
    from fintin.adapters.lease.file_lease import FileLease

    _stub_store(monkeypatch)
    monkeypatch.setattr(
        fi_mod,
        "fetch_work_candidates",
        _boom_if_called("EDGAR discovery must not run while coalesced"),
    )
    lease_path = str(tmp_path / "test.lease")
    holder = FileLease(lease_path, ttl_seconds=120, heartbeat_seconds=15)
    assert holder.acquire() is True
    try:
        p = tmp_path / "fintin.toml"
        p.write_text(
            _CH_ONLY
            + "\n[universe]\nciks = [320193]\n"
            + _EDGAR_VALID
            + f'\n[lease]\npath = "{lease_path}"\n'
        )
        result = runner.invoke(app, ["catch-up", "--config", str(p)])
        assert result.exit_code == 0
        assert "ALREADY_RUNNING" in result.output
        assert "no EDGAR request" in result.output
        assert "Traceback" not in result.output
    finally:
        holder.release()


def test_backfill_already_running_coalesces_exit_0(tmp_path, monkeypatch):
    # AC-6: backfill shares the SAME lease, so it coalesces too (a backfill +
    # catch-up together would double the EDGAR rate — the ban this prevents).
    import fintin.core.backfill as bf_mod
    from fintin.adapters.lease.file_lease import FileLease

    _stub_store(monkeypatch)
    monkeypatch.setattr(
        bf_mod,
        "backfill_universe",
        _boom_if_called("backfill_universe must not run while coalesced"),
    )
    lease_path = str(tmp_path / "test.lease")
    holder = FileLease(lease_path, ttl_seconds=120, heartbeat_seconds=15)
    assert holder.acquire() is True
    try:
        p = tmp_path / "fintin.toml"
        p.write_text(
            _CH_ONLY
            + "\n[universe]\nciks = [320193]\n"
            + _EDGAR_VALID
            + f'\n[lease]\npath = "{lease_path}"\n'
        )
        result = runner.invoke(app, ["backfill", "--config", str(p)])
        assert result.exit_code == 0
        assert "ALREADY_RUNNING" in result.output
        assert "Traceback" not in result.output
    finally:
        holder.release()


# --- recover (Story 3.3) -------------------------------------------------------
# Scoped re-ingest of one company. The happy path hits EDGAR (companyfacts), so
# it's covered offline by test_recover; here we assert the error + ban-safety
# wiring only (NFR-7 — no live EDGAR). No [universe] needed (recover targets a CIK).


def test_help_lists_recover():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "recover" in result.output


def test_recover_invalid_cik_exits_2():
    # CIK validation precedes config load — a bad CIK fails fast (exit 2), offline.
    for bad in ("0", "5000000000"):  # below 1 and above 2**32-1
        result = runner.invoke(app, ["recover", "--cik", bad])
        assert result.exit_code == 2
        assert "Invalid CIK" in result.output
        assert "Traceback" not in result.output


def test_recover_missing_config_exits_2():
    result = runner.invoke(app, ["recover", "--cik", "320193", "--config", "does-not-exist.toml"])
    assert result.exit_code == 2
    assert "Config error" in result.output
    assert "Traceback" not in result.output


def test_recover_missing_edgar_exits_2(tmp_path):
    # No [edgar] — the EdgarClient gate must fail loudly (exit 2) before any EDGAR
    # or ClickHouse access (offline, ban-safe). No [universe] required.
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY)
    result = runner.invoke(app, ["recover", "--cik", "320193", "--config", str(p)])
    assert result.exit_code == 2
    assert "EDGAR config error" in result.output
    assert "Traceback" not in result.output


def test_recover_placeholder_email_exits_2(tmp_path):
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY + _EDGAR_PLACEHOLDER)
    result = runner.invoke(app, ["recover", "--cik", "320193", "--config", str(p)])
    assert result.exit_code == 2
    assert "EDGAR config error" in result.output
    assert "Traceback" not in result.output


def test_recover_no_company_facts_exits_1(tmp_path, monkeypatch):
    # EDGAR has no companyfacts for the CIK → a clean exit 1 (like ingest-company).
    import fintin.core.recover as rec_mod
    from fintin.adapters.edgar.facts import NoCompanyFactsError

    _stub_store(monkeypatch)
    monkeypatch.setattr(
        rec_mod,
        "recover_company",
        _raise(NoCompanyFactsError("no companyfacts for CIK 320193")),
    )
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY + _EDGAR_VALID)
    result = runner.invoke(app, ["recover", "--cik", "320193", "--config", str(p)])
    assert result.exit_code == 1
    assert "companyfacts" in result.output.lower()
    assert "Traceback" not in result.output


def test_recover_throttle_exits_1(tmp_path, monkeypatch):
    import fintin.core.recover as rec_mod
    from fintin.adapters.edgar.client import EdgarThrottleError

    _stub_store(monkeypatch)
    monkeypatch.setattr(
        rec_mod, "recover_company", _raise(EdgarThrottleError("throttled after retries"))
    )
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY + _EDGAR_VALID)
    result = runner.invoke(app, ["recover", "--cik", "320193", "--config", str(p)])
    assert result.exit_code == 1
    assert "throttled" in result.output.lower()
    assert "Traceback" not in result.output


def test_recover_already_running_coalesces_exit_0(tmp_path, monkeypatch):
    # Recover shares the single-flight lease — a live run holding it → ALREADY_RUNNING
    # (exit 0) with NO EDGAR request (recover_company is never reached).
    import fintin.core.recover as rec_mod
    from fintin.adapters.lease.file_lease import FileLease

    _stub_store(monkeypatch)
    monkeypatch.setattr(
        rec_mod,
        "recover_company",
        _boom_if_called("recover_company must not run while coalesced"),
    )
    lease_path = str(tmp_path / "test.lease")
    holder = FileLease(lease_path, ttl_seconds=120, heartbeat_seconds=15)
    assert holder.acquire() is True
    try:
        p = tmp_path / "fintin.toml"
        p.write_text(_CH_ONLY + _EDGAR_VALID + f'\n[lease]\npath = "{lease_path}"\n')
        result = runner.invoke(app, ["recover", "--cik", "320193", "--config", str(p)])
        assert result.exit_code == 0
        assert "ALREADY_RUNNING" in result.output
        assert "Traceback" not in result.output
    finally:
        holder.release()


def _recover_report(cik, *, rows_landed, projected, raw_seen):
    # Build a RecoverReport with the given tallies (the render reads only these).
    from fintin.core.canonical import ProjectResult
    from fintin.core.ingest import IngestResult
    from fintin.core.recover import RecoverReport

    return RecoverReport(
        cik=cik,
        ingest=IngestResult(
            cik=cik,
            facts_seen=rows_landed,
            rows_landed=rows_landed,
            dropped_dimensional=0,
            dropped_non_standard=0,
            dropped_non_numeric=0,
            dropped_incomplete=0,
            deduped=0,
            version=3,
        ),
        project=ProjectResult(cik=cik, raw_seen=raw_seen, projected=projected, version=4),
    )


def test_recover_success_renders_exit_0(tmp_path, monkeypatch):
    # The GREEN success render is offline-testable — monkeypatch the engine to
    # RETURN a report (rows landed) and assert the summary + exit 0.
    import fintin.core.recover as rec_mod

    _stub_store(monkeypatch)
    monkeypatch.setattr(
        rec_mod,
        "recover_company",
        lambda *a, **k: _recover_report(320193, rows_landed=5, projected=5, raw_seen=5),
    )
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY + _EDGAR_VALID)
    result = runner.invoke(app, ["recover", "--cik", "320193", "--config", str(p)])
    assert result.exit_code == 0
    assert "Recovered CIK 320193" in result.output
    assert "5 facts re-ingested" in result.output
    assert "resolution + mart re-derived" in result.output
    assert "Traceback" not in result.output


def test_recover_zero_landed_renders_honestly_exit_0(tmp_path, monkeypatch):
    # When EDGAR returned nothing ingestable, the render must NOT claim a re-ingest /
    # re-derivation that didn't happen — YELLOW "Tier 0 left unchanged", exit 0.
    import fintin.core.recover as rec_mod

    _stub_store(monkeypatch)
    monkeypatch.setattr(
        rec_mod,
        "recover_company",
        lambda *a, **k: _recover_report(320193, rows_landed=0, projected=0, raw_seen=0),
    )
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY + _EDGAR_VALID)
    result = runner.invoke(app, ["recover", "--cik", "320193", "--config", str(p)])
    assert result.exit_code == 0
    assert "no ingestable facts" in result.output
    assert "Tier 0 left unchanged" in result.output
    assert "re-ingested into Tier 0," not in result.output  # not the overclaiming line
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


def test_status_empty_universe_surfaces_gaps_and_exits_1(tmp_path):
    # Unresolvable-only tickers → empty resolved Universe → exit 1, fully offline
    # (resolution reads the bundled table; no [edgar] block / EdgarClient needed).
    # The unresolvable ticker must be NAMED, not silently dropped (SM-2 / P1).
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY + '\n[universe]\ntickers = ["ZZZZINVALID"]\n')
    result = runner.invoke(app, ["status", "--config", str(p)])
    assert result.exit_code == 1
    assert "empty" in result.output
    assert "ZZZZINVALID" in result.output  # the failing ticker is surfaced (P1)
    assert "Traceback" not in result.output


def test_status_connection_failure_exits_1(tmp_path):
    # A config pointing at an unreachable ClickHouse port → check_connection fails
    # → exit 1 "Connection failed". Offline: the connect is simply refused (no
    # server needed) — resolve_universe (ciks only) runs first, then the connection.
    p = tmp_path / "fintin.toml"
    p.write_text(
        '[clickhouse]\nhost = "127.0.0.1"\nport = 59999\n'
        'username = "default"\npassword = ""\ndatabase = "default"\n'
        "\n[universe]\nciks = [320193]\n"
    )
    result = runner.invoke(app, ["status", "--config", str(p)])
    assert result.exit_code == 1
    assert "Connection failed" in result.output
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


@pytest.fixture
def status_db(local_clickhouse_config):
    """A throwaway ClickHouse DB with the schema created; yields (db_name, client).
    Dropped in teardown. Only runs for integration tests that reach it (conftest
    skips them when no server is listening)."""
    import uuid

    from fintin.adapters.store import schema as store_schema
    from fintin.adapters.store.client import get_client

    db = f"fintin_test_{uuid.uuid4().hex[:12]}"
    admin = get_client(local_clickhouse_config)
    try:
        admin.command(f"CREATE DATABASE {db}")
    finally:
        admin.close()
    client = get_client(local_clickhouse_config, database=db)
    try:
        store_schema.create_schema(client)
        yield db, client
    finally:
        client.close()
        cleanup = get_client(local_clickhouse_config)
        try:
            cleanup.command(f"DROP DATABASE IF EXISTS {db}")
        finally:
            cleanup.close()


def _seed_fact(client, cik, filed):
    from datetime import date

    from fintin.adapters.store.raw_fact_repo import insert_raw_facts
    from fintin.core.ingest import RawFactRow

    insert_raw_facts(
        client,
        [
            RawFactRow(
                cik=cik,
                accession=f"{cik:010d}-24-000001",
                raw_tag="us-gaap:Revenues",
                raw_label="Revenues",
                taxonomy="us-gaap",
                period_start=date(2023, 1, 1),
                period_end=date(2023, 12, 31),
                unit="USD",
                value=1000.0,
                form="10-K",
                filed_date=filed,
                content_hash=f"h{cik}",
                taxonomy_version="5.43.0",
                version=1,
            )
        ],
    )


def _write_status_toml(tmp_path, cfg, db, universe_block):
    p = tmp_path / "fintin.toml"
    p.write_text(
        f'[clickhouse]\nhost = "{cfg.host}"\nport = {cfg.port}\n'
        f'username = "{cfg.username}"\npassword = "{cfg.password}"\ndatabase = "{db}"\n'
        + universe_block
    )
    return p


@pytest.mark.integration
def test_status_both_gap_classes_default_vs_show_gaps(
    tmp_path, status_db, local_clickhouse_config
):
    from datetime import date

    db, client = status_db
    _seed_fact(client, 320193, date(2024, 2, 1))  # one in-scope company present
    # In scope: 320193 (present) + 1652044 (absent); plus one unresolvable ticker.
    universe = '\n[universe]\ntickers = ["ZZZZINVALID"]\nciks = [320193, 1652044]\n'
    p = _write_status_toml(tmp_path, local_clickhouse_config, db, universe)

    # Default: counts shown, per-item list omitted.
    res = runner.invoke(app, ["status", "--config", str(p)])
    assert res.exit_code == 0
    assert "1 of 2 in-scope companies present" in res.output
    assert "2 explained gap(s)" in res.output
    assert "1 unresolvable ticker(s), 1 zero-fact company(ies)" in res.output
    assert "ZZZZINVALID" not in res.output  # not enumerated without --show-gaps
    assert "no facts in store" not in res.output
    assert "Traceback" not in res.output

    # --show-gaps: BOTH gap classes enumerated.
    res2 = runner.invoke(app, ["status", "--config", str(p), "--show-gaps"])
    assert res2.exit_code == 0
    assert "ZZZZINVALID: not found" in res2.output  # resolution-gap render branch
    assert "CIK 1652044: no facts in store" in res2.output  # zero-fact render branch


@pytest.mark.integration
def test_status_empty_store_reports_none_hwm(tmp_path, status_db, local_clickhouse_config):
    db, _client = status_db  # schema created, NO rows seeded
    universe = "\n[universe]\nciks = [320193]\n"
    p = _write_status_toml(tmp_path, local_clickhouse_config, db, universe)
    res = runner.invoke(app, ["status", "--config", str(p), "--show-gaps"])
    assert res.exit_code == 0  # a report over an empty store is valid, not an error
    assert "0 of 1 in-scope company present" in res.output
    assert "none (store empty)" in res.output  # the HWM=None render literal
    assert "CIK 320193: no facts in store" in res.output
    assert "Traceback" not in res.output


@pytest.mark.integration
def test_status_fully_covered_has_no_gap_line(tmp_path, status_db, local_clickhouse_config):
    from datetime import date

    db, client = status_db
    _seed_fact(client, 320193, date(2024, 5, 1))
    universe = "\n[universe]\nciks = [320193]\n"
    p = _write_status_toml(tmp_path, local_clickhouse_config, db, universe)
    res = runner.invoke(app, ["status", "--config", str(p)])
    assert res.exit_code == 0
    assert "1 of 1 in-scope company present" in res.output
    assert "2024-05-01" in res.output
    assert "explained gap" not in res.output  # fully covered → no gap line at all
    assert "Traceback" not in res.output


# --- reset (destructive; the --yes guard is the safety-critical part) -----------


def _reset_env(tmp_path, monkeypatch, dropped=("screening_wide", "screening_mart")):
    """A reset whose store calls are stubbed, so the guard/reporting is testable
    without a container."""
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY)
    calls: list[str] = []
    monkeypatch.setattr(app_mod, "check_connection", lambda cfg: "24.1")
    monkeypatch.setattr(app_mod, "get_client", lambda cfg: None)
    monkeypatch.setattr(
        app_mod.store_schema,
        "drop_schema",
        lambda client: (calls.append("drop"), list(dropped))[1],
    )
    monkeypatch.setattr(
        app_mod.store_schema,
        "create_schema",
        lambda client: (calls.append("create"), ["raw_fact"])[1],
    )
    return p, calls


def test_reset_without_yes_drops_nothing(tmp_path, monkeypatch):
    """The guard: no --yes means no DDL is issued at all, and the user is told
    which database and objects were at stake."""
    p, calls = _reset_env(tmp_path, monkeypatch)
    result = runner.invoke(app, ["reset", "--config", str(p)])
    assert result.exit_code == 2
    assert calls == [], "reset issued DDL without --yes"
    assert "Refusing to drop" in result.output
    assert "'default'" in result.output  # names the database it would have wiped
    assert "screening_wide" in result.output  # and the objects
    assert "Traceback" not in result.output


def test_reset_with_yes_drops_and_reports(tmp_path, monkeypatch):
    p, calls = _reset_env(tmp_path, monkeypatch)
    result = runner.invoke(app, ["reset", "--config", str(p), "--yes"])
    assert result.exit_code == 0
    assert calls == ["drop"]  # dropped, did NOT recreate
    assert "Dropped 2 object(s)" in result.output
    assert "schema-init" in result.output  # tells you how to rebuild


def test_reset_recreate_rebuilds_the_empty_schema(tmp_path, monkeypatch):
    p, calls = _reset_env(tmp_path, monkeypatch)
    result = runner.invoke(app, ["reset", "--config", str(p), "--yes", "--recreate"])
    assert result.exit_code == 0
    assert calls == ["drop", "create"]  # order matters
    assert "Recreated empty schema" in result.output


def test_reset_missing_config_reports_clean_error():
    result = runner.invoke(app, ["reset", "--config", "does-not-exist.toml", "--yes"])
    assert result.exit_code == 2
    assert "Config error" in result.output
    assert "Traceback" not in result.output


def test_help_lists_reset():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "reset" in result.output


# --- --debug traceback escape hatch (cross-command) ----------------------------
# Every command's terminal `except Exception` renders a friendly one-liner and
# discards the stack. `--debug` recovers it without changing the default UX.
# `schema-init` stands in for all 13 handlers — they share one funnel
# (`_fail_unexpected`), so testing the funnel once covers the family.


def _boom(*_args, **_kwargs):
    raise RuntimeError("boom")


def _schema_init_that_faults(tmp_path, monkeypatch):
    """A `schema-init` whose DDL step raises an unexpected error: connection and
    client are stubbed so the fault lands in the generic handler, not a typed one."""
    p = tmp_path / "fintin.toml"
    p.write_text(_CH_ONLY)
    monkeypatch.setattr(app_mod, "check_connection", lambda cfg: "24.1")
    monkeypatch.setattr(app_mod, "get_client", lambda cfg: None)
    monkeypatch.setattr(app_mod.store_schema, "create_schema", _boom)
    return p


def test_unexpected_error_shows_one_liner_not_a_traceback(tmp_path, monkeypatch):
    """Default UX is unchanged: a one-line message, never a raw traceback."""
    p = _schema_init_that_faults(tmp_path, monkeypatch)
    result = runner.invoke(app, ["schema-init", "--config", str(p)])
    assert result.exit_code == 1
    assert "Schema init failed: boom" in result.output
    assert "Traceback" not in result.output


def test_debug_flag_recovers_the_discarded_traceback(tmp_path, monkeypatch, caplog):
    """`--debug` logs the stack the friendly one-liner would otherwise throw away,
    while stderr still shows the same one-liner."""
    p = _schema_init_that_faults(tmp_path, monkeypatch)
    result = runner.invoke(app, ["--debug", "schema-init", "--config", str(p)])
    assert result.exit_code == 1
    assert "Schema init failed: boom" in result.output  # friendly line unchanged
    assert "Traceback (most recent call last)" in caplog.text  # stack preserved
    assert "RuntimeError: boom" in caplog.text
    assert "Traceback" not in result.output  # and it stays out of the user's face


def test_without_debug_the_traceback_is_not_logged(tmp_path, monkeypatch, caplog):
    """Negative control: the stack really is suppressed by default, so the test
    above is exercising the flag rather than an unconditional `exc_info=True`."""
    p = _schema_init_that_faults(tmp_path, monkeypatch)
    result = runner.invoke(app, ["schema-init", "--config", str(p)])
    assert result.exit_code == 1
    assert "Traceback (most recent call last)" not in caplog.text


def test_debug_flag_is_documented_in_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "--debug" in result.output
