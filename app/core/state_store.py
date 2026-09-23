"""
Conversation state store — Redis.

Session 6 upgrade: this used to be a plain in-memory dict (see
state_store.py.orig.bak). That meant conversation state vanished on every
restart and couldn't be shared across backend replicas — each instance
behind a load balancer had its own private copy, so a user's follow-up
message could land on a different process with no memory of the
in-progress booking/qualification flow.

Redis fixes both: state is shared across every backend instance and
survives individual process restarts (as long as Redis itself stays up).
Each session's state dict is stored as a single JSON blob under
`leadflow:state:{session_id}`, with a TTL so abandoned conversations don't
accumulate forever.

Interface (get/set/update/delete by session_id) is unchanged from the
in-memory version, so orchestrator.py needed no changes.
"""
from __future__ import annotations

import json
import os
from typing import Any

import redis

DEFAULT_REDIS_URL = "redis://localhost:6379/0"
DEFAULT_TTL_SECONDS = 60 * 60 * 24  # 24h: long enough to resume a stalled chat, short enough not to leak memory forever
KEY_PREFIX = "leadflow:state:"


class RedisStateStore:
    def __init__(self, redis_url: str | None = None, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.redis_url = redis_url or os.environ.get("REDIS_URL", DEFAULT_REDIS_URL)
        self.ttl_seconds = ttl_seconds
        self._client = redis.Redis.from_url(self.redis_url, decode_responses=True)

    def _key(self, session_id: str) -> str:
        return f"{KEY_PREFIX}{session_id}"

    def get(self, session_id: str) -> dict[str, Any]:
        raw = self._client.get(self._key(session_id))
        if not raw:
            return {}
        return json.loads(raw)

    def set(self, session_id: str, state: dict[str, Any]) -> None:
        self._client.set(self._key(session_id), json.dumps(state), ex=self.ttl_seconds)

    def update(self, session_id: str, **kwargs) -> dict[str, Any]:
        # Not a single atomic Redis op (read-modify-write), matching the
        # original in-memory store's semantics (which used a plain Lock).
        # A single conversation is always handled by one request at a time
        # in practice (the widget waits for a reply before sending the next
        # message), so this is not a real contention point.
        current = self.get(session_id)
        current.update(kwargs)
        self.set(session_id, current)
        return current

    def delete(self, session_id: str) -> None:
        self._client.delete(self._key(session_id))

    def ping(self) -> bool:
        return bool(self._client.ping())
