"""Config loader tests (no container required)."""

from __future__ import annotations

import textwrap

import pytest

from fintin.config import ConfigError, EdgarConfig, load_config

_VALID = textwrap.dedent(
    """
    [clickhouse]
    host = "localhost"
    port = 8123
    username = "default"
    password = ""
    database = "default"
    """
)


def _with_edgar(edgar_block: str) -> str:
    """A valid [clickhouse] block plus the given [edgar] block."""
    return _VALID + textwrap.dedent(edgar_block)


_FULL_EDGAR = """
    [edgar]
    user_agent_name = "fin-tin"
    contact_email = "you@example.com"
    rate_limit_per_sec = 10
    cooldown_seconds = 600
    max_throttle_retries = 3
    """


def test_valid_config(tmp_path):
    p = tmp_path / "fintin.toml"
    p.write_text(_VALID)
    cfg = load_config(p)
    assert cfg.clickhouse.host == "localhost"
    assert cfg.clickhouse.port == 8123
    assert cfg.clickhouse.username == "default"
    assert cfg.clickhouse.password == ""
    assert cfg.clickhouse.database == "default"


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.toml")


def test_malformed_toml_raises(tmp_path):
    p = tmp_path / "fintin.toml"
    p.write_text("this is = = not valid toml [[[")
    with pytest.raises(ConfigError):
        load_config(p)


def test_missing_clickhouse_section_raises(tmp_path):
    p = tmp_path / "fintin.toml"
    p.write_text('[other]\nkey = "value"\n')
    with pytest.raises(ConfigError):
        load_config(p)


def test_missing_required_key_raises(tmp_path):
    p = tmp_path / "fintin.toml"
    p.write_text('[clickhouse]\nhost = "localhost"\n')  # missing port, etc.
    with pytest.raises(ConfigError):
        load_config(p)


def test_non_integer_port_raises(tmp_path):
    p = tmp_path / "fintin.toml"
    p.write_text(
        '[clickhouse]\nhost = "localhost"\nport = "8123"\n'
        'username = "default"\npassword = ""\ndatabase = "default"\n'
    )
    with pytest.raises(ConfigError):
        load_config(p)


def test_boolean_port_raises(tmp_path):
    p = tmp_path / "fintin.toml"
    p.write_text(
        '[clickhouse]\nhost = "localhost"\nport = true\n'
        'username = "default"\npassword = ""\ndatabase = "default"\n'
    )
    with pytest.raises(ConfigError):
        load_config(p)


def test_out_of_range_port_raises(tmp_path):
    p = tmp_path / "fintin.toml"
    p.write_text(
        '[clickhouse]\nhost = "localhost"\nport = 70000\n'
        'username = "default"\npassword = ""\ndatabase = "default"\n'
    )
    with pytest.raises(ConfigError):
        load_config(p)


def test_non_string_host_raises(tmp_path):
    p = tmp_path / "fintin.toml"
    p.write_text(
        '[clickhouse]\nhost = 123\nport = 8123\n'
        'username = "default"\npassword = ""\ndatabase = "default"\n'
    )
    with pytest.raises(ConfigError):
        load_config(p)


def test_utf8_bom_is_tolerated(tmp_path):
    p = tmp_path / "fintin.toml"
    p.write_bytes(b"\xef\xbb\xbf" + _VALID.encode("utf-8"))
    cfg = load_config(p)
    assert cfg.clickhouse.host == "localhost"
    assert cfg.clickhouse.port == 8123


# --- [edgar] block (Story 1.3) -------------------------------------------------
# Load-time validation is STRUCTURE + TYPES + RANGES only. The ban-safety
# semantic gate (blank / malformed / placeholder email) lives in EdgarClient
# construction, NOT here — see tests/test_edgar_client.py. So a well-formed
# block carrying the *placeholder* email MUST load cleanly (regression guard:
# otherwise check-connection / schema-init / conftest would all break).


def test_edgar_absent_is_none(tmp_path):
    p = tmp_path / "fintin.toml"
    p.write_text(_VALID)
    cfg = load_config(p)
    assert cfg.edgar is None


def test_edgar_placeholder_email_loads_cleanly(tmp_path):
    p = tmp_path / "fintin.toml"
    p.write_text(_with_edgar(_FULL_EDGAR))
    cfg = load_config(p)
    assert isinstance(cfg.edgar, EdgarConfig)
    assert cfg.edgar.user_agent_name == "fin-tin"
    assert cfg.edgar.contact_email == "you@example.com"  # placeholder — loads fine
    assert cfg.edgar.rate_limit_per_sec == 10
    assert cfg.edgar.cooldown_seconds == 600
    assert cfg.edgar.max_throttle_retries == 3


def test_edgar_defaults_applied(tmp_path):
    p = tmp_path / "fintin.toml"
    p.write_text(
        _with_edgar(
            """
            [edgar]
            user_agent_name = "fin-tin"
            contact_email = "you@example.com"
            """
        )
    )
    cfg = load_config(p)
    assert cfg.edgar.rate_limit_per_sec == 10.0
    assert cfg.edgar.cooldown_seconds == 600
    assert cfg.edgar.max_throttle_retries == 3


@pytest.mark.parametrize(
    "block",
    [
        # rate out of range / wrong type
        '[edgar]\nuser_agent_name="x"\ncontact_email="a@b.co"\nrate_limit_per_sec=11\n',
        '[edgar]\nuser_agent_name="x"\ncontact_email="a@b.co"\nrate_limit_per_sec=0\n',
        '[edgar]\nuser_agent_name="x"\ncontact_email="a@b.co"\nrate_limit_per_sec=true\n',
        # cooldown below the 10-min floor / wrong type
        '[edgar]\nuser_agent_name="x"\ncontact_email="a@b.co"\ncooldown_seconds=300\n',
        '[edgar]\nuser_agent_name="x"\ncontact_email="a@b.co"\ncooldown_seconds=true\n',
        # retries wrong type / negative
        '[edgar]\nuser_agent_name="x"\ncontact_email="a@b.co"\nmax_throttle_retries=true\n',
        '[edgar]\nuser_agent_name="x"\ncontact_email="a@b.co"\nmax_throttle_retries=-1\n',
        # required keys missing / wrong type
        '[edgar]\ncontact_email="a@b.co"\n',  # no user_agent_name
        '[edgar]\nuser_agent_name="x"\n',  # no contact_email
        '[edgar]\nuser_agent_name="x"\ncontact_email=123\n',  # email not a string
    ],
)
def test_edgar_structural_validation_rejects(tmp_path, block):
    p = tmp_path / "fintin.toml"
    p.write_text(_VALID + "\n" + textwrap.dedent(block))
    with pytest.raises(ConfigError):
        load_config(p)
