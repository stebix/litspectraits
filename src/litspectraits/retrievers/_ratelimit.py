"""Per-publisher token bucket (``docs/overview-v3.md`` §8).

Single-process, asyncio-only. Each :class:`RateLimiter` enforces a
caller-supplied ceiling in requests per second by sleeping the calling
task long enough that the *average* call rate never exceeds the
configured rate. There is no burst credit and no queue: callers are
serialized by an :class:`asyncio.Lock`, then released in arrival order.

For single-DOI ingest the bucket is essentially a no-op (one token
consumed, never throttled). It comes alive for the future
``litspectraits ingest --batch <doi-list.txt>`` flow, where it keeps us
politely below each publisher's published or assumed ceiling.

The implementation chooses simplicity over throughput: there is exactly
one tunable (the rate) and one piece of state (the next-available
monotonic timestamp). A leaky-bucket variant with burst credit is *not*
warranted here — TDM endpoints do not advertise burst windows we could
exploit safely.
"""

import asyncio
from typing import Final

from litspectraits.manifest import Publisher


class RateLimiter:
    """Token bucket: at most ``rate_per_second`` acquisitions per second.

    Parameters
    ----------
    rate_per_second : float
        Steady-state ceiling. Must be strictly positive.

    Notes
    -----
    Concurrency contract: the lock serializes all callers, so a slow
    callee blocks every other waiter on the same limiter. This is
    intentional — for batch ingest we want strict pacing, not
    overlapping inflight calls. If we ever want N concurrent inflight
    requests at rate R, we should use one limiter at rate R **and** a
    semaphore of size N at the call site, not change this class.
    """

    def __init__(self, rate_per_second: float) -> None:
        if rate_per_second <= 0:
            raise ValueError(f'rate_per_second must be > 0; got {rate_per_second!r}')
        self._interval = 1.0 / rate_per_second
        self._next_available: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def rate_per_second(self) -> float:
        """Configured ceiling in requests per second."""
        return 1.0 / self._interval

    async def acquire(self) -> None:
        """Block until a token is available, then consume it.

        First call returns immediately. Subsequent calls sleep just long
        enough that the average dispatch rate matches the configured
        ceiling.
        """
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            wait = self._next_available - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = loop.time()
            self._next_available = now + self._interval


def _default_rate(publisher: Publisher, settings_value: float) -> float:
    """Return ``settings_value`` if positive, else fall back to spec defaults.

    Used by the per-publisher retriever constructors so that an operator
    override of ``LITSPECTRAITS_RATE_LIMIT_*`` to ``0`` (which is invalid)
    falls back to the spec value rather than raising at construction time
    — the loud failure happens once, deferred to the first acquire.
    """
    if settings_value > 0:
        return settings_value
    return _SPEC_DEFAULTS[publisher]


_SPEC_DEFAULTS: Final[dict[Publisher, float]] = {
    Publisher.WILEY: 3.0,
    Publisher.SPRINGER_NATURE: 5.0,
    Publisher.ELSEVIER: 6.0,
}
