# AI LeadFlow — Progress Checkpoint (session 7)

**This checkpoint:** worked the "revised remaining tasks" list against the
session-6-rewired build. Same sandbox limitation as every prior session —
no network here — so nothing below has been run against a live Postgres/
Redis/Ollama/Google Calendar/Docker daemon/GitHub runner. What changed is
code, all offline-logic-tested where that's possible; see "Explicitly not
verified this session" for exactly what still needs a real run.

## Session 7 summary — what changed this session

1. **Real local embeddings — MiniLM** (`app/core/embeddings_client.py`).
   Added a third backend, `all-MiniLM-L6-v2` via `sentence-transformers`,
   ahead of the existing OpenAI/mock ones in priority (free, local, no API
   key). `AI_LEADFLOW_EMBEDDINGS_BACKEND=minilm|openai|mock` forces one
   explicitly. Lazy-imported so the module still loads with nothing
   installed (falls through to OpenAI/mock, same as before). Vector store
   already used ChromaDB (unchanged) — this closes the "real embeddings"
   gap specifically. `requirements.txt` updated (`sentence-transformers`
   is no longer commented out).
2. **Google Calendar, for real** (`app/core/tools/calendar_tool.py`). The
   previous `_google_check_availability`/`_google_book` just raised
   `NotImplementedError`. Now: service-account auth
   (`GOOGLE_CALENDAR_CREDENTIALS_JSON` = key-file contents, not a path),
   `freebusy.query` for availability, `events.insert` for booking, event id
   stored in `appointments.calendar_event_id` (existing column, same as
   mock mode's placeholder — no schema change needed). Slot generation
   (`_slot_grid`) and validation (`_validate_slot`, including our own
   `is_slot_booked` check) are now shared between mock and Google modes, so
   double-booking protection applies identically either way. New
   `CalendarConfigError` (bad/missing credentials, API unreachable) is
   caught in `orchestrator.py` and escalates to a human rather than
   crashing the request.
3. **Ollama for free local dev** (`app/core/llm_client.py`). Added as a
   third provider alongside Anthropic/mock, auto-selected when
   `OLLAMA_BASE_URL`/`OLLAMA_MODEL` is set (no `ANTHROPIC_API_KEY`
   configured), talking to `/api/chat` via stdlib `urllib` — same pattern
   as the Anthropic path, same "any hiccup falls back to MockLLM" policy.
   `AI_LEADFLOW_LLM_PROVIDER=anthropic|ollama|mock` forces one explicitly.
   The provider abstraction itself (4 public methods, backend-agnostic
   callers) is unchanged, so swapping in Anthropic later needs zero changes
   elsewhere — that was already true, just re-verified against a 3rd
   backend now.
4. **Airtable removed** (`app/core/tools/crm_tool.py`). Per the revised
   architecture ("No Airtable... PostgreSQL handles the CRM functionality"),
   the CRM tool is now a thin Postgres-only wrapper: `push_lead()` upserts
   into `leads` and marks synced — there's no second external call that can
   fail independently anymore. `.mode` is always `"postgres"`. All
   `AIRTABLE_*` env vars removed from `.env.example`.
5. **`GET /api/admin/escalations`** (`app/main.py`,
   `Database.list_escalations`/`count_escalations`). Escalations were
   already written to Postgres and counted in `/api/admin/stats`, but had
   no list endpoint — same `{items, total, limit, offset}` envelope as
   leads/conversations/appointments, joined with the parent conversation's
   channel/status. **Not yet wired into the dashboard UI** — the admin
   frontend has no Escalations table/nav item yet; that's the natural next
   step (see below).
6. **CI was broken, fixed it** (`.github/workflows/ci.yml`). The
   session-6-rewired `test-offline` job installed nothing beyond stdlib —
   but `database.py`/`rate_limiter.py` import `psycopg2`/`redis`
   unconditionally at module level (no fallback anymore, per session 6's
   own design), so that job could never have passed. Replaced both jobs
   with versions that spin up real `postgres:16`/`redis:7` service
   containers; `test-stub` installs just the psycopg2/redis client libs
   (still exercises the offline FastAPI stub, since fastapi/httpx aren't
   installed there), `test-full` installs everything in `requirements.txt`
   (so it's also the first real exercise of MiniLM + Chroma once it
   actually runs with network).
7. **`GET /health`** now also reports `embeddings_mode` (mirrors the
   existing `llm_mode`/`calendar_mode`/`crm_mode` fields) — one curl call
   tells you which of mock/minilm/openai actually loaded.

### Explicitly not verified this session (needs network / a real run)
Same structural limitation as every prior session, now covering more
surface area given items 1/3/6 above are new code paths:
- MiniLM model download + actual embedding quality/relevance scores against
  the knowledge base (`AI_LEADFLOW_EMBEDDINGS_BACKEND=minilm`).
- Google Calendar auth + freebusy/booking against a real calendar and
  service account.
- Ollama against a real running `ollama serve` instance.
- The rewritten `ci.yml` against an actual GitHub Actions runner.
- Docker Compose end-to-end with the new `sentence-transformers`/
  `google-api-python-client` dependencies added to the image.
What *was* checked offline: all edited files pass `py_compile`; the mock
paths of `embeddings_client.py`/`llm_client.py`/`calendar_tool.py` were
smoke-tested directly (bypassing the psycopg2-import chain via a stubbed
`Database` module) and behave correctly, including the Ollama-selected-but-
unreachable fallback-to-mock path and the calendar double-booking guard.

### Next recommended steps
1. Wire `GET /api/admin/escalations` into `frontend/admin/index.html` (a
   new nav section + table, same shape as the Appointments one — see
   `list_appointments()`'s JS for the pattern to copy).
2. Run this for real once network is available: `pip install -r
   requirements.txt` (now pulls in `torch` via sentence-transformers — a
   much bigger install than before), start Postgres+Redis, run the test
   suite, and confirm `GET /health` reports `"embeddings_mode": "minilm"`.
3. If deploying with Google Calendar, create a service account, share the
   target calendar with its email address (Google Calendar API requires
   this even for a calendar the account "owns" via domain-wide delegation),
   and set `GOOGLE_CALENDAR_CREDENTIALS_JSON`/`GOOGLE_CALENDAR_ID`.
4. `Dockerfile` still needs `sentence-transformers`'s dependencies (torch)
   accounted for in image size/build time — not adjusted this session.

---

# AI LeadFlow — Progress Checkpoint (session 6)

**This checkpoint:** closed out the highest-value items still fully in
Claude's control without network/Docker/real-API access — a visual and
functional pass on both frontends, plus the two backend gaps that pass
directly enabled: `GET /api/admin/stats` and in-place lead editing.

## Session 6 summary — what changed this session

1. **Admin dashboard — full visual + structural rebuild**
   (`frontend/admin/index.html`, still a single static file, zero build
   step). Session 5's dashboard was a top-bar-plus-stacked-tables layout;
   this session replaced it with a conventional SaaS-console shape:
   - Dark sidebar with brand mark, per-section nav (Overview / Leads /
     Conversations / Appointments) and live item counts, connection status
     indicator, and a settings popover for API base/credentials (previously
     always-visible text inputs in the header).
   - An **Overview** view: 5 stat cards (leads, conversations, escalated,
     booked, appointments) plus "recent leads"/"recent conversations"
     preview tables — new, didn't exist in any prior session.
   - Per-table client-side **search/filter** boxes (name/phone/email/
     requirement for leads, id/channel/status for conversations, etc.) —
     new.
   - **Lead detail/edit drawer**: clicking a lead row now opens a slide-over
     panel with editable name/phone/email/requirement/budget fields, a
     "Save changes" button wired to the new `PATCH /api/admin/leads/{id}`,
     and a "View conversation" shortcut into the existing transcript modal.
     This closes checklist item 16 ("dashboard in-place lead editing").
   - Toast notifications replaced the single dismissable banner; empty
     states got actual copy instead of "Not connected yet." placeholders.
   - Pagination, the add-lead form, and the conversation-transcript modal
     are carried over unchanged in behavior (same endpoints, same
     {items,total,limit,offset} envelope) — only the surrounding chrome
     changed.
2. **Chat widget — visual refresh** (`frontend/widget/widget.js`,
   `frontend/widget/index.html`). Same `/api/message` wire protocol, same
   zero-build vanilla-JS embed via one `<script>` tag — only presentation
   changed:
   - Gradient launcher button with an online-status dot and an open/close
     icon swap; panel now animates in (fade + slight translate) instead of
     a hard show/hide.
   - Header redesigned with an avatar chip, business name (now
     configurable via `data-brand="..."` on the script tag, defaults to
     "AI LeadFlow"), and a "typically replies instantly" status line.
   - **Typing indicator** (three animated dots) shown between send and
     response — previously there was no feedback during the request.
   - **Quick-reply starter chips** on first open (3 canned questions) to
     reduce first-message friction; removed as soon as the user sends
     anything.
   - Demo host page (`frontend/widget/index.html`) rebuilt to match the
     dashboard's visual language (same indigo brand, same type scale) with
     a short feature-card row instead of a single paragraph.
3. **`GET /api/admin/stats`** (`app/main.py`, `Database.get_stats` in
   `app/core/database.py`) — closes checklist item 15. Returns
   `total_leads`, `synced_leads`, `total_conversations`,
   `escalated_conversations`, `booked_conversations`,
   `active_appointments`, `total_appointments`, each a real `COUNT(*)`
   over the whole table. Replaces the session-5 dashboard behavior of
   deriving "escalated"/"booked" counts from whichever page of
   `conversations` happened to be loaded (documented as a known
   approximation in session 5's notes) — the new Overview cards are always
   globally correct regardless of pagination.
4. **`PATCH /api/admin/leads/{lead_id}`** (`app/main.py`,
   `Database.update_lead`) — closes checklist item 16. Partial update:
   only the fields present in the request body are written; a bare
   `{"phone": "..."}` leaves name/email/requirement/budget untouched.
   404s on an unknown `lead_id`. Basic-auth gated like every other
   `/api/admin/*` route.
5. **Offline test stub extended** with `PATCH` support
   (`tests/_offline_fastapi_stub.py`: `FastAPI.patch()` decorator +
   `StubTestClient.patch()`), so the new endpoint is covered the same way
   as every other route — offline via the stub, or against real FastAPI
   automatically once installed.
6. **37 → 42 tests passing**, still fully offline, ~0.36s: 5 new
   (`test_update_lead_patches_only_given_fields`,
   `test_update_lead_missing_is_404`, `test_update_lead_requires_auth`,
   `test_stats_reflects_global_counts_not_page_scoped`,
   `test_stats_requires_auth`).

### Explicitly not touched this session
Items 10–14, 17–19 in the checklist below are unchanged from session 5 for
the same reason stated there: they need network access to install
packages (ChromaDB, psycopg2, redis-py) or run against a live
Docker/GitHub Actions/browser environment, none of which this sandbox
has. Nothing in this session's frontend or backend work changes that
list or its blockers.

---

# AI LeadFlow — Progress Checkpoint (session 5)

**This checkpoint:** the project moved from "internship prototype" to "make
this industry-grade." Session 5's scope: pagination, structured logging,
rate limiting, and deployment scaffolding (Docker + CI). This checkpoint
also introduces an explicit **industry-grade checklist** (section 0 below)
so "how close is this to done" has a concrete, shared definition instead of
being a vibe.

## Session 5 summary — what changed this session

1. **Pagination on all three admin list endpoints.** `GET /api/admin/leads`,
   `/api/admin/conversations`, `/api/admin/appointments` now accept
   `?limit=&offset=` (default 50, max 200) and return
   `{items, total, limit, offset}` instead of a bare array. Added
   `Database.count_leads()`/`count_conversations()`/`count_appointments()`.
   **Breaking change** to the response shape of all three — anything
   calling these directly (not through the dashboard, which was updated in
   the same session) needs to read `.items` instead of the top-level array.
2. **Structured JSON logging** (`app/core/logging_config.py`, stdlib
   `logging` only). Every request now logs one JSON line
   (`request_completed`) with method/path/status/duration and a
   correlation id (`X-Request-ID`, client-supplied or generated, echoed
   back in the response header) — the standard "log to stdout, let the
   platform collect it" pattern for Docker/Kubernetes/most PaaS log
   drains. Level configurable via `AI_LEADFLOW_LOG_LEVEL`.
3. **Rate limiting** (`app/core/rate_limiter.py`, stdlib only). Sliding-
   window limiter applied to `POST /api/message` (the one publicly
   reachable, unauthenticated, LLM-calling route — the obvious cost/abuse
   target). Configurable via `AI_LEADFLOW_RATE_LIMIT_MAX_REQUESTS` /
   `_WINDOW_SECONDS` (default 20 req/60s per client IP). Documented
   limitation: in-memory, so it's per-process and doesn't survive a
   restart or work across replicas yet — same upgrade path as
   `state_store.py` (swap for Redis) would fix both at once.
4. **Offline test stub extended** to support `Query` params and the new
   `Request`/`JSONResponse`/`@app.middleware("http")` symbols, preserving
   the "same test file runs against the stub or real FastAPI" guarantee.
   `@app.middleware("http")` is a documented no-op in the stub (no real
   ASGI pipeline to hang it off) — the rate limiter's own *logic* is
   instead covered directly by 5 new dependency-free unit tests in
   `tests/test_rate_limiter.py`, which run identically with or without
   fastapi installed.
5. **Admin dashboard pagination UI** — Prev/Next controls on all three
   tables, wired to the new `{items, total}` envelope, with a
   "X–Y of Z" indicator and disabled-state styling at the start/end of the
   result set. **Known trade-off:** the "escalated"/"booked" summary stat
   cards are now computed from the current page only (documented inline in
   the dashboard JS) — a true global count needs a dedicated
   `GET /api/admin/stats` endpoint doing the count in SQL, not done this
   session.
6. **Deployment scaffolding — new, not previously started:**
   - `Dockerfile` — slim Python 3.11 image, non-root user, healthcheck
     hitting the app's own `/health`, sqlite data dir mounted as a volume.
   - `docker-compose.yml` — `backend` + `frontend` (static file server for
     the dashboard/widget) services, with the documented Postgres/Redis
     upgrade path present but commented out (uncommenting the services
     alone does not change app behavior — `database.py`/`state_store.py`
     still need the corresponding code swap first; see section 6 of the
     session-2 detail below).
   - `.github/workflows/ci.yml` — three jobs: offline stub tests, real
     FastAPI tests + a live `uvicorn` start + `/health` curl, and a Docker
     build, gated on both test jobs passing.
   - **None of the above has been run against a live Docker daemon or
     GitHub Actions runner** — this sandbox has neither. Written to the
     same "correct by inspection, verify once for real before trusting in
     production" standard as everything else in this project; see section
     0 below for exactly what that leaves open.
7. **31 → 37 tests passing** (6 new: 5 rate-limiter unit tests, 1
   pagination test), all offline, ~0.35s.

---

## 0. Industry-grade checklist (new this session)

A concrete definition of "done," so progress can be reported as "X/Y
items" rather than a feeling. Rough weight reflects effort, not
importance — a fair complexity estimate, not a formal points system.

| # | Item | Status | Weight |
|---|---|---|---|
| 1 | Core conversation engine (RAG, orchestrator, qualification, booking, escalation) | ✅ Done, tested | 15 |
| 2 | FastAPI web layer over the engine | ✅ Done, tested (stub + real-FastAPI-ready) | 10 |
| 3 | Admin dashboard (leads/conversations/appointments, transcripts) | ✅ Done | 8 |
| 4 | Admin auth (Basic auth on `/api/admin/*`) | ✅ Done | 4 |
| 5 | Pagination on admin endpoints + dashboard UI | ✅ Done this session | 4 |
| 6 | Structured logging + request correlation IDs | ✅ Done this session | 4 |
| 7 | Rate limiting on the public message endpoint | ✅ Done this session | 4 |
| 8 | Dockerfile + docker-compose scaffolding | ✅ Written this session — **unverified, no Docker daemon available** | 6 |
| 9 | CI pipeline (GitHub Actions) | ✅ Written this session — **unverified, never run on a real runner** | 5 |
| 10 | Real embeddings + ChromaDB (replacing TF-IDF) | ❌ Not started | 8 |
| 11 | Real Postgres (replacing sqlite) | ❌ Not started (schema-compatible, upgrade path documented) | 6 |
| 12 | Real Redis (replacing in-memory state/rate-limit buckets) | ❌ Not started | 4 |
| 13 | Real Anthropic/Airtable/Slack calls, tested end-to-end with real keys | ❌ Written, never executed (no network + no keys supplied in any build session) | 6 |
| 14 | HTTPS / reverse proxy in front of admin auth | ❌ Not started | 3 |
| 15 | `GET /api/admin/stats` (global counts, not page-scoped) | ✅ Done this session | 2 |
| 16 | Dashboard in-place lead editing | ✅ Done this session | 3 |
| 17 | Actually opened the dashboard/widget in a real browser | ❌ Still structurally impossible in every sandbox so far | 2 |
| 18 | WhatsApp adapter (Phase 2) | ❌ Out of scope per doc, not started | 6 |
| 19 | Voice adapter (Phase 3) | ❌ Out of scope per doc, not started | 6 |

**Completed weight: 65 / 106 ≈ 61%** of the full list above (or **65 / 94 ≈
69%** if you exclude Phase 2/3 channel adapters (items 18–19), which the
original architecture doc explicitly scoped as later phases, not part of
the Phase 1 MVP bar). Session 5 reported this as "60/102 ≈ 59%"; the
denominator here is the actual sum of the weight column (106, not 102),
and the numerator now includes this session's two completed items (15, 16).

The single biggest lever left, by weight, is items 10–13 (real
embeddings/DB/state/external-API calls) — all four are blocked on the same
thing: **network access in a build sandbox, or you running them yourself
locally/in CI with real credentials.** I'll flag it clearly once the
weighted total crosses 90% of whichever denominator we're tracking against
— let me know if you want Phase 2/3 counted in that denominator or treated
as separately-scoped stretch goals (my default, above, is the latter).

---

## Updated "what remains" list (highest priority first, session 6)

1. **Run the Docker build and docker-compose stack for real** — the
   single highest-value unverified item, same "structurally impossible
   here, first priority wherever you run this next" status the dashboard
   browser-check has had since session 2.
2. **Actually open `frontend/admin/index.html` and `frontend/widget/index.html`
   in a real browser** — this session's visual rebuild has been checked by
   inspection (valid HTML/JS, every `getElementById` target exists, `node
   --check` on all script content) but, same as every prior session, this
   sandbox has no browser binary to load and screenshot it in. Treat
   layout/spacing edge cases (very long lead names, very small viewports)
   as possible until someone does this once.
3. **Swap TF-IDF → ChromaDB + real embeddings** — biggest remaining
   quality lever for actual answer relevance; needs network to install
   `chromadb`/call an embeddings API.
4. **Swap sqlite → Postgres, in-memory → Redis** — mechanical given the
   documented upgrade path, but needs both packages and a real
   Postgres/Redis instance to verify against.
5. **Supply real credentials and test the Anthropic/Airtable/Slack paths
   end-to-end** — code is written and has been since session 1; nobody has
   run it against the real services yet.
6. **HTTPS in front of admin auth** before deploying anywhere non-local.
7. WhatsApp (Phase 2) / Voice (Phase 3) — explicitly later phases per the
   doc; channel-agnostic schema already supports adding them without
   touching the orchestrator.

---

<details>
<summary>Session 4 checkpoint (superseded above, kept for history)</summary>

# AI LeadFlow — Progress Checkpoint (session 4)

**This checkpoint:** picked up from session 3. Session 3's updated
"what remains" list had item 3 (open the dashboard in a real browser) as
blocked — no browser binary in this sandbox either, confirmed again — so
this session did the next item: **Basic auth on `/api/admin/*`**.

## Session 4 summary — what changed this session

1. **HTTP Basic auth on every `/api/admin/*` route.** New
   `verify_admin` dependency in `app/main.py` (via `fastapi.Depends` +
   `fastapi.security.HTTPBasic`), applied as `dependencies=[Depends(verify_admin)]`
   on `GET/POST /api/admin/leads`, `GET /api/admin/leads/{id}`,
   `GET /api/admin/conversations`, and `GET /api/admin/appointments`.
   Deliberately **not** applied to `/api/message`, `/api/conversations/{id}`,
   or `/api/tools/*` — those are channel-facing / orchestrator-internal and
   gating them would break the chat widget and the LLM's own function-calling
   loop (see the doc's own scoping of "admin endpoints" to Phase 1.5).
   Credentials come from `config.ADMIN_USERNAME`/`ADMIN_PASSWORD`
   (env-configurable via `AI_LEADFLOW_ADMIN_USERNAME`/`_PASSWORD`, defaulting
   to `admin`/`admin` so the dashboard still works with zero setup for a
   local demo), compared with `secrets.compare_digest` to avoid a timing
   side-channel. `GET /health` gained an `admin_auth_mode` field
   (`"default_credentials"` vs `"configured"`) — same idiom as the existing
   `llm_mode`/`calendar_mode`/`crm_mode` fields, so it's visible in one curl
   call rather than needing to check env vars by hand.
2. **Extended the offline stub (`tests/_offline_fastapi_stub.py`) to support
   `Depends`/`HTTPBasic`/`HTTPBasicCredentials`**, plus header support in
   `StubTestClient`. This matters because the whole design of that stub is
   "the exact same test file runs against real FastAPI or the stub with zero
   changes" — without this, adding auth would have silently broken that
   guarantee the next time someone runs this in a no-network sandbox. Also
   wired up the real `python-dotenv` load call in `app/main.py` (imported in
   `requirements.txt` since session 1 but never actually called — a latent
   gap noticed while touching this same env-var-loading path).
3. **6 new tests** in `tests/test_api.py::TestAdminAuth`: every admin route
   requires auth, rejects wrong credentials, accepts correct ones, `POST
   /api/admin/leads` specifically requires auth, and non-admin routes
   (`/api/message`) still work with none. Existing admin tests updated to
   send the auth header. **31/31 tests pass**, confirmed in *both* modes this
   session — real FastAPI (default here) and the offline stub, the latter by
   deliberately blocking the real `fastapi` import mid-run and re-running
   the full suite to prove the stub path still works now that it has real
   logic behind `Depends`/`HTTPBasic`, not just an import shim.
4. **Live-server verification, both directions:** started `uvicorn` for
   real and used `curl -u` to confirm an admin route returns 401 with no
   auth, 401 with the wrong password, and 200 with `admin:admin`, and that
   `/api/message` still works with **no** auth header at all. Separately,
   used Node's built-in `fetch` to replay the dashboard's exact new
   auth-header-bearing calls (`loadLeads`/`loadConversations`/
   `loadAppointments`, the add-lead form's `POST`, and the wrong-password
   error path) against the same live server and confirmed each response
   shape matches what the updated JS reads.
5. **Dashboard (`frontend/admin/index.html`):** added **User**/**Pass**
   fields next to the API base input (defaulting to `admin`/`admin`,
   persisted to `localStorage` alongside the API base — see the new "Auth"
   section in `frontend/admin/README.md` for the plaintext-storage caveat),
   sends `Authorization: Basic ...` on every `apiFetch` call, and surfaces a
   401 as a clear "check your credentials" message in the status banner
   instead of a silent empty table.

**Still not done: opening the dashboard in an actual browser** (unchanged
from session 3 — no browser binary available here). Verification continues
to use the same two-pronged approach: JS syntax check + scripted calls that
replay the dashboard's exact request/response shapes against the live
server.

---

## Updated "what remains" list

1. **Open `frontend/admin/index.html` and `frontend/widget/index.html` in an
   actual browser** — still the single highest-value next step; still
   structurally impossible in every sandbox this project has been built in
   so far.
2. **Docker Compose / deployment config** — not started; can now actually be
   attempted, since sessions 3–4 both had network access.
3. **WhatsApp (Phase 2) / Voice (Phase 3) adapters** — still explicitly out
   of scope per the doc.
4. **Real embeddings / ChromaDB swap** — see "Swapping in the original
   stack" in the session-2 details below; unchanged.
5. **Dashboard follow-ups** — pagination, in-place lead editing (see
   `frontend/admin/README.md`).
6. **Real Anthropic/Airtable/Slack calls** — could now be tested end-to-end
   given network access, if API keys are supplied — nobody has done this yet
   since none were provided.
7. **HTTPS in front of the admin auth** — Basic auth is only as safe as the
   transport it rides on; see the new "Auth" section in
   `frontend/admin/README.md`.

---

<details>
<summary>Session 3 checkpoint (superseded above, kept for history)</summary>

# AI LeadFlow — Progress Checkpoint (session 3)

**This checkpoint:** picked up from the session-2 checkpoint. Session 2's
own top-priority item ("run it for real once, with network") was **this
session's environment**, so that's what got done, plus the next-highest
item on session 2's list (the appointments read path).

**Build environment note — this changed:** unlike sessions 1 and 2, *this*
sandbox **does have network access** to PyPI. That resolved session 2's
single biggest open risk. Everything below marked "✅ verified against real
FastAPI" or "✅ verified against the live server" is now genuinely
confirmed, not just logic-checked through the offline stub.

## Session 3 summary — what changed this session

1. **Installed the real stack** (`fastapi==0.115.0`, `uvicorn[standard]`,
   `pydantic==2.9.2`, `httpx==0.27.2`) and re-ran the full suite.
   `tests/test_api.py` auto-detected real `fastapi` and switched off the
   offline stub automatically (no code change needed — this was the whole
   point of how session 2 wrote the import fallback). **26/26 tests pass
   against real FastAPI**, up from 25 (see item 2).
2. **Added `GET /api/admin/appointments`** — the top item on session 2's
   "what remains to be built" list. `appointments` existed in the DB and
   `calendar_tool.py` already wrote to it, but there was no admin read path.
   Added `Database.list_appointments()` (joins `appointments` → `leads` so
   `lead_name`/`lead_phone`/`conversation_id` come back inline, no per-row
   lookup) and the route in `app/main.py`. One new test in
   `tests/test_api.py::TestAdminEndpoints` books a real appointment through
   the tool endpoints and asserts every joined field.
3. **Added an Appointments table to `frontend/admin/index.html`** — new
   section, new stat card, new status badge styles (pending/confirmed/
   cancelled), wired into the existing `refreshAll()`/click-through-to-
   transcript pattern used by the other two tables. `frontend/admin/README.md`
   updated to mark this item done.
4. **Actually ran the live server and hit it** — `uvicorn app.main:app`,
   then real `curl` calls (not the test client) against `/health`,
   `/api/message`, `/api/tools/calendar/check-availability`,
   `/api/admin/leads` (including the session-2 FK-fix path), and the new
   `/api/admin/appointments` — booked a real appointment end-to-end and
   confirmed the response has exactly the fields the new dashboard JS reads.
   This is genuinely the first time in this project's history that the app
   has been started and hit with a real HTTP client outside a test harness.

**Still not done: opening the dashboard in an actual browser.** No browser
binary is available in this sandbox either (checked: no chromium, no
playwright). Verification here used the same approach as session 2 —
`node -c` on the extracted `<script>` block for JS syntax, plus scripted
calls against the live server confirming every field the new table's JS
reads is present with the right shape — but nobody has visually opened this
page. That remains the single highest-value next step (see section 3).

---

<details>
<summary>Session 2 checkpoint (superseded above, kept for history)</summary>

# AI LeadFlow — Progress Checkpoint (session 2)

**This checkpoint:** picked up from the session-1 checkpoint (core engine
complete + tested, FastAPI layer written but never executed). This session's
scope, as requested: **build the admin dashboard, and test the FastAPI
layer.** Both are done. Stopping here proactively — same reasoning as
before — to guarantee a working, downloadable artifact.

**Build environment note (unchanged from session 1):** this sandbox still
has **no network access** (`pip install` fails — confirmed again this
session, see below). `fastapi`/`uvicorn`/`pydantic` still aren't installed
here. That mattered a lot more this session, since "test the FastAPI layer"
is exactly the thing that needs those packages — see section 1 for how
that was handled.

---

## 1. What was done this session ✅

### `tests/test_api.py` — the FastAPI layer is now actually tested
13 new tests exercise **every** route in `app/main.py`: `/health`,
`/api/message` (success, empty-text 400, default-channel),
`/api/conversations/{id}` (found + 404), `/api/admin/leads` (list, create,
get-by-id, 404), `/api/admin/conversations`, and all three
`/api/tools/*` endpoints including the double-booking 409 path.

**The problem:** these need `fastapi`/`pydantic` to import `app/main.py` at
all, and this sandbox can't install them (verified again — see "Build
environment note").

**The solution:** `tests/_offline_fastapi_stub.py` — a small, stdlib-only
reimplementation of just the slice of FastAPI + Pydantic that `main.py`
actually uses (`FastAPI`, `HTTPException`, `CORSMiddleware`, `BaseModel`,
and a `TestClient`-alike that does real path-template routing and calls the
real route handler functions). `tests/test_api.py` tries
`from fastapi.testclient import TestClient` first and **only** falls back to
the stub on `ImportError`. So:
- Right now, in this sandbox: the stub runs, and it's genuinely exercising
  `app/main.py`'s actual route functions, actual `Orchestrator`, actual
  sqlite DB — not a mock of the app, a real run of it through a minimal
  transport layer.
- The moment you `pip install -r requirements.txt` somewhere with network:
  the exact same test file switches to the real `fastapi.testclient`
  automatically, no code changes. That's a strictly stronger check (real
  Pydantic validation/coercion, real Starlette routing, etc.) — the stub's
  docstring is explicit that it does *not* validate those things.

Run it: `python3 -m unittest tests.test_api -v` (prints which mode it ran
in). Full suite: `python3 -m unittest discover -s tests -v` →
**25/25 passing** (12 from session 1 + 13 new).

### A real bug this found and fixed
`POST /api/admin/leads` and `POST /api/tools/crm/push-lead` both call
`db.upsert_lead(conversation_id, ...)`, and `leads.conversation_id` is a
`FOREIGN KEY` into `conversations`. That's fine when a lead comes from the
normal chat flow (the orchestrator always creates the conversation row
first) but breaks immediately — `sqlite3.IntegrityError` — for a lead
created any other way with a `conversation_id` that doesn't exist yet
(exactly what the admin dashboard's "add lead manually" form does, and
exactly what `test_book_then_double_book_conflicts` /
`test_push_lead_to_crm` hit). Fixed both endpoints to call
`db.get_or_create_conversation(conversation_id, "manual")` first
(idempotent, so it's a no-op for a `conversation_id` that already exists).
See `frontend/admin/README.md` for more detail on this one.

### Admin dashboard (`frontend/admin/index.html`)
Single self-contained static HTML file — same dependency-free approach as
the chat widget (no npm, no build step, no framework, inline CSS/JS). Talks
to the already-existing `/api/admin/*` and `/api/conversations/{id}`
endpoints:
- Leads table + conversations table, both click-through to a transcript
  modal (`GET /api/conversations/{id}`)
- A form to manually add a lead (the thing that surfaced the bug above)
- Summary stat cards (total leads, total conversations, escalated, booked)
- A configurable, `localStorage`-persisted API base URL in the header, so
  the same file works against `localhost:8000` in dev or a real deployed
  backend later without editing anything

**Verification within this sandbox's limits:** couldn't launch an actual
browser here, so this was checked two ways instead of just "looks right":
1. `node -c` on the extracted inline `<script>` block — confirms it's
   syntactically valid JS (no runtime/DOM check, since there's no browser).
2. A scripted smoke test drove the *exact* sequence of calls the dashboard
   makes (`POST /api/message` → `POST /api/admin/leads` →
   `GET /api/admin/leads` → `GET /api/admin/conversations` →
   `GET /api/conversations/{id}`) through the same stub client used in
   `test_api.py`, and asserted the real response JSON has every key the
   dashboard's JS reads (`name`, `phone`, `crm_synced`, `conversation_id`
   for leads; `id`, `channel`, `status`, `started_at`/`updated_at` for
   conversations; `conversation`/`messages` with `role`/`content`/
   `timestamp` for the transcript modal). All matched. **Still genuinely
   unverified: what it looks like/behaves like in an actual browser** — do
   that before trusting it for a real demo (see "How to run" below).

See `frontend/admin/README.md` for what's *not* done yet (no auth, no
pagination, no in-UI lead editing, no appointments view).

### `frontend/widget/index.html`
The trivial "first 2-minute task" flagged at the end of session 1 — a bare
host page for `widget.js` so there's something clickable to open in a
browser. Points at `localhost:8000` by default.

---

## 2. What is partially completed 🟡

Unchanged from session 1, still true:
- **Real Anthropic API integration** (`llm_client.py`) — written, untested
  (no network here to call `api.anthropic.com`).
- **Real Airtable CRM push** (`crm_tool.py`) — written, untested.
- **Real Slack escalation** (`escalation.py`) — written, untested.
- **The FastAPI layer, in the sense of "run against real FastAPI/uvicorn"**
  — this session proved the route *logic* thoroughly via the stub, and the
  moment `fastapi` is installed the same tests become the real thing, but
  nobody has literally run `uvicorn app.main:app` and hit it with curl from
  outside a test harness yet.

## 3. What remains to be built ❌

Re-prioritized after this session:

1. **Run it for real once, with network:** `pip install -r requirements.txt`
   → `pytest tests/` (or `python3 -m unittest discover -s tests`) to
   confirm `test_api.py` passes against the *real* FastAPI TestClient, not
   just the stub → then `uvicorn app.main:app --reload` and open
   `frontend/admin/index.html` and `frontend/widget/index.html` in an
   actual browser. This is now the single highest-value next step — it's
   the one thing that genuinely cannot be done inside this sandbox.
2. **Auth on `/api/admin/*`** — still wide open; the dashboard has no login
   and neither does the API. Basic auth (per the doc, Week 8) is a
   reasonable minimum bar before this goes anywhere non-local.
3. **`GET /api/admin/appointments`** (new) + a bookings view in the
   dashboard — appointments exist in the DB and calendar tool but have no
   admin-facing read path at all yet.
4. **Docker Compose / deployment config** — not started (same as session
   1; needs network to verify against Render/Railway/Vercel).
5. **WhatsApp (Phase 2) / Voice (Phase 3) adapters** — still explicitly out
   of scope per the doc; channel-agnostic schema already supports adding
   them without touching the orchestrator.
6. **Real embeddings / ChromaDB swap** — unchanged from session 1, see
   "Swapping in the original stack" there for the plug-in points (same
   file layout, nothing this session changed there).
7. **Dashboard follow-ups** — pagination, in-place lead editing, listed in
   detail in `frontend/admin/README.md`.

---

## 4. How to run the current version

### Right now, zero installs, proves the whole engine + API logic offline

```bash
cd ai-leadflow
python3 -m unittest discover -s tests -v
```

25/25 tests, ~0.2s, fully offline — this now includes the FastAPI route
logic (via the stub client when `fastapi` isn't installed, or the real
`fastapi.testclient` automatically once it is — see section 1).

### To actually see the app running (needs network to pip install)

```bash
cd ai-leadflow
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Then, in a second terminal:

```bash
python3 -m http.server 8080 --directory frontend/admin
```

Open `http://localhost:8080` for the admin dashboard, or open
`frontend/widget/index.html` directly for the chat widget demo.

Or hit the API directly:

```bash
curl -X POST http://localhost:8000/api/message \
  -H "Content-Type: application/json" \
  -d '{"session_id":"demo-1","channel":"web","text":"What are your working hours?"}'
```

### To use the real Claude API instead of mock mode

Unchanged from session 1 — `export ANTHROPIC_API_KEY=sk-ant-...` before
starting uvicorn; `GET /health` will show `"llm_mode": "anthropic"`. Still
untested end-to-end (no network in this sandbox to verify).

---

## 5. Known bugs/issues

### Fixed this session
1. `POST /api/admin/leads` and `POST /api/tools/crm/push-lead` — FK
   constraint failure on any `conversation_id` without a prior chat
   conversation. Details in section 1 above and in
   `frontend/admin/README.md`.

### Fixed in session 1 (unchanged, still holding)
TF-IDF stopword filtering, heading-aware chunk indexing, out-of-scope
escalation threshold, `actions_taken` merging for escalation branches,
mid-booking stage routing, and a name-extraction regex case-sensitivity
scoping bug. See git history / the previous checkpoint for full details —
all still covered by the passing test suite.

### Known limitations (not bugs, carried over from session 1, still true)
- TF-IDF retrieval is keyword-based, not semantic (paraphrase-sensitive).
- Booking slot picking only accepts a numeric choice, not natural language.
- No auth anywhere, including `/api/admin/*` and the new dashboard.
- Single-process, single-file sqlite.
- The real Anthropic/Airtable/Slack integration paths are written but
  unexecuted (no network in any build sandbox so far).

### New limitation from this session
- The admin dashboard has been logic-verified (JSON shapes match exactly,
  JS is syntactically valid) but **not** actually opened in a browser —
  see section 3, item 1. Treat visual/UX issues as possible until someone
  does that once.

---

## 6. Next recommended implementation steps

1. **Run it for real** (network required): `pip install -r
   requirements.txt`, run the full test suite again (confirms the same
   `tests/test_api.py` now runs against real FastAPI), start uvicorn, and
   actually click through `frontend/admin/index.html` and
   `frontend/widget/index.html` in a browser. This is the one thing this
   session structurally could not do.
2. Add `GET /api/admin/appointments` + a bookings table in the dashboard.
3. Add basic auth to `/api/admin/*` (and gate the dashboard behind it)
   before deploying anywhere non-local.
4. Set `ANTHROPIC_API_KEY` and verify the real LLM path end-to-end.
5. If/when better FAQ recall matters, swap `vector_store.py` for ChromaDB +
   real embeddings — interface points documented below.
6. Docker Compose / actual deployment (Render/Railway + Vercel per the doc).
7. Dashboard follow-ups from `frontend/admin/README.md`: pagination,
   in-place lead editing.

### Swapping in the original stack (ChromaDB / Postgres / Redis)
Unchanged from session 1:
- `vector_store.SimpleVectorStore.add_documents()` / `.query()` →
  replace internals with a `chromadb.PersistentClient` collection +
  OpenAI/Voyage embeddings calls; `rag_engine.py` doesn't need to change.
- `database.Database` → replace `sqlite3` connection with a
  SQLAlchemy engine pointed at Postgres, keep the same table/column names
  and the same method signatures; nothing else in the codebase touches
  sqlite directly.
- `state_store.InMemoryStateStore` → replace with a thin `redis-py`
  wrapper exposing the same `get`/`set`/`update`/`delete` methods.

---

## 7. Deviations from the original documentation (summary)

Unchanged from session 1, plus two new rows:

| Doc said | Built instead | Why |
|---|---|---|
| PostgreSQL | sqlite3 (stdlib) | No network to install psycopg2/run a DB server; same schema, trivial upgrade path |
| Redis | In-memory dict | Same reason; single-process demo doesn't need it yet |
| ChromaDB + OpenAI/Voyage embeddings | Pure-Python TF-IDF | No network to install/call either; fully offline and testable now |
| Anthropic/OpenAI SDK | Direct `urllib` HTTPS calls + MockLLM fallback | No network to install the SDK; MockLLM makes the whole app demoable with zero API keys |
| React embeddable widget | Vanilla JS, single `<script>` tag | No npm/network access to set up a build step; same embed UX, zero build tooling |
| `fastapi.testclient.TestClient` for `tests/test_api.py` | Same test file, with an offline stdlib stub as automatic fallback | No network to install `fastapi`/`httpx`; the stub is dropped in favor of the real thing automatically once they're installed |
| React admin dashboard (implied by doc's general frontend stack) | Vanilla JS static HTML, same as the widget | Consistency with the widget's zero-build-step approach; no reason to introduce a framework for two tables and a modal |

All of these are reversible — see "Swapping in the original stack" above.

</details>

---

## Session 3 — updated "what remains" list

1. ~~Run it for real once, with network~~ — **done this session**, see
   summary above.
2. ~~`GET /api/admin/appointments` + bookings view~~ — **done this session**,
   see summary above.
3. **Open `frontend/admin/index.html` and `frontend/widget/index.html` in an
   actual browser** — now the single highest-value next step; still
   structurally impossible in this sandbox (no browser binary available).
4. **Auth on `/api/admin/*`** — still wide open; still the right minimum bar
   before deploying anywhere non-local.
5. **Docker Compose / deployment config** — not started; can now actually be
   attempted, since this sandbox has network (unlike sessions 1–2).
6. **WhatsApp (Phase 2) / Voice (Phase 3) adapters** — still explicitly out
   of scope per the doc.
7. **Real embeddings / ChromaDB swap** — see "Swapping in the original
   stack" in the session-2 details above; unchanged.
8. **Dashboard follow-ups** — pagination, in-place lead editing (see
   `frontend/admin/README.md`).
9. **Real Anthropic/Airtable/Slack calls** — now *could* be tested end-to-end
   given network access, if API keys are supplied (`ANTHROPIC_API_KEY`,
   etc.) — nobody has done this yet this session since none were provided.

</details>

</details>
