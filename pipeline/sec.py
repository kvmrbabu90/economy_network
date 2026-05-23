"""SEC HTTP client with a single global rate limiter and required User-Agent.

CLAUDE.md invariant #7: every EDGAR request must send a descriptive User-Agent
and the fetcher must rate-limit to <=10 requests/second. The SEC enforces the
limit per source-IP across BOTH data.sec.gov and www.sec.gov, so a single
process-wide limiter (not per-host) is the correct shape.

All sec.gov traffic in this project must go through `sec_get()`. Do not call
`requests.get()` directly on a sec.gov URL elsewhere.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Optional
from urllib.parse import urlsplit

import requests


SEC_HOSTS = {"www.sec.gov", "data.sec.gov", "sec.gov"}
_MAX_REQ_PER_SEC = 10.0


class EdgarConfigError(RuntimeError):
    """Raised when EDGAR_USER_AGENT is missing or empty."""


def get_user_agent() -> str:
    """Read EDGAR_USER_AGENT from the environment. Fail loudly if missing.

    SEC requires a real contact (name + email). We deliberately do NOT fall
    back to a placeholder — that would risk getting the project IP-banned and
    violates CLAUDE.md invariant #7.
    """
    ua = os.environ.get("EDGAR_USER_AGENT", "").strip()
    if not ua:
        raise EdgarConfigError(
            "EDGAR_USER_AGENT environment variable is missing or empty.\n"
            "SEC requires every request to include a descriptive User-Agent "
            "with a real name + email, e.g.:\n"
            '  PowerShell: $env:EDGAR_USER_AGENT = "Your Name your@email.com"\n'
            '  bash:       export EDGAR_USER_AGENT="Your Name your@email.com"\n'
            "Set it before re-running the ingest pipeline."
        )
    return ua


class _GlobalRateLimiter:
    """Thread-safe min-interval throttle. <=10/s means >=0.1s spacing."""

    def __init__(self, max_per_sec: float = _MAX_REQ_PER_SEC) -> None:
        self.min_interval = 1.0 / float(max_per_sec)
        self._last = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self.min_interval - (now - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()


# Module-level singleton: ALL sec.gov requests in this process share it.
EDGAR_LIMITER = _GlobalRateLimiter(_MAX_REQ_PER_SEC)

_session: Optional[requests.Session] = None
_session_lock = threading.Lock()


def _get_session() -> requests.Session:
    global _session
    with _session_lock:
        if _session is None:
            s = requests.Session()
            s.headers.update(
                {
                    "User-Agent": get_user_agent(),
                    "Accept-Encoding": "gzip, deflate",
                    "Host": "www.sec.gov",  # overridden per-request below
                }
            )
            _session = s
        return _session


def _is_sec_host(url: str) -> bool:
    return urlsplit(url).hostname in SEC_HOSTS


def sec_get(url: str, *, timeout: float = 30.0, **kwargs) -> requests.Response:
    """Throttled GET against any sec.gov host. Use this everywhere.

    Calling with a non-sec.gov URL raises — we want a clear blast radius for
    the rate limiter; non-SEC requests (e.g. Wikipedia) go through their own
    callsite.
    """
    if not _is_sec_host(url):
        raise ValueError(
            f"sec_get() called with non-sec.gov URL {url!r}; route other hosts elsewhere"
        )
    EDGAR_LIMITER.acquire()
    sess = _get_session()
    # Per-request Host header so both www.sec.gov and data.sec.gov resolve cleanly.
    host = urlsplit(url).hostname
    headers = kwargs.pop("headers", {}) or {}
    headers.setdefault("Host", host)
    resp = sess.get(url, headers=headers, timeout=timeout, **kwargs)
    return resp
