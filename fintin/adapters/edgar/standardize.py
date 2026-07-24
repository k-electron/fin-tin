"""Offline concept standardization — Tier 0 raw tag → canonical concept (AD-9).

Uses edgartools' bundled XBRL standardization taxonomy
(``edgar.xbrl.standardization.reverse_index``), which maps a raw us-gaap/dei/srt
concept to a canonical concept id from local JSON — **zero network**. This is the
Tier 0 → Tier 1 mapping's only edgartools dependency; unlike the ingest path it
constructs no ``EdgarClient``, sets no identity, and issues no request (FR-4/AD-4).

NOTE: this module is a distinct, pure-offline ``edgar`` importer. It deliberately
does NOT reuse ``adapters.edgar.facts.edgartools_version`` so the map path never
pulls in the rate-limited client module.
"""

from __future__ import annotations

import edgar
from edgar.xbrl.standardization.reverse_index import get_standard_concept


def _local_name(raw_tag: str) -> str:
    """Strip a namespace prefix (``us-gaap:Assets`` → ``Assets``). The reverse
    index strips ``us-gaap:``/``dei:``/``ifrs:`` itself but NOT ``srt:``, so we
    normalize every namespace uniformly here (srt axes then resolve to ``None``,
    as intended — they are dimensions, not canonical facts)."""
    return raw_tag.split(":", 1)[1] if ":" in raw_tag else raw_tag


def standardize_concept(raw_tag: str) -> str | None:
    """Return the canonical concept id for a raw tag, or ``None`` when edgartools
    cannot standardize it (unknown or excluded). ``None`` is the AC-2 signal: no
    ``canonical_fact`` row is produced. Purely offline (bundled-JSON lookup).

    Ambiguous tags (a handful map to multiple candidates) resolve deterministically
    to their primary candidate — they DO map; context-based disambiguation is
    deferred (v1)."""
    return get_standard_concept(_local_name(raw_tag))


def taxonomy_version() -> str:
    """The edgartools package version, stamped as ``taxonomy_version`` on every
    Tier 1 row (AD-9/AD-14). The bundled standardization mapping is pinned by this
    package version."""
    return edgar.__version__
