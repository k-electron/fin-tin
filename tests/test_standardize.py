"""Offline standardization adapter tests.

The standardizer is fully offline (edgartools' bundled JSON), so these call the
real edgartools mapping directly — no network, no live EDGAR (NFR-7). One test
blocks all outbound sockets to PROVE the zero-network property (AC-1).
"""

from __future__ import annotations

import socket

import edgar

from fintin.adapters.edgar.standardize import standardize_concept, taxonomy_version


def test_maps_known_us_gaap_concepts():
    # The four screening_mart concepts, verified against edgartools 5.43.0 output.
    assert standardize_concept("us-gaap:Assets") == "Assets"
    assert standardize_concept("us-gaap:Revenues") == "Revenue"
    assert standardize_concept("us-gaap:NetIncomeLoss") == "NetIncome"
    assert standardize_concept("us-gaap:Liabilities") == "Liabilities"


def test_prefix_strip_parity():
    # Namespaced and bare forms resolve identically (adapter strips the prefix).
    assert standardize_concept("us-gaap:Assets") == standardize_concept("Assets") == "Assets"


def test_unmappable_returns_none():
    # The AC-2 signal: unknown tag → no canonical row.
    assert standardize_concept("us-gaap:ZzzFakeConceptXyz") is None
    assert standardize_concept("ZzzFakeConceptXyz") is None


def test_srt_axis_not_mapped():
    # srt dimensions/axes are not canonical facts (the adapter strips 'srt:' too).
    assert standardize_concept("srt:CumulativeEffectPeriodOfAdoptionAxis") is None


def test_taxonomy_version_is_package_version():
    assert taxonomy_version() == edgar.__version__


def test_standardize_is_offline(monkeypatch):
    """AC-1: mapping issues zero network requests. Block all outbound connections
    and confirm a lookup still succeeds (bundled-JSON only). Warm the singleton
    first so an unrelated first-call file read isn't what we're measuring."""
    standardize_concept("us-gaap:Assets")  # warm the reverse-index singleton

    def _blocked(*a, **k):
        raise OSError("network blocked in test (offline proof)")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)

    assert standardize_concept("us-gaap:Revenues") == "Revenue"
    assert standardize_concept("us-gaap:ZzzFakeConceptXyz") is None
