"""
Tests for the FastAPI web layer (app/main.py) — Section 7 of the
architecture doc.

Import strategy: try the real `fastapi.testclient.TestClient` first (what
you get once `pip install -r requirements.txt` has network access to run).
If `fastapi` isn't importable — as in the sandbox this project was built
in — fall back to a small stdlib-only stub client
(tests/_offline_fastapi_stub.py) that reimplements just enough routing to
actually execute every handler in main.py and check real status
codes/bodies. Either way the test bodies below are unchanged, so this
becomes a strictly stronger check the moment real FastAPI is available.
Run `python3 -m unittest tests.test_api -v` and check the printed banner
to see which mode ran.
"""
from __future__ import annotations

import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    import fastapi  # noqa: F401
    from fastapi.testclient import TestClient
    USING_REAL_FASTAPI = True
except ImportError:
    sys.path.insert(0, os.path.dirname(__file__))
    from _offline_fastapi_stub import install_stub_fastapi, StubTestClient as TestClient
    install_stub_fastapi()
    USING_REAL_FASTAPI = False


# session 6: there's no more sqlite temp-file-per-test trick — Database and
# the state/rate-limit stores now point at real Postgres/Redis. Isolation is
# done instead by pointing at a dedicated *test* database/Redis db-number
# (so a test run can never touch a dev/prod one by accident) and wiping it
# in setUp. Override via TEST_DATABASE_URL / TEST_REDIS_URL if your local
# Postgres/Redis need different credentials or ports than these defaults.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/ai_leadflow_test"
)
# Redis logical db 15 (last of the default 16) is the conventional "scratch/
# test" db — distinct from db 0, which is what REDIS_URL points at by
# default for a real run, so a stray `python -m unittest` can't wipe a
# developer's local dev-mode conversation state.
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")

# Tables in FK-dependency order (children first) so TRUNCATE ... CASCADE
# isn't strictly required, though CASCADE is still used below for safety if
# this list ever drifts from database.py's schema.
_TABLES = ["escalations", "appointments", "leads", "messages", "conversations"]


class ApiTestBase(unittest.TestCase):
    """Each test gets a fresh app instance wired against a dedicated test
    Postgres database and Redis db-number, wiped before every test — main.py
    wires its singletons at import time, so we force a clean re-import per
    test via importlib.reload after pointing DATABASE_URL/REDIS_URL at the
    test instances.

    Requires a reachable Postgres at TEST_DATABASE_URL and Redis at
    TEST_REDIS_URL (create the test database once yourself, e.g.
    `createdb ai_leadflow_test` — Database._init_schema creates the tables).
    """

    def setUp(self):
        import importlib

        os.environ["DATABASE_URL"] = TEST_DATABASE_URL
        os.environ["REDIS_URL"] = TEST_REDIS_URL

        import app.config as config
        importlib.reload(config)

        import app.main as main_module
        importlib.reload(main_module)
        self.main = main_module
        self.client = TestClient(main_module.app)

        self._truncate_postgres()
        self._flush_redis()

    def tearDown(self):
        os.environ.pop("DATABASE_URL", None)
        os.environ.pop("REDIS_URL", None)

    def _truncate_postgres(self):
        with self.main._db.cursor() as cur:
            cur.execute(f"TRUNCATE TABLE {', '.join(_TABLES)} RESTART IDENTITY CASCADE")

    def _flush_redis(self):
        # RedisStateStore/RedisRateLimiter don't expose a flush method (by
        # design — production code has no business wiping a whole Redis db),
        # so reach into the underlying client directly here, test-only.
        self.main._state_store._client.flushdb()
        self.main._message_rate_limiter._client.flushdb()

    def admin_headers(self, username="admin", password="admin"):
        # Admin routes are now Basic-auth gated (see app/main.py); defaults
        # match config.py's fallback when no env vars are set.
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        return {"Authorization": f"Basic {token}"}


class TestHealth(ApiTestBase):
    def test_health_reports_mock_modes_and_kb_size(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["llm_mode"], "mock")
        self.assertEqual(body["calendar_mode"], "mock")
        self.assertEqual(body["crm_mode"], "mock")
        self.assertGreater(body["kb_chunks_indexed"], 0)
        # New this session: flags default admin/admin creds vs configured
        # ones, without needing to check env vars by hand.
        self.assertEqual(body["admin_auth_mode"], "default_credentials")


class TestMessageEndpoint(ApiTestBase):
    def test_post_message_faq_roundtrip(self):
        resp = self.client.post(
            "/api/message",
            json={"session_id": "api-test-1", "channel": "web", "text": "What are your working hours?"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("reply_text", body)
        self.assertIn("intent", body)
        self.assertIn("actions_taken", body)
        self.assertIn("escalated", body)
        self.assertIsInstance(body["actions_taken"], list)

    def test_post_message_empty_text_is_400(self):
        resp = self.client.post(
            "/api/message",
            json={"session_id": "api-test-2", "channel": "web", "text": "   "},
        )
        self.assertEqual(resp.status_code, 400)

    def test_post_message_defaults_channel_to_web(self):
        # channel has a default in MessageRequest — omitting it shouldn't 500
        resp = self.client.post(
            "/api/message",
            json={"session_id": "api-test-3", "text": "Hello"},
        )
        self.assertEqual(resp.status_code, 200)

    def test_get_conversation_after_message(self):
        self.client.post(
            "/api/message",
            json={"session_id": "api-test-4", "channel": "web", "text": "What are your working hours?"},
        )
        resp = self.client.get("/api/conversations/api-test-4")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["conversation"]["id"], "api-test-4")
        self.assertGreaterEqual(len(body["messages"]), 2)  # user turn + bot reply

    def test_get_conversation_missing_is_404(self):
        resp = self.client.get("/api/conversations/does-not-exist")
        self.assertEqual(resp.status_code, 404)


class TestAdminEndpoints(ApiTestBase):
    def test_list_leads_empty_then_populated(self):
        resp = self.client.get("/api/admin/leads", headers=self.admin_headers())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["items"], [])
        self.assertEqual(body["total"], 0)

        create = self.client.post(
            "/api/admin/leads",
            json={
                "conversation_id": "conv-1",
                "name": "Anita Rao",
                "phone": "9876543210",
                "requirement": "Dental filling",
            },
            headers=self.admin_headers(),
        )
        self.assertEqual(create.status_code, 200)
        lead_id = create.json()["lead_id"]
        self.assertTrue(lead_id)

        listed = self.client.get("/api/admin/leads", headers=self.admin_headers())
        body = listed.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["name"], "Anita Rao")

    def test_list_leads_pagination(self):
        for i in range(5):
            self.client.post(
                "/api/admin/leads",
                json={"conversation_id": f"conv-page-{i}", "name": f"Lead {i}"},
                headers=self.admin_headers(),
            )
        page = self.client.get(
            "/api/admin/leads?limit=2&offset=0", headers=self.admin_headers()
        )
        body = page.json()
        self.assertEqual(body["total"], 5)
        self.assertEqual(len(body["items"]), 2)
        self.assertEqual(body["limit"], 2)
        self.assertEqual(body["offset"], 0)

        page2 = self.client.get(
            "/api/admin/leads?limit=2&offset=2", headers=self.admin_headers()
        )
        self.assertEqual(len(page2.json()["items"]), 2)
        # Different offset -> different slice (newest-first ordering, so
        # page 1 and page 2 shouldn't overlap).
        ids_page1 = {l["id"] for l in body["items"]}
        ids_page2 = {l["id"] for l in page2.json()["items"]}
        self.assertEqual(ids_page1 & ids_page2, set())

    def test_get_lead_by_id(self):
        create = self.client.post(
            "/api/admin/leads",
            json={"conversation_id": "conv-2", "name": "Ravi Kumar"},
            headers=self.admin_headers(),
        )
        lead_id = create.json()["lead_id"]

        resp = self.client.get(f"/api/admin/leads/{lead_id}", headers=self.admin_headers())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "Ravi Kumar")

    def test_get_lead_missing_is_404(self):
        resp = self.client.get("/api/admin/leads/no-such-id", headers=self.admin_headers())
        self.assertEqual(resp.status_code, 404)

    def test_update_lead_patches_only_given_fields(self):
        # session 6: in-place lead editing from the dashboard.
        create = self.client.post(
            "/api/admin/leads",
            json={"conversation_id": "conv-patch-1", "name": "Asha Rao", "phone": "9111111111"},
            headers=self.admin_headers(),
        )
        lead_id = create.json()["lead_id"]

        resp = self.client.patch(
            f"/api/admin/leads/{lead_id}",
            json={"phone": "9222222222"},
            headers=self.admin_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["phone"], "9222222222")
        # name wasn't in the PATCH body, so it must be untouched.
        self.assertEqual(body["name"], "Asha Rao")

    def test_update_lead_missing_is_404(self):
        resp = self.client.patch(
            "/api/admin/leads/no-such-id",
            json={"phone": "123"},
            headers=self.admin_headers(),
        )
        self.assertEqual(resp.status_code, 404)

    def test_update_lead_requires_auth(self):
        resp = self.client.patch("/api/admin/leads/whatever", json={"phone": "123"})
        self.assertEqual(resp.status_code, 401)

    def test_stats_reflects_global_counts_not_page_scoped(self):
        # session 6: GET /api/admin/stats replaces the session-5 dashboard
        # approximation (escalated/booked counted from one page only).
        for i in range(3):
            self.client.post(
                "/api/admin/leads",
                json={"conversation_id": f"conv-stats-{i}", "name": f"Lead {i}"},
                headers=self.admin_headers(),
            )
        resp = self.client.get("/api/admin/stats", headers=self.admin_headers())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["total_leads"], 3)
        self.assertEqual(body["total_conversations"], 3)
        self.assertEqual(body["escalated_conversations"], 0)
        self.assertEqual(body["booked_conversations"], 0)
        self.assertEqual(body["total_appointments"], 0)

    def test_stats_requires_auth(self):
        resp = self.client.get("/api/admin/stats")
        self.assertEqual(resp.status_code, 401)

    def test_list_conversations_reflects_messages(self):
        self.client.post(
            "/api/message",
            json={"session_id": "admin-conv-1", "channel": "web", "text": "Hi"},
        )
        resp = self.client.get("/api/admin/conversations", headers=self.admin_headers())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        ids = [c["id"] for c in body["items"]]
        self.assertIn("admin-conv-1", ids)
        self.assertGreaterEqual(body["total"], 1)

    def test_list_appointments_empty_then_populated_with_lead_details(self):
        # This endpoint: appointments existed in the DB and calendar tool
        # but had no admin read path until a previous session.
        empty = self.client.get("/api/admin/appointments", headers=self.admin_headers())
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.json()["items"], [])
        self.assertEqual(empty.json()["total"], 0)

        lead = self.client.post(
            "/api/admin/leads",
            json={"conversation_id": "conv-appt-1", "name": "Priya Nair", "phone": "9000090000"},
            headers=self.admin_headers(),
        )
        lead_id = lead.json()["lead_id"]

        avail = self.client.post("/api/tools/calendar/check-availability", json={"days_ahead": 7})
        slot_dt = avail.json()["available_slots"][0]
        booked = self.client.post(
            "/api/tools/calendar/book",
            json={"lead_id": lead_id, "slot_datetime": slot_dt},
        )
        self.assertEqual(booked.status_code, 200)

        resp = self.client.get("/api/admin/appointments", headers=self.admin_headers())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        rows = body["items"]
        self.assertEqual(body["total"], 1)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # Joined lead fields should come back inline, no second call needed.
        self.assertEqual(row["lead_name"], "Priya Nair")
        self.assertEqual(row["lead_phone"], "9000090000")
        self.assertEqual(row["conversation_id"], "conv-appt-1")
        self.assertEqual(row["slot_datetime"], slot_dt)
        self.assertEqual(row["status"], "confirmed")
        self.assertIn("calendar_event_id", row)


class TestAdminAuth(ApiTestBase):
    """New this session: every /api/admin/* route is now Basic-auth gated
    (app/main.py:verify_admin). These cover the auth boundary itself, kept
    separate from TestAdminEndpoints so a failure here reads unambiguously
    as an auth regression rather than a data-shape one."""

    ADMIN_ROUTES = [
        ("GET", "/api/admin/leads"),
        ("GET", "/api/admin/conversations"),
        ("GET", "/api/admin/appointments"),
        ("GET", "/api/admin/stats"),
    ]

    def test_admin_routes_require_auth(self):
        for method, path in self.ADMIN_ROUTES:
            with self.subTest(path=path):
                resp = self.client.get(path) if method == "GET" else self.client.post(path, json={})
                self.assertEqual(resp.status_code, 401, f"{path} should require auth")

    def test_admin_routes_reject_wrong_credentials(self):
        for method, path in self.ADMIN_ROUTES:
            with self.subTest(path=path):
                resp = self.client.get(path, headers=self.admin_headers("admin", "wrong-password"))
                self.assertEqual(resp.status_code, 401)

    def test_create_lead_requires_auth(self):
        resp = self.client.post(
            "/api/admin/leads",
            json={"conversation_id": "conv-noauth", "name": "Should Fail"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_admin_routes_accept_correct_credentials(self):
        for method, path in self.ADMIN_ROUTES:
            with self.subTest(path=path):
                resp = self.client.get(path, headers=self.admin_headers())
                self.assertEqual(resp.status_code, 200)

    def test_non_admin_routes_do_not_require_auth(self):
        # /api/message, /api/conversations/{id}, and /api/tools/* are
        # channel-facing / orchestrator-internal — gating these would break
        # the chat widget and the LLM's own function-calling loop.
        resp = self.client.post(
            "/api/message",
            json={"session_id": "noauth-check", "channel": "web", "text": "Hi"},
        )
        self.assertEqual(resp.status_code, 200)


class TestToolEndpoints(ApiTestBase):
    def test_check_availability_returns_slots(self):
        resp = self.client.post("/api/tools/calendar/check-availability", json={"days_ahead": 3})
        self.assertEqual(resp.status_code, 200)
        slots = resp.json()["available_slots"]
        self.assertIsInstance(slots, list)
        self.assertGreater(len(slots), 0)

    def test_book_then_double_book_conflicts(self):
        lead = self.client.post(
            "/api/admin/leads",
            json={"conversation_id": "conv-3", "name": "Test Lead"},
            headers=self.admin_headers(),
        )
        lead_id = lead.json()["lead_id"]

        avail = self.client.post("/api/tools/calendar/check-availability", json={"days_ahead": 7})
        slot_dt = avail.json()["available_slots"][0]  # ISO datetime string

        first = self.client.post(
            "/api/tools/calendar/book",
            json={"lead_id": lead_id, "slot_datetime": slot_dt},
        )
        self.assertEqual(first.status_code, 200)

        second = self.client.post(
            "/api/tools/calendar/book",
            json={"lead_id": lead_id, "slot_datetime": slot_dt},
        )
        self.assertEqual(second.status_code, 409)

    def test_push_lead_to_crm(self):
        resp = self.client.post(
            "/api/tools/crm/push-lead",
            json={"conversation_id": "conv-4", "name": "CRM Test", "phone": "111"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("lead_id", body)


if __name__ == "__main__":
    mode = "REAL fastapi.testclient" if USING_REAL_FASTAPI else "offline stub client (see _offline_fastapi_stub.py)"
    print(f"\n[tests/test_api.py] Running against: {mode}\n")
    unittest.main()
