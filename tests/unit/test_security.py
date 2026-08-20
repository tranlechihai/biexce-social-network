"""Unit tests for process-local abuse controls."""

from ting_ting.security import RateLimiter


def test_rate_limiter_rejects_requests_over_limit():
    limiter = RateLimiter()

    assert limiter.allow("login:client", limit=2, window_seconds=60, now=10)
    assert limiter.allow("login:client", limit=2, window_seconds=60, now=11)
    assert not limiter.allow("login:client", limit=2, window_seconds=60, now=12)


def test_rate_limiter_recovers_after_window():
    limiter = RateLimiter()

    assert limiter.allow("register:client", limit=1, window_seconds=60, now=10)
    assert not limiter.allow("register:client", limit=1, window_seconds=60, now=69)
    assert limiter.allow("register:client", limit=1, window_seconds=60, now=70)
