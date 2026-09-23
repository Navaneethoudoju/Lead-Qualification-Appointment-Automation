"""
Persistence layer — PostgreSQL.

Session 6 upgrade: this used to be stdlib sqlite3 (see database.py.orig.bak
for the prototype version, kept for reference). That was fine for a single
process demo; it doesn't hold up for a real multi-user deployment — no
concurrent writers, no querying from multiple backend replicas, and a
single file that has to live on one machine's disk.

This module now talks to PostgreSQL through psycopg2, using a small
threaded connection pool so multiple FastAPI worker threads/requests can
each get their own connection without serializing on a single one. Every
public method keeps the exact same name/signature/return shape as before
(a dict-like row per record, via RealDictCursor) — so orchestrator.py,
main.py, and every tool module needed zero changes.

Configure via DATABASE_URL, e.g.:
  postgresql://ai_leadflow:ai_leadflow@localhost:5432/ai_leadflow
"""
from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/ai_leadflow"

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp TEXT NOT NULL
);

-- `seq` gives us a real, monotonic insertion-order column to sort on.
-- SQLite's implicit `rowid` did this for free; Postgres needs it explicit.
CREATE TABLE IF NOT EXISTS leads (
    seq BIGSERIAL,
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    name TEXT,
    phone TEXT,
    email TEXT,
    requirement TEXT,
    budget TEXT,
    qualification_score DOUBLE PRECISION,
    crm_synced INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS appointments (
    id TEXT PRIMARY KEY,
    lead_id TEXT NOT NULL REFERENCES leads(id),
    slot_datetime TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    calendar_event_id TEXT
);

CREATE TABLE IF NOT EXISTS escalations (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    notified INTEGER NOT NULL DEFAULT 0
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


class Database:
    def __init__(self, database_url: Optional[str] = None, minconn: int = 1, maxconn: int = 10):
        self.database_url = database_url or os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
        self._pool = ThreadedConnectionPool(minconn, maxconn, dsn=self.database_url)
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = self._pool.getconn()
        try:
            yield conn
        finally:
            self._pool.putconn(conn)

    def _init_schema(self) -> None:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA)
            conn.commit()

    def close(self) -> None:
        self._pool.closeall()

    @contextmanager
    def cursor(self):
        """Yields a RealDictCursor (dict-like rows, same as sqlite3.Row
        access pattern: row["col"]). Commits on success, rolls back on
        exception, always returns the connection to the pool."""
        with self._conn() as conn:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            try:
                yield cur
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                cur.close()

    # ---- Conversations -----------------------------------------------

    def get_or_create_conversation(self, session_id: str, channel: str):
        with self.cursor() as cur:
            cur.execute("SELECT * FROM conversations WHERE id = %s", (session_id,))
            row = cur.fetchone()
            if row:
                return row
            ts = now_iso()
            cur.execute(
                "INSERT INTO conversations (id, channel, started_at, updated_at, status) VALUES (%s, %s, %s, %s, 'active')",
                (session_id, channel, ts, ts),
            )
            cur.execute("SELECT * FROM conversations WHERE id = %s", (session_id,))
            return cur.fetchone()

    def update_conversation_status(self, conversation_id: str, status: str) -> None:
        with self.cursor() as cur:
            cur.execute(
                "UPDATE conversations SET status = %s, updated_at = %s WHERE id = %s",
                (status, now_iso(), conversation_id),
            )

    def get_conversation(self, conversation_id: str):
        with self.cursor() as cur:
            cur.execute("SELECT * FROM conversations WHERE id = %s", (conversation_id,))
            return cur.fetchone()

    def list_conversations(self, limit: int = 50, offset: int = 0):
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM conversations ORDER BY updated_at DESC LIMIT %s OFFSET %s",
                (limit, offset),
            )
            return cur.fetchall()

    def count_conversations(self) -> int:
        with self.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM conversations")
            return cur.fetchone()["n"]

    # ---- Messages -------------------------------------------------------

    def add_message(self, conversation_id: str, role: str, content: str) -> str:
        mid = new_id()
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO messages (id, conversation_id, role, content, timestamp) VALUES (%s, %s, %s, %s, %s)",
                (mid, conversation_id, role, content, now_iso()),
            )
            cur.execute(
                "UPDATE conversations SET updated_at = %s WHERE id = %s",
                (now_iso(), conversation_id),
            )
        return mid

    def get_messages(self, conversation_id: str):
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM messages WHERE conversation_id = %s ORDER BY timestamp ASC",
                (conversation_id,),
            )
            return cur.fetchall()

    # ---- Leads ------------------------------------------------------------

    def upsert_lead(self, conversation_id: str, slots: dict) -> str:
        with self.cursor() as cur:
            cur.execute("SELECT id FROM leads WHERE conversation_id = %s", (conversation_id,))
            row = cur.fetchone()
            if row:
                lead_id = row["id"]
                cur.execute(
                    """UPDATE leads SET name=%s, phone=%s, email=%s, requirement=%s, budget=%s
                       WHERE id = %s""",
                    (
                        slots.get("name"),
                        slots.get("phone"),
                        slots.get("email"),
                        slots.get("requirement"),
                        slots.get("budget"),
                        lead_id,
                    ),
                )
            else:
                lead_id = new_id()
                cur.execute(
                    """INSERT INTO leads (id, conversation_id, name, phone, email, requirement, budget, qualification_score, crm_synced)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0)""",
                    (
                        lead_id,
                        conversation_id,
                        slots.get("name"),
                        slots.get("phone"),
                        slots.get("email"),
                        slots.get("requirement"),
                        slots.get("budget"),
                        slots.get("qualification_score"),
                    ),
                )
        return lead_id

    def mark_lead_synced(self, lead_id: str) -> None:
        with self.cursor() as cur:
            cur.execute("UPDATE leads SET crm_synced = 1 WHERE id = %s", (lead_id,))

    def get_lead_by_conversation(self, conversation_id: str):
        with self.cursor() as cur:
            cur.execute("SELECT * FROM leads WHERE conversation_id = %s", (conversation_id,))
            return cur.fetchone()

    def get_lead(self, lead_id: str):
        with self.cursor() as cur:
            cur.execute("SELECT * FROM leads WHERE id = %s", (lead_id,))
            return cur.fetchone()

    def list_leads(self, limit: int = 50, offset: int = 0):
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM leads ORDER BY seq DESC LIMIT %s OFFSET %s",
                (limit, offset),
            )
            return cur.fetchall()

    def count_leads(self) -> int:
        with self.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM leads")
            return cur.fetchone()["n"]

    def update_lead(self, lead_id: str, fields: dict):
        """Partial update for admin dashboard in-place editing (session 6).
        Only columns present as keys in `fields` are touched, so a caller can
        send just `{"phone": "..."}` without clobbering the rest of the row.
        Returns the updated row, or None if lead_id doesn't exist."""
        allowed = {"name", "phone", "email", "requirement", "budget", "qualification_score", "crm_synced"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return self.get_lead(lead_id)
        with self.cursor() as cur:
            cur.execute("SELECT id FROM leads WHERE id = %s", (lead_id,))
            if not cur.fetchone():
                return None
            set_clause = ", ".join(f"{col} = %s" for col in updates)
            cur.execute(
                f"UPDATE leads SET {set_clause} WHERE id = %s",
                (*updates.values(), lead_id),
            )
            cur.execute("SELECT * FROM leads WHERE id = %s", (lead_id,))
            return cur.fetchone()

    # ---- Appointments -----------------------------------------------------

    def list_appointments_on(self, date_str: str):
        with self.cursor() as cur:
            cur.execute(
                "SELECT * FROM appointments WHERE slot_datetime LIKE %s AND status != 'cancelled'",
                (f"{date_str}%",),
            )
            return cur.fetchall()

    def create_appointment(self, lead_id: str, slot_datetime: str, calendar_event_id: str) -> str:
        aid = new_id()
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO appointments (id, lead_id, slot_datetime, status, calendar_event_id)
                   VALUES (%s, %s, %s, 'confirmed', %s)""",
                (aid, lead_id, slot_datetime, calendar_event_id),
            )
        return aid

    def is_slot_booked(self, slot_datetime: str) -> bool:
        with self.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM appointments WHERE slot_datetime = %s AND status != 'cancelled'",
                (slot_datetime,),
            )
            return cur.fetchone() is not None

    def list_appointments(self, limit: int = 50, offset: int = 0):
        """All appointments, newest slot first, joined with the lead's
        name/phone/conversation_id so the admin dashboard can render a
        useful bookings table without a second round-trip per row."""
        with self.cursor() as cur:
            cur.execute(
                """SELECT
                       appointments.id AS id,
                       appointments.lead_id AS lead_id,
                       appointments.slot_datetime AS slot_datetime,
                       appointments.status AS status,
                       appointments.calendar_event_id AS calendar_event_id,
                       leads.name AS lead_name,
                       leads.phone AS lead_phone,
                       leads.conversation_id AS conversation_id
                   FROM appointments
                   JOIN leads ON leads.id = appointments.lead_id
                   ORDER BY appointments.slot_datetime DESC
                   LIMIT %s OFFSET %s""",
                (limit, offset),
            )
            return cur.fetchall()

    def count_appointments(self) -> int:
        with self.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM appointments")
            return cur.fetchone()["n"]

    # ---- Stats (session 6) ---------------------------------------------

    def get_stats(self) -> dict:
        with self.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM leads")
            total_leads = cur.fetchone()["n"]

            cur.execute("SELECT COUNT(*) AS n FROM leads WHERE crm_synced = 1")
            synced_leads = cur.fetchone()["n"]

            cur.execute("SELECT COUNT(*) AS n FROM conversations")
            total_conversations = cur.fetchone()["n"]

            cur.execute("SELECT COUNT(*) AS n FROM conversations WHERE status = 'escalated'")
            escalated_conversations = cur.fetchone()["n"]

            cur.execute("SELECT COUNT(*) AS n FROM conversations WHERE status = 'booked'")
            booked_conversations = cur.fetchone()["n"]

            cur.execute("SELECT COUNT(*) AS n FROM appointments WHERE status != 'cancelled'")
            active_appointments = cur.fetchone()["n"]

            cur.execute("SELECT COUNT(*) AS n FROM appointments")
            total_appointments = cur.fetchone()["n"]

        return {
            "total_leads": total_leads,
            "synced_leads": synced_leads,
            "total_conversations": total_conversations,
            "escalated_conversations": escalated_conversations,
            "booked_conversations": booked_conversations,
            "active_appointments": active_appointments,
            "total_appointments": total_appointments,
        }

    # ---- Escalations --------------------------------------------------

    def log_escalation(self, conversation_id: str, reason: str) -> str:
        eid = new_id()
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO escalations (id, conversation_id, reason, created_at, notified) VALUES (%s, %s, %s, %s, 1)",
                (eid, conversation_id, reason, now_iso()),
            )
        return eid

    def list_escalations(self, limit: int = 50, offset: int = 0):
        """All escalations, newest first, joined with the conversation's
        channel/status so the admin dashboard can render a useful table
        without a second round-trip per row. Mirrors list_appointments'
        shape (paginated {items,total,limit,offset} envelope, built by the
        caller in main.py)."""
        with self.cursor() as cur:
            cur.execute(
                """SELECT
                       escalations.id AS id,
                       escalations.conversation_id AS conversation_id,
                       escalations.reason AS reason,
                       escalations.created_at AS created_at,
                       escalations.notified AS notified,
                       conversations.channel AS channel,
                       conversations.status AS conversation_status
                   FROM escalations
                   JOIN conversations ON conversations.id = escalations.conversation_id
                   ORDER BY escalations.created_at DESC
                   LIMIT %s OFFSET %s""",
                (limit, offset),
            )
            return cur.fetchall()

    def count_escalations(self) -> int:
        with self.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM escalations")
            return cur.fetchone()["n"]
