"""When to try again, and how long to wait.

Retried: connection failures and timeouts, ``429`` and ``5xx``. Everything else is a refusal that the same request would get again.
Every call this SDK makes is safe to repeat: an event carries an idempotency key (generated if the caller gave none, and kept
across the retries), so a retry after a lost response cannot send a notification twice, and a preference link is only a link.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class RetryPolicy:
    #: Retries after the first attempt. ``0`` turns retrying off.
    max_retries: int = 3
    #: The first backoff ceiling, in seconds; it doubles with every retry.
    backoff_base: float = 0.5
    #: No backoff ceiling is ever larger than this.
    backoff_cap: float = 8.0
    #: The longest ``Retry-After`` that is waited out. A server asking for more than this gets the error raised instead: a
    #: request handler that sleeps for ten minutes is worse than one that fails.
    max_retry_after: float = 30.0

    def delay(
        self, retries_so_far: int, retry_after: Optional[float], rng: Callable[[], float] = random.random
    ) -> Optional[float]:
        """Seconds to wait before the next attempt, or ``None`` to stop and raise.

        ``retries_so_far`` is how many retries have already been made. A ``Retry-After`` from the server is honoured exactly
        (it is the server's own estimate, and adding jitter would only make it wait longer than it asked); otherwise the wait
        is exponential with equal jitter, so that a fleet of callers that failed together does not retry together and no
        retry is near-instant.
        """
        if retries_so_far >= self.max_retries:
            return None
        if retry_after is not None:
            return retry_after if retry_after <= self.max_retry_after else None
        ceiling = min(self.backoff_cap, self.backoff_base * float(2**retries_so_far))
        return ceiling / 2 + rng() * ceiling / 2


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    """``Retry-After`` in seconds. Hermesi sends seconds and never an HTTP date; anything else is ignored."""
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def is_retryable_status(status: int) -> bool:
    return status == 429 or status >= 500
