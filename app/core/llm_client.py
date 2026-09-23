"""
LLM client abstraction.

Provider abstraction is the point of this module: orchestrator.py, the
intent classifier, the RAG engine, and the qualification flow all call the
same four LLMClient methods (classify_intent / grounded_answer /
extract_slots / detect_negative_sentiment) and never know or care which
backend actually answered. Three backends, selected automatically:

1. **Anthropic** — if ANTHROPIC_API_KEY is set, calls the Messages API
   directly over HTTPS (urllib, stdlib only — no SDK dependency required).
   The intended production path once a client API key is available.
2. **Ollama** (session 7) — the project's documented free-development
   backend: a locally-running model (llama3.1, mistral, etc. — whatever
   `ollama pull`ed) with zero API cost and zero external network calls at
   inference time. Selected if OLLAMA_BASE_URL is set (or left at its
   default `http://localhost:11434` once OLLAMA_MODEL is set) and no
   ANTHROPIC_API_KEY is configured. Uses Ollama's /api/chat endpoint,
   non-streaming, same urllib-only approach as the Anthropic path.
3. **Mock** — deterministic, keyword/rule-based logic, zero dependencies,
   zero network. Automatic fallback so the whole platform stays runnable
   and testable with no external services at all.

AI_LEADFLOW_LLM_PROVIDER can force one of "anthropic" / "ollama" / "mock"
explicitly (e.g. to make a deployment fail fast if its expected backend
isn't actually configured, rather than silently falling back to mock).
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
import urllib.error
from typing import Optional

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MODEL = ANTHROPIC_DEFAULT_MODEL  # kept for backwards compatibility

OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434"
OLLAMA_DEFAULT_MODEL = "llama3.1"

_VALID_PROVIDERS = {"anthropic", "ollama", "mock"}


class LLMError(Exception):
    pass


class LLMClient:
    """Public interface used by the rest of the app."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = ANTHROPIC_DEFAULT_MODEL,
        ollama_base_url: Optional[str] = None,
        ollama_model: Optional[str] = None,
    ):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.model = model
        self.ollama_base_url = ollama_base_url or os.environ.get("OLLAMA_BASE_URL", OLLAMA_DEFAULT_BASE_URL)
        self.ollama_model = ollama_model or os.environ.get("OLLAMA_MODEL", OLLAMA_DEFAULT_MODEL)
        self._mock = MockLLM()

        forced = os.environ.get("AI_LEADFLOW_LLM_PROVIDER")
        if forced:
            if forced not in _VALID_PROVIDERS:
                raise LLMError(f"AI_LEADFLOW_LLM_PROVIDER={forced!r} is not one of {sorted(_VALID_PROVIDERS)}")
            if forced == "anthropic" and not self.api_key:
                raise LLMError("AI_LEADFLOW_LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set")
            self.mode = forced
            return

        if self.api_key:
            self.mode = "anthropic"
        elif os.environ.get("OLLAMA_BASE_URL") or os.environ.get("OLLAMA_MODEL"):
            # Only auto-select Ollama if the caller actually configured it —
            # OLLAMA_DEFAULT_BASE_URL alone (nothing set) stays mock, so a
            # laptop with no Ollama installed doesn't hang every request
            # trying to reach localhost:11434.
            self.mode = "ollama"
        else:
            self.mode = "mock"

    # ---- Public high-level operations -----------------------------------

    def classify_intent(self, user_text: str, history: list[dict]) -> dict:
        """Returns {"intent": str, "confidence": float, "reasoning": str}."""
        if self.mode == "anthropic":
            return self._anthropic_classify_intent(user_text, history)
        if self.mode == "ollama":
            return self._ollama_classify_intent(user_text, history)
        return self._mock.classify_intent(user_text, history)

    def grounded_answer(self, question: str, context_chunks: list[str]) -> dict:
        """Returns {"answer": str, "grounded": bool}."""
        if self.mode == "anthropic":
            return self._anthropic_grounded_answer(question, context_chunks)
        if self.mode == "ollama":
            return self._ollama_grounded_answer(question, context_chunks)
        return self._mock.grounded_answer(question, context_chunks)

    def extract_slots(self, user_text: str, known_slots: dict, needed: list[str]) -> dict:
        """Returns a dict of any of `needed` slot values found in user_text."""
        if self.mode == "anthropic":
            return self._anthropic_extract_slots(user_text, known_slots, needed)
        if self.mode == "ollama":
            return self._ollama_extract_slots(user_text, known_slots, needed)
        return self._mock.extract_slots(user_text, known_slots, needed)

    def detect_negative_sentiment(self, user_text: str) -> bool:
        if self.mode == "anthropic":
            return self._anthropic_detect_negative_sentiment(user_text)
        if self.mode == "ollama":
            return self._ollama_detect_negative_sentiment(user_text)
        return self._mock.detect_negative_sentiment(user_text)

    # ---- Real Anthropic API path (untested here — no network in sandbox) -

    def _anthropic_raw_call(self, system: str, user_content: str) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "max_tokens": 1000,
                "system": system,
                "messages": [{"role": "user", "content": user_content}],
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            ANTHROPIC_API_URL,
            data=body,
            method="POST",
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise LLMError(f"Anthropic API call failed: {e}") from e

        text_parts = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
        return "\n".join(text_parts)

    def _anthropic_classify_intent(self, user_text: str, history: list[dict]) -> dict:
        system = (
            "You are an intent classifier for a business chat assistant. "
            "Classify the user's latest message into exactly one of: "
            "faq, lead_qualification, booking_request, escalation. "
            'Respond ONLY with JSON: {"intent": "...", "confidence": 0.0-1.0, "reasoning": "..."}'
        )
        try:
            raw = self._anthropic_raw_call(system, user_text)
            return json.loads(_strip_code_fences(raw))
        except Exception:
            # Never let an LLM/network hiccup crash the conversation — fall
            # back to the deterministic mock and let escalation logic
            # (confidence < 0.6) route it to a human if needed.
            return self._mock.classify_intent(user_text, history)

    def _anthropic_grounded_answer(self, question: str, context_chunks: list[str]) -> dict:
        system = (
            "Answer the user's question using ONLY the provided context. "
            "If the answer is not in the context, say you don't know and that "
            "you'll connect them with a team member. Never invent prices, "
            "availability, or policies. "
            'Respond ONLY with JSON: {"answer": "...", "grounded": true/false}'
        )
        content = f"CONTEXT:\n{chr(10).join(context_chunks)}\n\nQUESTION:\n{question}"
        try:
            raw = self._anthropic_raw_call(system, content)
            return json.loads(_strip_code_fences(raw))
        except Exception:
            return self._mock.grounded_answer(question, context_chunks)

    def _anthropic_extract_slots(self, user_text: str, known_slots: dict, needed: list[str]) -> dict:
        system = (
            f"Extract these fields if present in the user's message: {needed}. "
            'Respond ONLY with a JSON object of the fields you found, e.g. {"name": "..."}. '
            "Omit fields you cannot find. Do not guess."
        )
        try:
            raw = self._anthropic_raw_call(system, user_text)
            return json.loads(_strip_code_fences(raw))
        except Exception:
            return self._mock.extract_slots(user_text, known_slots, needed)

    def _anthropic_detect_negative_sentiment(self, user_text: str) -> bool:
        system = 'Is this message an angry complaint or expressing frustration? Respond ONLY with JSON: {"negative": true/false}'
        try:
            raw = self._anthropic_raw_call(system, user_text)
            return bool(json.loads(_strip_code_fences(raw)).get("negative", False))
        except Exception:
            return self._mock.detect_negative_sentiment(user_text)

    # ---- Ollama path (session 7, free local dev) ---------------------------
    # Mirrors the Anthropic methods above exactly — same prompts, same
    # JSON-only response contract, same "any hiccup falls back to MockLLM"
    # policy — so swapping OLLAMA_BASE_URL/OLLAMA_MODEL for
    # ANTHROPIC_API_KEY later changes nothing else about the app's behavior.

    def _ollama_raw_call(self, system: str, user_content: str) -> str:
        body = json.dumps(
            {
                "model": self.ollama_model,
                "stream": False,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_content},
                ],
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.ollama_base_url.rstrip('/')}/api/chat",
            data=body,
            method="POST",
            headers={"content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise LLMError(
                f"Ollama call failed ({self.ollama_base_url}, model={self.ollama_model}): {e}. "
                "Is `ollama serve` running and has the model been pulled (`ollama pull "
                f"{self.ollama_model}`)?"
            ) from e
        return data.get("message", {}).get("content", "")

    def _ollama_classify_intent(self, user_text: str, history: list[dict]) -> dict:
        system = (
            "You are an intent classifier for a business chat assistant. "
            "Classify the user's latest message into exactly one of: "
            "faq, lead_qualification, booking_request, escalation. "
            'Respond ONLY with JSON: {"intent": "...", "confidence": 0.0-1.0, "reasoning": "..."}'
        )
        try:
            raw = self._ollama_raw_call(system, user_text)
            return json.loads(_strip_code_fences(raw))
        except Exception:
            # Same policy as the Anthropic path: never let a local-model or
            # connection hiccup crash the conversation — fall back to the
            # deterministic mock, and let the confidence<0.6 escalation rule
            # route ambiguous cases to a human if needed.
            return self._mock.classify_intent(user_text, history)

    def _ollama_grounded_answer(self, question: str, context_chunks: list[str]) -> dict:
        system = (
            "Answer the user's question using ONLY the provided context. "
            "If the answer is not in the context, say you don't know and that "
            "you'll connect them with a team member. Never invent prices, "
            "availability, or policies. "
            'Respond ONLY with JSON: {"answer": "...", "grounded": true/false}'
        )
        content = f"CONTEXT:\n{chr(10).join(context_chunks)}\n\nQUESTION:\n{question}"
        try:
            raw = self._ollama_raw_call(system, content)
            return json.loads(_strip_code_fences(raw))
        except Exception:
            return self._mock.grounded_answer(question, context_chunks)

    def _ollama_extract_slots(self, user_text: str, known_slots: dict, needed: list[str]) -> dict:
        system = (
            f"Extract these fields if present in the user's message: {needed}. "
            'Respond ONLY with a JSON object of the fields you found, e.g. {"name": "..."}. '
            "Omit fields you cannot find. Do not guess."
        )
        try:
            raw = self._ollama_raw_call(system, user_text)
            return json.loads(_strip_code_fences(raw))
        except Exception:
            return self._mock.extract_slots(user_text, known_slots, needed)

    def _ollama_detect_negative_sentiment(self, user_text: str) -> bool:
        system = 'Is this message an angry complaint or expressing frustration? Respond ONLY with JSON: {"negative": true/false}'
        try:
            raw = self._ollama_raw_call(system, user_text)
            return bool(json.loads(_strip_code_fences(raw)).get("negative", False))
        except Exception:
            return self._mock.detect_negative_sentiment(user_text)


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    return text


# ---------------------------------------------------------------------------
# Mock LLM: deterministic, offline, zero dependencies. Powers the whole demo
# when no ANTHROPIC_API_KEY is configured.
# ---------------------------------------------------------------------------

_HUMAN_REQUEST_RE = re.compile(
    r"\b(talk to|speak to|connect me|human|agent|real person|representative|staff member)\b",
    re.I,
)
_BOOKING_RE = re.compile(
    r"\b(book|appointment|schedule|slot|available time|reserve|reschedul\w*|cancel\w* (my )?appointment)\b",
    re.I,
)
_LEAD_RE = re.compile(
    r"\b(interested in|pricing for|quote|sign up|enroll|get started|i want|i need|i'm looking for|im looking for|looking for)\b",
    re.I,
)
_NEGATIVE_RE = re.compile(
    r"\b(angry|furious|terrible|awful|worst|unacceptable|disgusted|frustrat\w*|ridiculous|scam|never again|complain\w*)\b",
    re.I,
)
_PHONE_RE = re.compile(r"(\+?\d[\d\-\s]{7,}\d)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_DATE_HINT_RE = re.compile(
    r"\b(today|tomorrow|mon|tue|wed|thu|fri|sat|sun|next week|"
    r"\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|"
    r"jan\w*|feb\w*|mar\w*|apr\w*|may|jun\w*|jul\w*|aug\w*|sep\w*|oct\w*|nov\w*|dec\w*)\b",
    re.I,
)


class MockLLM:
    """Deterministic rule-based stand-in for the real LLM. No network calls."""

    def classify_intent(self, user_text: str, history: list[dict]) -> dict:
        text = user_text.strip()
        if not text:
            return {"intent": "escalation", "confidence": 0.3, "reasoning": "Empty message."}

        if _HUMAN_REQUEST_RE.search(text):
            return {"intent": "escalation", "confidence": 0.95, "reasoning": "User explicitly asked for a human."}

        if _BOOKING_RE.search(text):
            return {"intent": "booking_request", "confidence": 0.85, "reasoning": "Booking/appointment keywords found."}

        if _LEAD_RE.search(text) or _PHONE_RE.search(text) or _EMAIL_RE.search(text):
            return {"intent": "lead_qualification", "confidence": 0.75, "reasoning": "Sales-intent language or contact info found."}

        question_words = ("what", "how", "when", "where", "do you", "does", "can i", "is there", "are there", "why", "which")
        if text.rstrip().endswith("?") or text.lower().startswith(question_words):
            return {"intent": "faq", "confidence": 0.8, "reasoning": "Phrased as a question."}

        # Ambiguous — deliberately below the 0.6 escalation threshold.
        return {"intent": "faq", "confidence": 0.5, "reasoning": "No strong signal; defaulting to FAQ with low confidence."}

    def grounded_answer(self, question: str, context_chunks: list[str]) -> dict:
        if not context_chunks:
            return {
                "answer": "I don't have that information on hand. Let me connect you with a team member who can help.",
                "grounded": False,
            }
        # Extractive "answer": return the most relevant chunk, lightly framed.
        best = context_chunks[0].strip()
        if len(best) > 600:
            best = best[:600].rsplit(" ", 1)[0] + "..."
        return {"answer": best, "grounded": True}

    def extract_slots(self, user_text: str, known_slots: dict, needed: list[str]) -> dict:
        found = {}
        if "phone" in needed and "phone" not in known_slots:
            m = _PHONE_RE.search(user_text)
            if m:
                found["phone"] = re.sub(r"[\s-]", "", m.group(1))
        if "email" in needed and "email" not in known_slots:
            m = _EMAIL_RE.search(user_text)
            if m:
                found["email"] = m.group(0)
        if "preferred_date" in needed and "preferred_date" not in known_slots:
            m = _DATE_HINT_RE.search(user_text)
            if m:
                found["preferred_date"] = m.group(0)
        if "name" in needed and "name" not in known_slots:
            # crude heuristic: "my name is X" / "I'm X" / "I am X"
            # Only the prefix phrase is case-insensitive; the captured name
            # itself must still start with a capital letter (proper-noun
            # heuristic) so casual phrases like "I'm interested in..." don't
            # get misread as "I'm <Name>".
            m = re.search(
                r"(?:(?i:my name is|i am|i'm))\s+([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)?)",
                user_text,
            )
            if m:
                found["name"] = m.group(1)
        if "requirement" in needed and "requirement" not in known_slots:
            # fallback: treat the raw message as the requirement if nothing
            # more specific has been captured and it's not just contact info
            stripped = _PHONE_RE.sub("", _EMAIL_RE.sub("", user_text)).strip()
            if len(stripped) > 3 and not found.get("name"):
                found["requirement"] = stripped[:300]
        return found

    def detect_negative_sentiment(self, user_text: str) -> bool:
        return bool(_NEGATIVE_RE.search(user_text))
