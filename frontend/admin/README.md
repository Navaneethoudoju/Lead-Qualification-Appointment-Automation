# Admin Dashboard

Single static HTML page, same dependency-free approach as `frontend/widget/`:
no npm, no build step, no framework — just `index.html` with inline CSS and
vanilla JS. Rebuilt in session 6 for a professional-console layout (dark
sidebar nav, live global stats, search, in-place lead editing); the API
calls underneath are unchanged and documented below.

## What it does

Open `index.html` directly in a browser (or serve it from any static host)
and it talks to the running API:

- **Overview** — 5 stat cards (`GET /api/admin/stats`, added session 6:
  real `COUNT(*)` totals, not derived from whatever page happens to be
  loaded) plus preview tables of the 5 most recent leads/conversations.
- **Leads** — `GET /api/admin/leads` (paginated), with a client-side search
  box (name/phone/email/requirement) and CRM-synced badge. Click a row to
  open the **edit drawer**: pre-filled fields, "Save changes" →
  `PATCH /api/admin/leads/{id}` (added session 6 — only the fields you
  change are sent), and a "View conversation" shortcut into the transcript
  modal.
- **Manually add a lead** — inline form at the bottom of the Leads table →
  `POST /api/admin/leads` (for walk-ins/offline leads with no chat
  conversation yet — see note below).
- **Conversations** — `GET /api/admin/conversations` (paginated + searchable),
  status badge (active / qualified / booked / escalated / closed), click-
  through to the transcript modal (`GET /api/conversations/{id}`).
- **Appointments** — `GET /api/admin/appointments` (paginated + searchable),
  each row already joined server-side with the lead's `name`/`phone`/
  `conversation_id`, so it renders in one call with no per-row lookup.
- **Connection settings** — gear-style popover (bottom of the sidebar) for
  API base URL + Basic-auth username/password, persisted to `localStorage`
  — same static file can point at `localhost:8000` in dev or a deployed
  backend URL in prod, no rebuild needed.
- **Toast notifications** for save/error feedback instead of a banner.

## Running it

```bash
cd ai-leadflow
uvicorn app.main:app --reload --port 8000   # in one terminal
python3 -m http.server 8080 --directory frontend/admin  # in another
```

Then open `http://localhost:8080`. (Opening `index.html` via `file://`
directly also works, as long as the API's CORS config allows it — see
`AI_LEADFLOW_CORS_ORIGINS` in `.env.example`; the default `*` allows it.)

## Auth

Every `/api/admin/*` route requires HTTP Basic auth (`app/main.py:verify_admin`).
The connection-settings popover has **Username** / **Password** fields —
they default to `admin` / `admin`, matching `config.py`'s fallback when
`AI_LEADFLOW_ADMIN_USERNAME` / `AI_LEADFLOW_ADMIN_PASSWORD` aren't set, so
the dashboard works with zero setup for a local demo. `GET /health`'s
`admin_auth_mode` field tells you `"default_credentials"` vs `"configured"`
without checking env vars by hand — check that before pointing this
dashboard at anything beyond localhost. A wrong password surfaces as a
toast rather than a silent empty table.

Credentials are stored in this browser's `localStorage`, same as the API
base URL — convenient for a local demo, but plaintext in the browser and
sent in plaintext over the wire unless the API itself is behind HTTPS. Set
real, non-default credentials via env vars and put a TLS-terminating proxy
in front before this is reachable from anywhere but localhost.

## A bug this surfaced (fixed in `app/main.py`, still holding)

`POST /api/admin/leads` and `POST /api/tools/crm/push-lead` both write
straight to `leads.conversation_id`, which is a foreign key into
`conversations`. That's fine for leads created through the normal chat flow
(the orchestrator always creates the conversation row first), but the admin
dashboard's "manually add a lead" form has no prior conversation. Fixed by
having both endpoints call `db.get_or_create_conversation(conversation_id,
"manual")` first (idempotent — a real chat session with that id, if one
exists, is left untouched).

## Not done yet

- **HTTPS** isn't handled by this app itself — Basic auth over plain HTTP
  sends credentials in a headers value that's trivially decoded if
  intercepted, so put this behind TLS (a reverse proxy, or your hosting
  platform's HTTPS) before it's reachable from anywhere but localhost.
- **Never actually opened in a real browser** — every session so far,
  including this one, has verified the dashboard by inspection only (valid
  HTML, every `getElementById` target present, `node --check` on the
  inline script) because no sandbox in this project's history has had a
  browser binary. Do this once before trusting the visual layer in
  production; see PROGRESS.md's "what remains" list.
- **No bulk actions** (e.g. multi-select leads to sync/export) — out of
  scope for Phase 1.5 per the architecture doc; not attempted.
