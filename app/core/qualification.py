"""
Qualification Flow (Section 6, Week 4 / Section 2.2).

Rule-guided + LLM-assisted sequence: we always know exactly which slots are
still missing (rule-guided), and use the LLM (real or mock) to extract slot
values from free-form user text (LLM-assisted). This keeps the flow
predictable and testable while still letting users answer naturally
("I'm Priya, 98765 43210, looking for a dental cleanup") instead of being
forced through a rigid one-field-per-message wizard.
"""
from __future__ import annotations

from .llm_client import LLMClient

# Ordered so the flow asks for the most identity-critical info first.
REQUIRED_SLOTS = ["name", "phone", "requirement"]
OPTIONAL_SLOTS = ["email", "budget"]

PROMPTS = {
    "name": "Could I get your name?",
    "phone": "What's the best phone number to reach you on?",
    "requirement": "What service are you interested in?",
    "email": "What's your email address? (optional)",
    "budget": "Do you have a budget range in mind? (optional)",
}


class QualificationFlow:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def missing_required_slots(self, slots: dict) -> list[str]:
        return [s for s in REQUIRED_SLOTS if not slots.get(s)]

    def process_turn(self, user_text: str, slots: dict) -> dict:
        """
        Extracts any slot values present in user_text, merges them into a
        COPY of slots, and returns:
          {
            "slots": updated_slots,
            "missing": [...],
            "complete": bool,
            "next_prompt": str | None,
          }
        """
        updated = dict(slots)
        needed = self.missing_required_slots(updated) + [
            s for s in OPTIONAL_SLOTS if s not in updated
        ]
        extracted = self.llm.extract_slots(user_text, updated, needed)
        updated.update({k: v for k, v in extracted.items() if v})

        missing = self.missing_required_slots(updated)
        complete = len(missing) == 0

        next_prompt = None
        if not complete:
            next_prompt = PROMPTS.get(missing[0], f"Could you share your {missing[0]}?")

        return {
            "slots": updated,
            "missing": missing,
            "complete": complete,
            "next_prompt": next_prompt,
        }
