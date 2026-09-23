"""
Direct unit tests for app/core/rate_limiter.py.

Session 6: RateLimiter (in-memory deque, stdlib-only) was replaced by
RedisRateLimiter (sorted-set sliding window backed by Redis — see the
module docstring in rate_limiter.py) with the same allow()/reset()
interface, so these tests only needed the class name and connection
target updated, not their bodies. Unlike the old in-memory version this
now requires a reachable Redis at TEST_REDIS_URL; reset() is called in
setUp/via each test's own client key so tests don't leak buckets between
each other in the shared Redis db.
"""
import os
import time
import unittest

from app.core.rate_limiter import RedisRateLimiter

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")


class TestRateLimiter(unittest.TestCase):
    def setUp(self):
        # Fresh bucket namespace per test: flush this limiter's keys so
        # leftovers from a previous run/test can't affect max_requests
        # counting (the old in-memory version got this for free since each
        # RateLimiter() instance had its own private dict).
        RedisRateLimiter(max_requests=1, window_seconds=1, redis_url=TEST_REDIS_URL).reset()

    def test_allows_up_to_max_requests_in_window(self):
        limiter = RedisRateLimiter(max_requests=3, window_seconds=60, redis_url=TEST_REDIS_URL)
        for _ in range(3):
            allowed, retry_after = limiter.allow("client-a")
            self.assertTrue(allowed)
            self.assertEqual(retry_after, 0)

    def test_blocks_after_max_requests(self):
        limiter = RedisRateLimiter(max_requests=2, window_seconds=60, redis_url=TEST_REDIS_URL)
        limiter.allow("client-b")
        limiter.allow("client-b")
        allowed, retry_after = limiter.allow("client-b")
        self.assertFalse(allowed)
        self.assertGreater(retry_after, 0)

    def test_keys_are_independent(self):
        limiter = RedisRateLimiter(max_requests=1, window_seconds=60, redis_url=TEST_REDIS_URL)
        allowed_a, _ = limiter.allow("client-a")
        allowed_b, _ = limiter.allow("client-b")
        self.assertTrue(allowed_a)
        self.assertTrue(allowed_b)  # different key, own bucket

    def test_window_expiry_allows_requests_again(self):
        limiter = RedisRateLimiter(max_requests=1, window_seconds=0.05, redis_url=TEST_REDIS_URL)
        allowed_first, _ = limiter.allow("client-c")
        self.assertTrue(allowed_first)
        blocked, _ = limiter.allow("client-c")
        self.assertFalse(blocked)
        time.sleep(0.06)
        allowed_again, _ = limiter.allow("client-c")
        self.assertTrue(allowed_again)

    def test_reset_clears_all_buckets(self):
        limiter = RedisRateLimiter(max_requests=1, window_seconds=60, redis_url=TEST_REDIS_URL)
        limiter.allow("client-d")
        blocked, _ = limiter.allow("client-d")
        self.assertFalse(blocked)
        limiter.reset()
        allowed, _ = limiter.allow("client-d")
        self.assertTrue(allowed)


if __name__ == "__main__":
    unittest.main()
