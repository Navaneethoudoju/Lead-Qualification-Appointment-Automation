"""
CRM tool.

Session 7 scope change: the original design doc scoped this as "Airtable
(mock/lightweight)" for the prototype, with sqlite/Postgres as just the
durable backing store underneath it. The revised architecture drops
Airtable entirely — PostgreSQL *is* the CRM (leads, conversations,
appointments, escalations all live there; the admin dashboard reads
straight from it). This module is now a thin, single-mode wrapper: it
exists so orchestrator.py has a stable `push_lead()` call site and a
`.mode` attribute (surfaced at GET /health as `crm_mode`, same idiom as
llm_mode/calendar_mode/embeddings_mode) without needing to know that
"CRM" and "database" are the same thing.

If a future session wants a *second* CRM destination (Airtable, HubSpot,
Salesforce...) in addition to Postgres, the plug-in point is
`_push_external()` below — same pattern as calendar_tool.py's mock/Google
split: validate + write to Postgres first (the source of truth), then best-
effort push to whatever's external, never the other way around.
"""
from __future__ import annotations

from ..database import Database


class CRMTool:
    def __init__(self, db: Database):
        self.db = db
        self.mode = "postgres"

    def push_lead(self, conversation_id: str, slots: dict) -> dict:
        """Upserts the lead into the `leads` table (Database.upsert_lead)
        and marks it synced. Since Postgres is the CRM, "push" and "persist"
        are the same operation — there's no separate external call that can
        fail independently, so crm_synced is always True once this returns
        without raising."""
        lead_id = self.db.upsert_lead(conversation_id, slots)
        self.db.mark_lead_synced(lead_id)
        return {"lead_id": lead_id, "crm_synced": True}
