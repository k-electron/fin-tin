"""The single, compliant, rate-limited EDGAR client (AD-3, FR-1).

This is the **only** module in ``fintin/`` that imports ``edgar`` or issues
EDGAR requests. Ban-avoidance is structural, not a caller's responsibility:

- On construction it configures edgartools' global identity (the declared
  identifying User-Agent) and caps the request rate at **edgartools' own
  throttle** (``EDGAR_RATE_LIMIT_PER_SEC`` + a rebuilt ``HTTP_MGR``) — never a
  hand-rolled per-call limiter (AD-3).
- Every EDGAR call is run through :meth:`EdgarClient.run`, which catches a
  throttle failure (HTTP 429 → ``edgar.httprequests.TooManyRequestsError``),
  honors ``Retry-After`` if the SEC sent one, else self-imposes a ``>= 10``-min
  cool-down, then retries — bounded, so it never crashes the run.

edgartools 5.43.0 realities this wraps (verified against the installed source):
- ``edgar.set_identity(user_identity: str)`` takes one string ``"Name email"``.
- There is **no** ``set_rate_limit()``; the rate lives in the import-time
  ``edgar.httpclient.HTTP_MGR`` singleton, seeded from ``EDGAR_RATE_LIMIT_PER_SEC``
  (default 9). We set the env var and rebuild the manager so a configured (often
  lower, for margin) rate actually takes effect on the live client.
- ``TooManyRequestsError(url, retry_after=None)`` carries ``.retry_after``
  (edgar extracts the ``Retry-After`` header, integer-seconds or HTTP-date).
- ``Accept-Encoding: gzip, deflate`` is httpx's default and is sent as-is (no
  override needed).

Actual EDGAR fetch methods (companyfacts, filings index) are **not** here — they
land in Story 1.4 and call through :meth:`run`. This module delivers the client
and the safe-execution surface only.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Callable, TypeVar

import edgar
from edgar import httpclient
from edgar.httprequests import TooManyRequestsError

from fintin.config import Config, EdgarConfig

logger = logging.getLogger("fintin.edgar")

T = TypeVar("T")

# Obvious placeholders we refuse to send to EDGAR — sending an undeclared/fake
# UA gets the IP flagged as an "Undeclared Automated Tool" (FR-1).
_PLACEHOLDER_EMAILS = frozenset(
    {
        "you@example.com",
        "your.email@example.com",
        "changeme@example.com",
        "example@example.com",
    }
)

# Minimal shape check: a local part, an "@", and a dotted domain.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class EdgarConfigError(Exception):
    """Raised when EDGAR config is missing or unusable for a compliant request
    (no ``[edgar]`` block, blank name, or blank/malformed/placeholder email)."""


class EdgarThrottleError(Exception):
    """Raised when EDGAR throttling persists after the configured cool-down retries."""


class EdgarClient:
    """The one doorway to EDGAR. Construct it from the loaded :class:`Config`;
    it configures edgartools and gives you :meth:`run` to execute EDGAR calls
    under the fair-access policy."""

    def __init__(self, config: Config, *, sleep: Callable[[float], None] = time.sleep) -> None:
        edgar_cfg = self._require_valid_config(config)
        self._cfg: EdgarConfig = edgar_cfg
        self._sleep = sleep

        # Declared identifying User-Agent (AD-3, FR-1): "Name email".
        self.identity = f"{edgar_cfg.user_agent_name.strip()} {edgar_cfg.contact_email.strip()}"
        edgar.set_identity(self.identity)

        # Cap the rate at edgartools' own throttle. edgartools reads the ceiling
        # as an int req/s; sub-1 rates can't be expressed there.
        rate = int(edgar_cfg.rate_limit_per_sec)
        if rate < 1:
            raise EdgarConfigError(
                f"rate_limit_per_sec must be >= 1 req/s to configure edgartools' "
                f"throttle (got {edgar_cfg.rate_limit_per_sec})."
            )
        self.rate_limit_per_sec = rate
        os.environ["EDGAR_RATE_LIMIT_PER_SEC"] = str(rate)
        # Rebuild the singleton so the live client reflects the configured rate
        # even if edgar was imported earlier with its default. http_client() reads
        # this module global per request, so the reassignment propagates.
        httpclient.HTTP_MGR = httpclient.get_http_mgr(request_per_sec_limit=rate)

        logger.info(
            "EDGAR client configured: user_agent_name=%r rate=%d req/s cooldown=%ds",
            edgar_cfg.user_agent_name,
            rate,
            edgar_cfg.cooldown_seconds,
        )
        logger.debug("EDGAR declared identity: %s", self.identity)

    @staticmethod
    def _require_valid_config(config: Config) -> EdgarConfig:
        """Ban-safety gate (FR-1): refuse to construct without a real identity."""
        cfg = getattr(config, "edgar", None)
        if cfg is None:
            raise EdgarConfigError(
                "No [edgar] config present — set user_agent_name and a real "
                "contact_email in fintin.toml before touching EDGAR."
            )
        if not cfg.user_agent_name.strip():
            raise EdgarConfigError("[edgar].user_agent_name must not be blank.")
        email = cfg.contact_email.strip()
        if not email or not _EMAIL_RE.match(email):
            raise EdgarConfigError(
                f"[edgar].contact_email must be a real email address, got {cfg.contact_email!r}."
            )
        if email.lower() in _PLACEHOLDER_EMAILS:
            raise EdgarConfigError(
                f"[edgar].contact_email is still the placeholder {email!r} — set your "
                f"real address (EDGAR rejects an undeclared/placeholder User-Agent)."
            )
        return cfg

    def run(self, operation: Callable[[], T], *, description: str = "EDGAR request") -> T:
        """Execute an EDGAR ``operation`` under the fair-access cool-down policy.

        On a throttle failure (HTTP 429), waits ``Retry-After`` if the SEC sent
        one, else ``cooldown_seconds`` (>= 10 min), then retries — up to
        ``max_throttle_retries`` times. Exhausting the retries raises
        :class:`EdgarThrottleError` (a typed, catchable error) rather than
        letting the run crash. Non-throttle errors propagate unchanged."""
        retries = self._cfg.max_throttle_retries
        for attempt in range(retries + 1):  # initial try + `retries` retries
            try:
                return operation()
            except TooManyRequestsError as exc:
                if attempt >= retries:
                    raise EdgarThrottleError(
                        f"EDGAR still throttling {description} after "
                        f"{retries} cool-down retr{'y' if retries == 1 else 'ies'}."
                    ) from exc
                wait = exc.retry_after if exc.retry_after else self._cfg.cooldown_seconds
                logger.warning(
                    "EDGAR throttled on %s; cooling down %ss then retrying (%d/%d)%s",
                    description,
                    wait,
                    attempt + 1,
                    retries,
                    " [Retry-After]" if exc.retry_after else "",
                )
                self._sleep(wait)
        raise AssertionError("unreachable")  # loop always returns or raises
