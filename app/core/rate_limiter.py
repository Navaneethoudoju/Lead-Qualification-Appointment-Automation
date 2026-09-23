"""
Rate limiter — Redis-backed sliding window.

Session 6 upgrade: this used to be an in-memory deque per key (see
rate_limiter.py.orig.bak). That worked for a single process, but a script
hammering /api/message would just get a fresh quota on every backend
replica behind a load balancer — the limit was per-process, not per-client.

This version stores each key's hit timestamps in a Redis sorted set
(score = timestamp), so every backend instance shares the same bucket:

  ZADD leadflow:ratelimit:{key} <now> <now>-<random>   -- record this hit
  ZREMRANGEBYSCORE ... -inf (now - window)              -- drop stale hits
  ZCARD ...                                             -- count hits in window

The four ops run in a single MULTI/EXEC pipeline so the check-and-record is
atomic per key (no race between two concurrent requests from the same
client both reading a stale count).
"""
from __future__ import annotations

import os
import time
import uuid

import redis

DEFAULT_REDIS_URL = "redis://localhost:6379/0"
KEY_PREFIX = "leadflow:ratelimit:"


class RedisRateLimiter:
    def __init__(self, max_requests: int, window_seconds: float, redis_url: str | None = None):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.redis_url = redis_url or os.environ.get("REDIS_URL", DEFAULT_REDIS_URL)
        self._client = redis.Redis.from_url(self.redis_url, decode_responses=True)

    def _key(self, key: str) -> str:
        return f"{KEY_PREFIX}{key}"

    def allow(self, key: str) -> tuple[bool, int]:
        """Returns (allowed, retry_after_seconds). retry_after_seconds is 0
        when allowed=True."""
        redis_key = self._key(key)
        now = time.time()
        cutoff = now - self.window_seconds

        pipe = self._client.pipeline()
        pipe.zremrangebyscore(redis_key, "-inf", cutoff)
        pipe.zcard(redis_key)
        _, count = pipe.execute()

        if count >= self.max_requests:
            oldest = self._client.zrange(redis_key, 0, 0, withscores=True)
            oldest_ts = oldest[0][1] if oldest else now
            retry_after = int(self.window_seconds - (now - oldest_ts)) + 1
            return False, max(retry_after, 1)

        member = f"{now}-{uuid.uuid4().hex[:8]}"
        pipe = self._client.pipeline()
        pipe.zadd(redis_key, {member: now})
        pipe.expire(redis_key, int(self.window_seconds) + 1)
        pipe.execute()
        return True, 0

    def reset(self) -> None:
        """Test-only helper — clears every bucket this limiter has touched."""
        for k in self._client.scan_iter(match=f"{KEY_PREFIX}*"):
            self._client.delete(k)
