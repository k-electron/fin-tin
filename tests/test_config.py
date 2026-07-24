"""Config loader tests (no container required)."""

from __future__ import annotations

import textwrap

import pytest

from fintin.config import ConfigError, load_config

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
