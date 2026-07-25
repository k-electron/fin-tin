"""Constituent-list fetch — the outward adapter for `core.constituents`.

A single HTTP GET for a public CSV of index members. Deliberately **not** EDGAR:
the SEC does not publish S&P 500 membership (it is S&P's index), so the list has
to come from elsewhere. That also means none of the EDGAR fair-access machinery
(the rate limiter, the cool-down, the contact-email gate) applies here — this
request never touches sec.gov, and it must not be routed through `EdgarClient`,
which would consume that budget.

Uses `urllib.request` from the standard library rather than adding an HTTP
dependency for one GET (`httpx` is present only transitively, via edgartools;
depending on it directly would be depending on someone else's dependency).

The default source is pinned below and overridable via
``[universe].constituents_url`` — a third-party URL can move, and an operator who
prefers their own list should not have to patch the code.
"""

from __future__ import annotations

import urllib.error
import urllib.request

# Wikipedia's S&P 500 table, maintained as CSV. Carries Symbol AND CIK columns,
# so a symbol edgartools can't resolve offline still has an exact CIK to fall
# back on (see `core.constituents`).
DEFAULT_CONSTITUENTS_URL = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
    "main/data/constituents.csv"
)

# Politeness: identify the tool to the host we're fetching from, same principle as
# the EDGAR User-Agent even though this host imposes no such requirement.
_USER_AGENT = "fin-tin (github.com/k-electron/fin-tin)"

# A constituent list is ~30 KB. Cap the read so a redirect to something enormous
# can't exhaust memory, and bound the wait so a hung host can't stall the CLI.
_MAX_BYTES = 8 * 1024 * 1024
_TIMEOUT_SECONDS = 30


class ConstituentFetchError(Exception):
    """The constituent list could not be fetched — rendered as a clean CLI error."""


def fetch_constituents_csv(
    url: str = DEFAULT_CONSTITUENTS_URL, *, timeout: int = _TIMEOUT_SECONDS
) -> str:
    """GET ``url`` and return the body as text.

    Every failure mode (bad scheme, DNS, HTTP status, timeout, oversized body,
    undecodable bytes) becomes a :class:`ConstituentFetchError` carrying the URL,
    so the CLI renders one clear line instead of a urllib traceback."""
    if not url.lower().startswith(("http://", "https://")):
        # Without this, urllib would happily open file:// or ftp:// from config.
        raise ConstituentFetchError(
            f"constituent URL must be http(s), got {url!r}"
        )

    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(_MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise ConstituentFetchError(
            f"constituent source returned HTTP {exc.code} for {url}"
        ) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ConstituentFetchError(f"could not fetch {url}: {exc}") from exc

    if len(raw) > _MAX_BYTES:
        raise ConstituentFetchError(
            f"constituent source at {url} exceeded {_MAX_BYTES} bytes — "
            "that is not a constituent list"
        )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConstituentFetchError(
            f"constituent source at {url} is not valid UTF-8 text"
        ) from exc
