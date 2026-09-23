"""
FastAPI orchestrator API (Section 7 of the architecture doc).

This is a thin HTTP layer over app/core/orchestrator.py — all the actual
conversation logic lives in the core package and is unit/e2e tested
independently of this web layer (see tests/). Run with:

    uvicorn app.main:app --reload --port 8000

NOTE: this file requires `fastapi`, `uvicorn`, and `pydantic` (see
requirements.txt) which are not installed in the build sandbox used to
create this project (no network access there). Its logic mirrors the
already-tested Orchestrator interface exactly, so the risk surface here is
just request/response wiring — but you should run the test suite in
tests/test_api.py (or at minimum `uvicorn app.main:app` + a manual curl)
once you have the dependencies installed, before deploying.
"""
from __future__ import annotations

import secrets
import time
import uuid
from typing import Any, Optional

try:
    from dotenv import load_dotenv  # optional; see requirements.txt
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed — fine, just export env vars directly

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from . import config
from .core.database import Database
from .core.ingest import build_vector_store_from_dir
from .core.llm_client import LLMClient
from .core.logging_config import configure_logging, get_logger
from .core.orchestrator import Orchestrator
from .core.rag_engine import RAGEngine
from .core.rate_limiter import RedisRateLimiter
from .core.state_store import RedisStateStore
from .core.tools.calendar_tool import CalendarTool
from .core.tools.crm_tool import CRMTool
from .core.tools.escalation import EscalationNotifier

configure_logging()
logger = get_logger(__name__)

app = FastAPI(title="AI LeadFlow Orchestrator API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- Request logging + correlation IDs -------------------------------------
# Every request gets a request_id (client-supplied via X-Request-ID, or a
# generated uuid4) that's echoed back in the response header and attached to
# every log line for that request — the minimum viable version of the
# "trace a single user's request through the logs" capability a real
# on-call engineer needs. Also logs method/path/status/duration for every
# request, which is what most APM/log-based alerting is built on.
@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    start = time.monotonic()
    response = await call_next(request)
    duration_ms = round((time.monotonic() - start) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request_completed",
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        },
    )
    return response


# ---- Rate limiting ----------------------------------------------------
# Applied only to /api/message (the one publicly reachable, unauthenticated,
# LLM-calling endpoint — the obvious abuse/cost target). Admin routes are
# already behind auth; internal /api/tools/* routes are meant to be called
# by the orchestrator/trusted callers. See rate_limiter.py for the documented
# single-process limitation and Redis upgrade path.
_message_rate_limiter = RedisRateLimiter(
    max_requests=config.RATE_LIMIT_MAX_REQUESTS,
    window_seconds=config.RATE_LIMIT_WINDOW_SECONDS,
    redis_url=config.REDIS_URL,
)


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if request.url.path == "/api/message" and request.method == "POST":
        client_key = request.client.host if request.client else "unknown"
        allowed, retry_after = _message_rate_limiter.allow(client_key)
        if not allowed:
            logger.warning(
                "rate_limit_exceeded",
                extra={"client": client_key, "path": request.url.path},
            )
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests — please slow down."},
                headers={"Retry-After": str(retry_after)},
            )
    return await call_next(request)

# ---- Admin auth (new this session) -----------------------------------------
# HTTP Basic on every /api/admin/* route. Deliberately NOT applied to
# /api/message, /api/conversations/{id}, or /api/tools/* — those are the
# channel-facing and orchestrator-internal endpoints the doc scopes
# separately, and gating them would break the chat widget and the LLM's own
# function-calling loop. Credentials come from config (env-configurable,
# defaults to admin/admin so the dashboard still works with zero setup for a
# local demo — see config.py and .env.example).
_security = HTTPBasic()


def verify_admin(credentials: HTTPBasicCredentials = Depends(_security)) -> None:
    # secrets.compare_digest instead of == to avoid a timing side-channel on
    # the comparison (overkill for a prototype's admin/admin default, but
    # free to do correctly and this is exactly the kind of thing that's easy
    # to forget once real credentials are configured).
    valid_user = secrets.compare_digest(credentials.username, config.ADMIN_USERNAME)
    valid_pass = secrets.compare_digest(credentials.password, config.ADMIN_PASSWORD)
    if not (valid_user and valid_pass):
        raise HTTPException(
            status_code=401,
            detail="Invalid admin credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


_ADMIN_AUTH = [Depends(verify_admin)]

# ---- Wiring (single shared instance per process; fine for a prototype) ----

_db = Database(config.DATABASE_URL)
_llm = LLMClient()
_vector_store = build_vector_store_from_dir(config.KB_DIR)  # uses EmbeddingsClient() internally now
_rag = RAGEngine(_vector_store, _llm)
_state_store = RedisStateStore(config.REDIS_URL)
_orchestrator = Orchestrator(
    db=_db,
    rag_engine=_rag,
    llm=_llm,
    calendar_tool=CalendarTool(_db),
    crm_tool=CRMTool(_db),
    escalation_notifier=EscalationNotifier(),
    state_store=_state_store,
)


# ---- Schemas (Section 4.3 channel-agnostic message schema + Section 7 API contract) --

class MessageRequest(BaseModel):
    session_id: str
    channel: str = "web"
    text: str
    metadata: Optional[dict[str, Any]] = None


class MessageResponse(BaseModel):
    reply_text: str
    intent: str
    actions_taken: list[str]
    escalated: bool


class LeadCreateRequest(BaseModel):
    conversation_id: str
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    requirement: Optional[str] = None
    budget: Optional[str] = None


class LeadUpdateRequest(BaseModel):
    """All fields optional — a PATCH only touches what's provided.
    See Database.update_lead for the column allow-list."""
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    requirement: Optional[str] = None
    budget: Optional[str] = None
    crm_synced: Optional[bool] = None


# ---- Health ---------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "llm_mode": _llm.mode,
        "calendar_mode": _orchestrator.calendar.mode,
        "crm_mode": _orchestrator.crm.mode,
        "embeddings_mode": _vector_store.embeddings.mode,
        "kb_chunks_indexed": len(_vector_store),
        "admin_auth_mode": "default_credentials" if config.ADMIN_AUTH_IS_DEFAULT else "configured",
    }


# ---- Core messaging endpoint ------------------------------------------

@app.post("/api/message", response_model=MessageResponse)
def post_message(req: MessageRequest) -> MessageResponse:
    if not req.text or not req.text.strip():
        raise HTTPException(status_code=400, detail="text must not be empty")
    result = _orchestrator.handle_message(req.session_id, req.channel, req.text)
    return MessageResponse(**result)


@app.get("/api/conversations/{session_id}")
def get_conversation(session_id: str) -> dict:
    conversation = _db.get_conversation(session_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="conversation not found")
    messages = _db.get_messages(session_id)
    return {
        "conversation": dict(conversation),
        "messages": [dict(m) for m in messages],
    }


# ---- Admin endpoints (Phase 1.5) ---------------------------------------

@app.get("/api/admin/leads", dependencies=_ADMIN_AUTH)
def list_leads(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    return {
        "items": [dict(l) for l in _db.list_leads(limit=limit, offset=offset)],
        "total": _db.count_leads(),
        "limit": limit,
        "offset": offset,
    }


@app.get("/api/admin/leads/{lead_id}", dependencies=_ADMIN_AUTH)
def get_lead(lead_id: str) -> dict:
    lead = _db.get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="lead not found")
    return dict(lead)


@app.post("/api/admin/leads", dependencies=_ADMIN_AUTH)
def create_lead(req: LeadCreateRequest) -> dict:
    """Manual lead creation/edit from the admin dashboard (bypasses the CRM push flow).

    `leads.conversation_id` is a foreign key into `conversations`, but an
    admin adding a walk-in/manual lead has no prior chat conversation to
    point at. get_or_create_conversation is idempotent (a real chat session
    with this id, if one exists, is left untouched), so this makes manual
    lead creation work without weakening the FK for real chat-linked leads.
    """
    _db.get_or_create_conversation(req.conversation_id, "manual")
    slots = req.model_dump(exclude={"conversation_id"}, exclude_none=True)
    lead_id = _db.upsert_lead(req.conversation_id, slots)
    return {"lead_id": lead_id}


@app.patch("/api/admin/leads/{lead_id}", dependencies=_ADMIN_AUTH)
def update_lead(lead_id: str, req: LeadUpdateRequest) -> dict:
    """In-place lead editing from the admin dashboard (session 6 —
    PROGRESS.md checklist item 16). Only the fields the caller actually
    sent are touched; omitted fields are left as-is (see LeadUpdateRequest
    / Database.update_lead)."""
    updated = _db.update_lead(lead_id, req.model_dump(exclude_none=True))
    if not updated:
        raise HTTPException(status_code=404, detail="lead not found")
    return dict(updated)


@app.get("/api/admin/stats", dependencies=_ADMIN_AUTH)
def get_stats() -> dict:
    """Global, SQL-computed counts for the dashboard's stat cards (session
    6). Replaces the session-5 approximation that derived escalated/booked
    counts from whatever page of `conversations` happened to be loaded —
    see Database.get_stats."""
    return _db.get_stats()


@app.get("/api/admin/conversations", dependencies=_ADMIN_AUTH)
def list_conversations(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    return {
        "items": [dict(c) for c in _db.list_conversations(limit=limit, offset=offset)],
        "total": _db.count_conversations(),
        "limit": limit,
        "offset": offset,
    }


@app.get("/api/admin/appointments", dependencies=_ADMIN_AUTH)
def list_appointments(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    """Appointments exist in the DB and the calendar tool already writes
    them, but had no admin-facing read path at all until session 4. Returns
    lead name/phone/conversation_id inline (via a JOIN in
    Database.list_appointments) so the dashboard doesn't need a second call
    per row. Paginated (session 5) — same {items, total, limit, offset}
    envelope as leads/conversations."""
    return {
        "items": [dict(a) for a in _db.list_appointments(limit=limit, offset=offset)],
        "total": _db.count_appointments(),
        "limit": limit,
        "offset": offset,
    }


@app.get("/api/admin/escalations", dependencies=_ADMIN_AUTH)
def list_escalations(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    """Escalation records were already written to PostgreSQL by
    Orchestrator._escalate (via Database.log_escalation) and counted in
    /api/admin/stats, but had no dedicated list endpoint — the dashboard's
    Escalations section had nothing to page through. Same
    {items, total, limit, offset} envelope as leads/conversations/
    appointments."""
    return {
        "items": [dict(e) for e in _db.list_escalations(limit=limit, offset=offset)],
        "total": _db.count_escalations(),
        "limit": limit,
        "offset": offset,
    }


# ---- Internal tool endpoints (Section 7) -------------------------------
# These mirror what the orchestrator's function-calling layer invokes
# internally; exposed here too so they can be tested/inspected directly and
# so a real LLM function-calling loop (if you wire Claude's tool-use API
# directly instead of going through orchestrator.py) has HTTP endpoints to
# call.

class AvailabilityRequest(BaseModel):
    days_ahead: int = 7


class BookRequest(BaseModel):
    lead_id: str
    slot_datetime: str


class CRMPushRequest(BaseModel):
    conversation_id: str
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    requirement: Optional[str] = None
    budget: Optional[str] = None


@app.post("/api/tools/calendar/check-availability")
def check_availability(req: AvailabilityRequest) -> dict:
    slots = _orchestrator.calendar.check_availability(days_ahead=req.days_ahead)
    return {"available_slots": slots}


@app.post("/api/tools/calendar/book")
def book_slot(req: BookRequest) -> dict:
    from .core.tools.calendar_tool import SlotUnavailableError

    try:
        return _orchestrator.calendar.book(req.lead_id, req.slot_datetime)
    except SlotUnavailableError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/api/tools/crm/push-lead")
def push_lead(req: CRMPushRequest) -> dict:
    # Same FK concern as create_lead above — this tool endpoint can be
    # called directly (not just via the orchestrator, which always has a
    # real conversation already), so make sure one exists first.
    _db.get_or_create_conversation(req.conversation_id, "manual")
    slots = req.model_dump(exclude={"conversation_id"}, exclude_none=True)
    return _orchestrator.crm.push_lead(req.conversation_id, slots)
