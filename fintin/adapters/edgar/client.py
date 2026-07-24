"""The single, compliant, rate-limited EDGAR client (AD-3, FR-1).

This is the **only** module in ``fintin/`` that imports ``edgar`` or issues
EDGAR requests. Ban-avoidance is structural, not a caller's responsibility:

- On construction it configures edgartools' global identity (the declared
  identifying User-Agent) and caps the request rate at **edgartools' own
  throttle** (``EDGAR_RATE_LIMIT_PER_SEC`` + a rebuilt ``HTTP_MGR``) — never a
  hand-rolled per-call limiter (AD-3). The full ``[edgar]`` config is validated
  *before* any edgar global is touched, so a bad config never leaves a
  half-configured process.
- Every EDGAR call is run through :meth:`EdgarClient.run`, which catches a
  throttle failure (HTTP 429 → ``edgar.httprequests.TooManyRequestsError``) and
  waits at least the ``>= 10``-minute cool-down — honoring a *longer*
  ``Retry-After`` if the SEC sent one, never a shorter one (retrying inside the
  SEC block extends it) — then retries, bounded, so it never crashes the run.

edgartools 5.43.0 realities this wraps (verified against the installed source):
- ``edgar.set_identity(user_identity: str)`` takes one string ``"Name email"``.
- There is **no** ``set_rate_limit()``; the rate lives in the import-time
  ``edgar.httpclient.HTTP_MGR`` singleton, seeded from ``EDGAR_RATE_LIMIT_PER_SEC``
  (default 9). We set the env var and rebuild the manager so a configured (often
  lower, for margin) rate actually takes effect on the live client.
- ``TooManyRequestsError(url, retry_after=None)`` carries ``.retry_after`` —
  edgar extracts and normalizes the ``Retry-After`` header to integer seconds.
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

# Obvious placeholder addresses we refuse to send to EDGAR — sending an
# undeclared/fake UA gets the IP flagged as an "Undeclared Automated Tool" (FR-1).
_PLACEHOLDER_EMAILS = frozenset(
    {
        "you@example.com",
        "your.email@example.com",
        "changeme@example.com",
        "example@example.com",
    }
)
# RFC 2606 / 6761 reserved names — never a real, reachable contact.
_RESERVED_EMAIL_DOMAINS = frozenset({"example.com", "example.org", "example.net"})
_RESERVED_EMAIL_TLDS = (".example", ".invalid", ".test", ".localhost")

# Minimal shape check: a local part, an "@", and a dotted domain.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# A single throttle cool-down never waits longer than this — a hostile/garbage
# Retry-After (huge int or far-future date) must not wedge the run for years.
_MAX_COOLDOWN_SECONDS = 24 * 60 * 60  # 24h — far above any real SEC block (~10 min)


class EdgarConfigError(Exception):
    """Raised when EDGAR config is missing or unusable for a compliant request
    (no ``[edgar]`` block; blank/control-char name; blank/malformed/placeholder
    email; or a rate/cool-down/retry value that would be unsafe)."""


class EdgarThrottleError(Exception):
    """Raised when EDGAR throttling persists after the configured cool-down retries."""


def _mask_email(email: str) -> str:
    """Mask the local part of an email for logging (public-repo email privacy)."""
    local, sep, domain = email.partition("@")
    if not sep:
        return "***"
    return f"{local[:1]}***@{domain}"


class EdgarClient:
    """The one doorway to EDGAR. Construct it from the loaded :class:`Config`;
    it configures edgartools and gives you :meth:`run` to execute EDGAR calls
    under the fair-access policy."""

    def __init__(self, config: Config, *, sleep: Callable[[float], None] = time.sleep) -> None:
        # Validate EVERYTHING before mutating any edgar global, so a rejected
        # config never leaves a half-configured process (identity set, rate not).
        edgar_cfg, rate = self._require_valid_config(config)
        self._cfg: EdgarConfig = edgar_cfg
        self._sleep = sleep
        self.rate_limit_per_sec = rate

        # Declared identifying User-Agent (AD-3, FR-1): "Name email".
        self.identity = f"{edgar_cfg.user_agent_name.strip()} {edgar_cfg.contact_email.strip()}"
        edgar.set_identity(self.identity)

        # Cap the rate at edgartools' own throttle. edgar reads the ceiling from
        # EDGAR_RATE_LIMIT_PER_SEC into HTTP_MGR at import; set the env AND rebuild
        # the singleton so the live client reflects the configured rate even if
        # edgar was imported earlier with its default. http_client() reads this
        # module global per request, so the reassignment propagates.
        os.environ["EDGAR_RATE_LIMIT_PER_SEC"] = str(rate)
        httpclient.HTTP_MGR = httpclient.get_http_mgr(request_per_sec_limit=rate)

        logger.info(
            "EDGAR client configured: user_agent_name=%r rate=%d req/s cooldown=%ds",
            edgar_cfg.user_agent_name,
            rate,
            edgar_cfg.cooldown_seconds,
        )
        logger.debug(
            "EDGAR declared identity: %s <%s>",
            edgar_cfg.user_agent_name.strip(),
            _mask_email(edgar_cfg.contact_email.strip()),
        )

    @classmethod
    def _require_valid_config(cls, config: Config) -> tuple[EdgarConfig, int]:
        """Ban-safety gate (FR-1): refuse to construct with anything that could
        send a non-compliant request. Validates the full ``[edgar]`` config —
        including the ban-safety floors, re-checked here so a directly-built
        ``EdgarConfig`` (which bypasses ``load_config``) can't smuggle in an
        unsafe value — and returns the validated integer rate. Runs before any
        edgar global is mutated."""
        cfg = getattr(config, "edgar", None)
        if cfg is None:
            raise EdgarConfigError(
                "No [edgar] config present — set user_agent_name and a real "
                "contact_email in fintin.toml before touching EDGAR."
            )
        if not cfg.user_agent_name.strip():
            raise EdgarConfigError("[edgar].user_agent_name must not be blank.")
        if not cfg.user_agent_name.isprintable():
            raise EdgarConfigError(
                "[edgar].user_agent_name must not contain control characters or "
                "newlines (they corrupt the User-Agent header)."
            )
        email = cfg.contact_email.strip()
        if not email or not _EMAIL_RE.match(email):
            raise EdgarConfigError(
                f"[edgar].contact_email must be a real email address, got {cfg.contact_email!r}."
            )
        if cls._is_placeholder_email(email):
            raise EdgarConfigError(
                f"[edgar].contact_email is still a placeholder/reserved address ({email!r}) — "
                f"set your real address (EDGAR rejects an undeclared/placeholder User-Agent)."
            )
        if cfg.cooldown_seconds < 600:
            raise EdgarConfigError(
                f"[edgar].cooldown_seconds must be >= 600 (SEC 10-minute cool-down), "
                f"got {cfg.cooldown_seconds}."
            )
        if cfg.max_throttle_retries < 0:
            raise EdgarConfigError(
                f"[edgar].max_throttle_retries must be >= 0, got {cfg.max_throttle_retries}."
            )
        rate = int(cfg.rate_limit_per_sec)
        if not (1 <= rate <= 10):
            raise EdgarConfigError(
                f"[edgar].rate_limit_per_sec must be in [1, 10] req/s (SEC max is 10), "
                f"got {cfg.rate_limit_per_sec}."
            )
        return cfg, rate

    @staticmethod
    def _is_placeholder_email(email: str) -> bool:
        """True for obvious placeholders and RFC-2606/6761 reserved addresses —
        anything that is provably not a real, reachable contact."""
        e = email.lower()
        if e in _PLACEHOLDER_EMAILS:
            return True
        domain = e.rsplit("@", 1)[-1]
        if domain in _RESERVED_EMAIL_DOMAINS:
            return True
        return any(domain == tld[1:] or domain.endswith(tld) for tld in _RESERVED_EMAIL_TLDS)

    def run(self, operation: Callable[[], T], *, description: str = "EDGAR request") -> T:
        """Execute an EDGAR ``operation`` under the fair-access cool-down policy.

        On a throttle failure (HTTP 429), waits **at least** ``cooldown_seconds``
        (>= 10 min) — honoring a *longer* ``Retry-After`` if the SEC sent one, but
        never a shorter one (retrying inside the SEC block extends it) — capped so
        a garbage header can't wedge the run, then retries up to
        ``max_throttle_retries`` times. Exhausting the retries raises
        :class:`EdgarThrottleError` (a typed, catchable error) rather than letting
        the run crash. Non-throttle errors propagate unchanged."""
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
                # Ban-safety: never wait LESS than the >=10-min floor (a retry
                # inside the SEC block extends it); honor a LONGER Retry-After;
                # cap so a hostile/garbage header can't wedge the run for years.
                floor = self._cfg.cooldown_seconds
                honoring = bool(exc.retry_after and exc.retry_after > floor)
                wait = min(max(exc.retry_after or 0, floor), _MAX_COOLDOWN_SECONDS)
                logger.warning(
                    "EDGAR throttled on %s; cooling down %ss then retrying (%d/%d)%s",
                    description,
                    wait,
                    attempt + 1,
                    retries,
                    " [honoring Retry-After]" if honoring else "",
                )
                # AD-12 forward-hook: a run inside an EDGAR cool-down must keep the
                # (future) single-flight lease heartbeating (Epic 3). `sleep` is
                # injectable precisely so a heartbeating/interruptible sleeper can
                # replace time.sleep here without touching this policy.
                self._sleep(wait)
        raise AssertionError("unreachable")  # loop always returns or raises
