from __future__ import annotations

import importlib
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestFallbackRuntime(unittest.TestCase):
    def test_sqlite_and_in_memory_fallbacks(self):
        os.environ["DATABASE_URL"] = "sqlite:///:memory:"
        os.environ["REDIS_URL"] = "redis://localhost:6379/0"

        import app.config as config
        importlib.reload(config)

        import app.core.database as database_module
        import app.core.state_store as state_store_module
        import app.core.rate_limiter as rate_limiter_module

        importlib.reload(database_module)
        importlib.reload(state_store_module)
        importlib.reload(rate_limiter_module)

        db = database_module.Database(config.DATABASE_URL)
        conversation = db.get_or_create_conversation("fallback-session", "web")
        self.assertEqual(conversation["id"], "fallback-session")

        db.add_message("fallback-session", "user", "Hello")
        self.assertEqual(len(db.get_messages("fallback-session")), 1)

        state = state_store_module.RedisStateStore(config.REDIS_URL)
        state.set("fallback-session", {"stage": "demo"})
        self.assertEqual(state.get("fallback-session"), {"stage": "demo"})

        limiter = rate_limiter_module.RedisRateLimiter(max_requests=2, window_seconds=60, redis_url=config.REDIS_URL)
        allowed_1, retry_1 = limiter.allow("demo-client")
        allowed_2, retry_2 = limiter.allow("demo-client")
        allowed_3, retry_3 = limiter.allow("demo-client")
        self.assertTrue(allowed_1)
        self.assertTrue(allowed_2)
        self.assertFalse(allowed_3)
        self.assertGreaterEqual(retry_3, 1)

        db.close()
