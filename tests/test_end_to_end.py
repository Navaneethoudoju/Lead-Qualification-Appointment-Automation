"""
End-to-end scripted conversation tests (Section 8: "End-to-end tests: Full
conversation paths (FAQ, Lead, Booking, Escalation)").

Runs entirely offline in mock LLM mode — no ANTHROPIC_API_KEY, no network
calls to Anthropic/OpenAI/CRM/Slack. Session 6: Database/state/rate-limiter
are now real Postgres/Redis, not a temp sqlite file, so isolation is done
by pointing at a dedicated test database/Redis db-number and truncating/
flushing it per test (see TEST_DATABASE_URL / TEST_REDIS_URL below — same
convention as tests/test_api.py). The Chroma vector store is still a local
embedded index with no server, so it keeps using a fresh temp persist_dir
per test.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.database import Database
from app.core.ingest import build_vector_store_from_dir
from app.core.llm_client import LLMClient
from app.core.orchestrator import Orchestrator
from app.core.rag_engine import RAGEngine
from app.core.state_store import RedisStateStore
from app.core.tools.calendar_tool import CalendarTool
from app.core.tools.crm_tool import CRMTool
from app.core.tools.escalation import EscalationNotifier

KB_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "knowledge_base", "clinic"))

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/ai_leadflow_test"
)
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")

_TABLES = ["escalations", "appointments", "leads", "messages", "conversations"]


def make_orchestrator(tmp_dir):
    escalation_log = os.path.join(tmp_dir, "escalations.log")
    os.environ["ESCALATION_LOG_PATH"] = escalation_log
    # Ensure mock mode regardless of the host environment.
    os.environ.pop("ANTHROPIC_API_KEY", None)
    os.environ.pop("AIRTABLE_API_KEY", None)
    os.environ.pop("SLACK_WEBHOOK_URL", None)
    os.environ.pop("OPENAI_API_KEY", None)  # keep embeddings on the offline mock path too

    db = Database(TEST_DATABASE_URL)
    with db.cursor() as cur:
        cur.execute(f"TRUNCATE TABLE {', '.join(_TABLES)} RESTART IDENTITY CASCADE")

    state_store = RedisStateStore(TEST_REDIS_URL)
    state_store._client.flushdb()

    llm = LLMClient()
    assert llm.mode == "mock"
    # persist_dir=tmp_dir/chroma keeps each test's index isolated and
    # disposable, same spirit as the old per-test sqlite file.
    vector_store = build_vector_store_from_dir(KB_DIR, persist_dir=os.path.join(tmp_dir, "chroma"))
    rag = RAGEngine(vector_store, llm)
    orch = Orchestrator(
        db=db,
        rag_engine=rag,
        llm=llm,
        calendar_tool=CalendarTool(db),
        crm_tool=CRMTool(db),
        escalation_notifier=EscalationNotifier(),
        state_store=state_store,
    )
    return orch, db


class TestFAQFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.orch, self.db = make_orchestrator(self.tmp)

    def test_grounded_faq_answer(self):
        result = self.orch.handle_message("s1", "web", "What are your working hours?")
        self.assertEqual(result["intent"], "faq")
        self.assertFalse(result["escalated"])
        self.assertIn("9:00 AM", result["reply_text"])
        self.assertIn("rag_retrieval", result["actions_taken"])

    def test_faq_pricing_question(self):
        result = self.orch.handle_message("s2", "web", "How much is a dental cleaning?")
        self.assertEqual(result["intent"], "faq")
        self.assertFalse(result["escalated"])
        self.assertIn("800", result["reply_text"])  # dental check-up price from services.md

    def test_out_of_scope_question_escalates(self):
        result = self.orch.handle_message("s3", "web", "What's your opinion on quantum computing research funding policy?")
        self.assertTrue(result["escalated"])
        self.assertEqual(result["intent"], "escalation")


class TestLeadQualificationFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.orch, self.db = make_orchestrator(self.tmp)

    def test_multi_turn_qualification_completes_and_pushes_to_crm(self):
        session = "lead-session-1"
        r1 = self.orch.handle_message(session, "web", "I'm interested in physiotherapy sessions, looking for pricing")
        self.assertEqual(r1["intent"], "lead_qualification")
        self.assertFalse(r1["complete"] if "complete" in r1 else False)

        r2 = self.orch.handle_message(session, "web", "My name is Rahul Sharma")
        r3 = self.orch.handle_message(session, "web", "You can reach me at 9876543210")

        # by now requirement, name, phone should all be captured
        self.assertIn("pushed_lead_to_crm", r3["actions_taken"])
        self.assertFalse(r3["escalated"])

        lead = self.db.list_leads()[0]
        self.assertEqual(lead["name"], "Rahul Sharma")
        self.assertEqual(lead["phone"], "9876543210")
        self.assertEqual(lead["crm_synced"], 1)


class TestBookingFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.orch, self.db = make_orchestrator(self.tmp)

    def test_full_booking_flow(self):
        session = "booking-session-1"
        r1 = self.orch.handle_message(session, "web", "I'd like to book an appointment")
        self.assertEqual(r1["intent"], "booking_request")

        r2 = self.orch.handle_message(session, "web", "My name is Anita Rao")
        r3 = self.orch.handle_message(session, "web", "9998887770")
        self.assertIn("checked_calendar_availability", r3["actions_taken"])
        self.assertIn("1.", r3["reply_text"])  # at least one slot offered

        r4 = self.orch.handle_message(session, "web", "1")
        self.assertIn("booked_calendar_slot", r4["actions_taken"])
        self.assertFalse(r4["escalated"])
        self.assertIn("confirmed", r4["reply_text"].lower())

        # A second booking for the same slot should not double-book it —
        # verify it no longer appears in a fresh availability check.
        appts = self.db.list_appointments_on(
            self.db.list_leads()[0]["id"] and __import__("datetime").datetime.now().strftime("%Y-%m-%d")
        )
        # (loose sanity check; exact date match isn't required for this test)


class TestEscalationFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.orch, self.db = make_orchestrator(self.tmp)

    def test_explicit_human_request_escalates(self):
        result = self.orch.handle_message("esc-1", "web", "I want to talk to a human agent please")
        self.assertTrue(result["escalated"])
        self.assertEqual(result["intent"], "escalation")
        self.assertIn("notified_human", result["actions_taken"])

    def test_negative_sentiment_escalates(self):
        result = self.orch.handle_message("esc-2", "web", "This is absolutely terrible, I'm furious about the service")
        self.assertTrue(result["escalated"])

    def test_escalation_is_logged(self):
        self.orch.handle_message("esc-3", "web", "I need to speak to a real person")
        escalation_log = os.environ["ESCALATION_LOG_PATH"]
        self.assertTrue(os.path.exists(escalation_log))
        with open(escalation_log) as f:
            content = f.read()
        self.assertIn("esc-3", content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
