"""Tests for :mod:`litspectraits.retrievers._ratelimit`."""

import asyncio

import pytest

from litspectraits.manifest import Publisher
from litspectraits.retrievers._ratelimit import RateLimiter, _default_rate


def test_invalid_rate_raises() -> None:
    with pytest.raises(ValueError):
        RateLimiter(0)
    with pytest.raises(ValueError):
        RateLimiter(-1.0)


def test_rate_per_second_round_trips() -> None:
    limiter = RateLimiter(7.5)
    # Floating-point round-trip via 1/interval is exact for inverses we set;
    # if it ever drifts, this guard fires before subtler bugs.
    assert limiter.rate_per_second == pytest.approx(7.5)


async def test_first_acquire_is_immediate() -> None:
    limiter = RateLimiter(rate_per_second=10.0)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await limiter.acquire()
    elapsed = loop.time() - started
    # No prior calls → no throttle. Generous ceiling for CI scheduler jitter.
    assert elapsed < 0.05


async def test_throttles_to_configured_rate() -> None:
    # rate=50/s ⇒ interval=20ms. 4 calls ⇒ 3 enforced gaps ⇒ ≥ 60ms.
    rate = 50.0
    n_calls = 4
    expected_min = (n_calls - 1) * (1.0 / rate)
    limiter = RateLimiter(rate_per_second=rate)
    loop = asyncio.get_running_loop()
    started = loop.time()
    for _ in range(n_calls):
        await limiter.acquire()
    elapsed = loop.time() - started
    # Lower bound is the spec; upper bound catches a wildly broken sleep
    # (e.g. interval used as seconds * 1000) without flaking on slow CI.
    assert elapsed >= expected_min * 0.95
    assert elapsed < expected_min + 0.5


async def test_serializes_concurrent_acquires() -> None:
    # Two coroutines racing on the same limiter must emerge in arrival
    # order and one interval apart, even though they're scheduled together.
    limiter = RateLimiter(rate_per_second=20.0)  # interval = 50ms
    loop = asyncio.get_running_loop()
    completion_times: list[float] = []

    async def take() -> None:
        await limiter.acquire()
        completion_times.append(loop.time())

    started = loop.time()
    await asyncio.gather(take(), take(), take())
    assert len(completion_times) == 3
    deltas = [t - started for t in completion_times]
    # Two enforced gaps of 50ms each between three acquires.
    assert deltas[1] - deltas[0] >= 0.045
    assert deltas[2] - deltas[1] >= 0.045


def test_default_rate_uses_settings_when_positive() -> None:
    assert _default_rate(Publisher.WILEY, 7.5) == 7.5
    assert _default_rate(Publisher.ELSEVIER, 0.1) == 0.1


def test_default_rate_falls_back_on_nonpositive() -> None:
    # Operator override of 0 (or negative) is invalid; we fall back to the
    # spec default rather than raise here so the failure surfaces at the
    # first ``acquire`` call instead of at module import.
    assert _default_rate(Publisher.WILEY, 0.0) == 3.0
    assert _default_rate(Publisher.SPRINGER_NATURE, -2.0) == 5.0
    assert _default_rate(Publisher.ELSEVIER, 0.0) == 6.0
