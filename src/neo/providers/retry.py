"""Retry helpers shared by all providers."""

from __future__ import annotations

import random
import re

_RETRY_PATTERNS = re.compile(
    r"rate[-_ ]?limit|too many requests|overloaded|temporarily unavailable|"
    r"try again|timeout|timed out|service unavailable|bad gateway|"
    r"connection (?:reset|refused|aborted)|socket|econnreset|econnrefused|"
    r"429|503|502|504",
    re.IGNORECASE,
)


def is_retryable(status: int | None, text: str) -> bool:
    """True when a failure looks transient and worth retrying.

    - No status (connection-level failure) -> retryable.
    - 429 and 5xx -> retryable.
    - Otherwise retryable only when the body text matches a
      rate-limit/overload/timeout pattern.
    """
    if status is None:
        return True
    if status == 429 or 500 <= status < 600:
        return True
    return bool(text and _RETRY_PATTERNS.search(text))


def compute_delay(attempt: int) -> float:
    """Exponential backoff with jitter: min(30, 2.0 * 2**attempt * (1 + 0.25*jitter))."""
    jitter = random.random()  # nosec - backoff jitter, not crypto
    delay = 2.0 * (2.0**max(0, attempt)) * (1.0 + 0.25 * jitter)
    return min(30.0, delay)
