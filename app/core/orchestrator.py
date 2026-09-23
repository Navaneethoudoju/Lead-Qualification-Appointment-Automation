"""
Orchestrator — the central conversation flow (Section 5 of the architecture
doc). This is the piece every channel adapter (web widget today, WhatsApp /
voice later) calls through the same `handle_message()` entrypoint, using the
channel-agnostic message schema (Section 4.3).

Escalation triggers implemented here (Section 5.1):
  - Classifier confidence below threshold (< 0.6)
  - RAG retrieval returns no relevant chunks (grounded == False)
  - User explicitly asks for a human (classifier maps this to 'escalation')
  - Booking conflict that can't be auto-resolved (SlotUnavailableError)
  - Negative sentiment / complaint detected
"""
from __future__ import annotations

import re
from datetime import datetime

from .database import Database
from .llm_client import LLMClient
from .qualification import QualificationFlow, REQUIRED_SLOTS
from .rag_engine import RAGEngine
from .state_store import RedisStateStore
from .tools.calendar_tool import CalendarConfigError, CalendarTool, SlotUnavailableError
from .tools.crm_tool import CRMTool
from .tools.escalation import EscalationNotifier

CONFIDENCE_ESCALATION_THRESHOLD = 0.6
BOOKING_MIN_SLOTS = ["name", "phone"]
MAX_OFFERED_SLOTS = 5


class Orchestrator:
    def __init__(
        self,
        db: Database,
        rag_engine: RAGEngine,
        llm: LLMClient,
        calendar_tool: CalendarTool | None = None,
        crm_tool: CRMTool | None = None,
        escalation_notifier: EscalationNotifier | None = None,
        state_store: RedisStateStore | None = None,
    ):
        self.db = db
        self.rag = rag_engine
        self.llm = llm
        self.calendar = calendar_tool or CalendarTool(db)
        self.crm = crm_tool or CRMTool(db)
        self.escalation = escalation_notifier or EscalationNotifier()
        self.state = state_store or RedisStateStore()
        self.qualification = QualificationFlow(llm)

    def handle_message(self, session_id: str, channel: str, text: str) -> dict:
        conversation = self.db.get_or_create_conversation(session_id, channel)
        conversation_id = conversation["id"]
        self.db.add_message(conversation_id, "user", text)

        session_state = self.state.get(session_id)
        stage = session_state.get("stage", "idle")
        slots = session_state.get("slots", {})

        actions_taken: list[str] = []

        # A booking flow already in progress takes priority over re-classifying
        # intent, so "3" (picking a slot) isn't misread as a fresh FAQ, and a
        # reply like "My name is Anita" while we're still collecting the
        # minimal name/phone for booking continues the booking flow instead
        # of being reclassified from scratch.
        if stage in ("booking_awaiting_choice", "booking_collecting_info"):
            result = self._continue_booking_choice(session_id, conversation_id, text, session_state)
            return self._finalize(conversation_id, result, actions_taken + result.get("actions_taken", []))

        if stage == "qualifying":
            result = self._continue_qualification(session_id, conversation_id, text, slots)
            return self._finalize(conversation_id, result, actions_taken + result.get("actions_taken", []))

        # Fresh turn: classify intent.
        history = [dict(r) for r in self.db.get_messages(conversation_id)]
        classification = self.llm.classify_intent(text, history)
        intent = classification.get("intent", "faq")
        confidence = float(classification.get("confidence", 0.0))

        if self.llm.detect_negative_sentiment(text):
            result = self._escalate(conversation_id, "Negative sentiment detected.")
            return self._finalize(conversation_id, result, actions_taken + result.get("actions_taken", []))

        if intent == "escalation":
            result = self._escalate(conversation_id, "User explicitly requested a human.")
            return self._finalize(conversation_id, result, actions_taken + result.get("actions_taken", []))

        if confidence < CONFIDENCE_ESCALATION_THRESHOLD:
            result = self._escalate(
                conversation_id,
                f"Low classifier confidence ({confidence:.2f}) for intent '{intent}'.",
            )
            return self._finalize(conversation_id, result, actions_taken + result.get("actions_taken", []))

        if intent == "faq":
            result = self._handle_faq(conversation_id, text)
        elif intent == "lead_qualification":
            result = self._start_qualification(session_id, conversation_id, text, slots)
        elif intent == "booking_request":
            result = self._start_booking(session_id, conversation_id, text, slots)
        else:
            result = self._escalate(conversation_id, f"Unhandled intent '{intent}'.")

        result["intent"] = result.get("intent", intent)
        return self._finalize(conversation_id, result, actions_taken + result.get("actions_taken", []))

    # ---- FAQ branch -------------------------------------------------------

    def _handle_faq(self, conversation_id: str, text: str) -> dict:
        answer = self.rag.answer(text)
        if not answer["grounded"]:
            return self._escalate(conversation_id, "RAG retrieval found no relevant knowledge-base content.")
        return {
            "reply_text": answer["answer"],
            "intent": "faq",
            "actions_taken": ["rag_retrieval"],
            "escalated": False,
        }

    # ---- Lead qualification branch ----------------------------------------

    def _start_qualification(self, session_id: str, conversation_id: str, text: str, slots: dict) -> dict:
        return self._continue_qualification(session_id, conversation_id, text, slots)

    def _continue_qualification(self, session_id: str, conversation_id: str, text: str, slots: dict) -> dict:
        flow_result = self.qualification.process_turn(text, slots)
        updated_slots = flow_result["slots"]

        if not flow_result["complete"]:
            self.state.set(session_id, {"stage": "qualifying", "slots": updated_slots})
            missing_so_far = ", ".join(REQUIRED_SLOTS[: REQUIRED_SLOTS.index(flow_result["missing"][0])]) or "nothing yet"
            reply = flow_result["next_prompt"]
            if any(k in updated_slots for k in REQUIRED_SLOTS):
                reply = f"Thanks! {reply}"
            return {
                "reply_text": reply,
                "intent": "lead_qualification",
                "actions_taken": ["extracted_lead_slots"],
                "escalated": False,
            }

        # Complete — push to CRM.
        crm_result = self.crm.push_lead(conversation_id, updated_slots)
        self.state.set(session_id, {"stage": "idle", "slots": {}})
        self.db.update_conversation_status(conversation_id, "qualified")

        if not crm_result.get("crm_synced"):
            return self._escalate(
                conversation_id,
                f"CRM push failed: {crm_result.get('error', 'unknown error')}",
            )

        return {
            "reply_text": (
                f"Thanks {updated_slots.get('name', '')}! I've got your details for "
                f"\"{updated_slots.get('requirement', 'your request')}\" and our team will "
                f"follow up on {updated_slots.get('phone', 'the number you gave')} shortly. "
                "Would you also like to book an appointment now?"
            ),
            "intent": "lead_qualification",
            "actions_taken": ["extracted_lead_slots", "pushed_lead_to_crm"],
            "escalated": False,
        }

    # ---- Booking branch -----------------------------------------------

    def _start_booking(self, session_id: str, conversation_id: str, text: str, slots: dict) -> dict:
        # Minimal qualification (name + phone) before we book, so every
        # appointment is tied to an identifiable lead.
        needed = [s for s in BOOKING_MIN_SLOTS if s not in slots]
        if needed:
            extracted = self.llm.extract_slots(text, slots, needed)
            slots = {**slots, **{k: v for k, v in extracted.items() if v}}
            still_missing = [s for s in BOOKING_MIN_SLOTS if not slots.get(s)]
            if still_missing:
                self.state.set(session_id, {"stage": "booking_collecting_info", "slots": slots})
                prompt = {"name": "Could I get your name?", "phone": "And a phone number to confirm the booking?"}[
                    still_missing[0]
                ]
                return {
                    "reply_text": f"Happy to help you book an appointment. {prompt}",
                    "intent": "booking_request",
                    "actions_taken": [],
                    "escalated": False,
                }

        return self._offer_slots(session_id, conversation_id, slots)

    def _offer_slots(self, session_id: str, conversation_id: str, slots: dict) -> dict:
        try:
            available = self.calendar.check_availability(days_ahead=7)[:MAX_OFFERED_SLOTS]
        except CalendarConfigError as e:
            # Google Calendar mode is misconfigured (bad/missing credentials,
            # API unreachable) — a deployment problem, not something a retry
            # from the user fixes, so this escalates to a human rather than
            # looping the user on "no slots found".
            return self._escalate(conversation_id, f"Calendar integration error: {e}")
        if not available:
            return self._escalate(conversation_id, "No calendar availability found in the next 7 days.")

        self.state.set(
            session_id,
            {"stage": "booking_awaiting_choice", "slots": slots, "offered_slots": available},
        )
        lines = [self._format_slot(s, i + 1) for i, s in enumerate(available)]
        reply = "Here are the next available appointment slots:\n" + "\n".join(lines) + "\n\nReply with the number of the slot you'd like."
        return {
            "reply_text": reply,
            "intent": "booking_request",
            "actions_taken": ["checked_calendar_availability"],
            "escalated": False,
        }

    def _continue_booking_choice(self, session_id: str, conversation_id: str, text: str, session_state: dict) -> dict:
        stage = session_state.get("stage")
        slots = session_state.get("slots", {})

        if stage == "booking_collecting_info":
            return self._start_booking(session_id, conversation_id, text, slots)

        offered = session_state.get("offered_slots", [])
        choice_idx = self._parse_choice(text, len(offered))

        if choice_idx is None:
            lines = [self._format_slot(s, i + 1) for i, s in enumerate(offered)]
            reply = "Sorry, I didn't catch that. Please reply with the number of one of these slots:\n" + "\n".join(lines)
            return {
                "reply_text": reply,
                "intent": "booking_request",
                "actions_taken": [],
                "escalated": False,
            }

        chosen_slot = offered[choice_idx]
        lead_result = self.crm.push_lead(conversation_id, slots)
        lead_id = lead_result["lead_id"]

        try:
            booking = self.calendar.book(lead_id, chosen_slot)
        except SlotUnavailableError as e:
            # Booking conflict that can't be auto-resolved -> escalate, per Section 5.1.
            self.state.set(session_id, {"stage": "idle", "slots": {}})
            return self._escalate(conversation_id, f"Booking conflict: {e}")
        except CalendarConfigError as e:
            self.state.set(session_id, {"stage": "idle", "slots": {}})
            return self._escalate(conversation_id, f"Calendar integration error: {e}")

        self.state.set(session_id, {"stage": "idle", "slots": {}})
        self.db.update_conversation_status(conversation_id, "booked")

        return {
            "reply_text": (
                f"You're all set! Your appointment is confirmed for {self._format_slot(booking['slot_datetime'])}. "
                "We'll send a reminder before your visit."
            ),
            "intent": "booking_request",
            "actions_taken": ["pushed_lead_to_crm", "booked_calendar_slot"],
            "escalated": False,
        }

    @staticmethod
    def _parse_choice(text: str, num_options: int) -> int | None:
        m = re.search(r"\d+", text.strip())
        if not m:
            return None
        idx = int(m.group(0)) - 1
        if 0 <= idx < num_options:
            return idx
        return None

    @staticmethod
    def _format_slot(iso_dt: str, index: int | None = None) -> str:
        dt = datetime.strptime(iso_dt, "%Y-%m-%dT%H:%M:%S")
        formatted = dt.strftime("%a, %d %b %Y at %I:%M %p")
        return f"{index}. {formatted}" if index is not None else formatted

    # ---- Escalation -------------------------------------------------------

    def _escalate(self, conversation_id: str, reason: str) -> dict:
        messages = self.db.get_messages(conversation_id)
        snippet = " | ".join(f"{m['role']}: {m['content']}" for m in messages[-4:])
        self.escalation.notify(conversation_id, reason, snippet)
        self.db.log_escalation(conversation_id, reason)
        self.db.update_conversation_status(conversation_id, "escalated")
        return {
            "reply_text": "I want to make sure you get the right answer, so I'm connecting you with a team member who'll follow up shortly.",
            "intent": "escalation",
            "actions_taken": ["notified_human"],
            "escalated": True,
        }

    # ---- Shared finalize step -------------------------------------------

    def _finalize(self, conversation_id: str, result: dict, actions_taken: list[str]) -> dict:
        result = dict(result)
        result["actions_taken"] = list(dict.fromkeys(actions_taken))  # de-dupe, preserve order
        self.db.add_message(conversation_id, "assistant", result["reply_text"])
        return result
