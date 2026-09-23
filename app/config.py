"""
App-wide configuration, loaded from environment variables (optionally via a
.env file if python-dotenv is installed — see main.py).
"""
import os

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/ai_leadflow")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# Passthrough only — app/core/embeddings_client.py and app/core/vector_store.py
# read OPENAI_API_KEY / AI_LEADFLOW_CHROMA_DIR from the environment directly
# (EmbeddingsClient() / ChromaVectorStore() have their own os.environ.get
# defaults), so nothing here actually threads these into a constructor call.
# They're still exposed as config attributes so callers (e.g. /health, or
# anything wanting to report configuration without re-reading os.environ)
# have one place to look, and so .env.example has a documented name to set.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
AI_LEADFLOW_CHROMA_DIR = os.environ.get("AI_LEADFLOW_CHROMA_DIR", "chroma_data")

KB_DIR = os.environ.get("AI_LEADFLOW_KB_DIR", os.path.join(os.path.dirname(__file__), "..", "knowledge_base", "clinic"))
CORS_ALLOW_ORIGINS = os.environ.get("AI_LEADFLOW_CORS_ORIGINS", "*").split(",")

# --- Admin auth (Basic Auth on /api/admin/*) --------------------------------
# No env vars set -> falls back to admin/admin so the dashboard still works
# out of the box for a local demo. ADMIN_AUTH_IS_DEFAULT lets /health flag
# this (same idiom as llm_mode/calendar_mode/crm_mode) so it's obvious from
# one curl call, not buried in a log line, when a deploy is still running on
# the default credentials.
ADMIN_USERNAME = os.environ.get("AI_LEADFLOW_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("AI_LEADFLOW_ADMIN_PASSWORD", "admin")
ADMIN_AUTH_IS_DEFAULT = (
    os.environ.get("AI_LEADFLOW_ADMIN_USERNAME") is None
    and os.environ.get("AI_LEADFLOW_ADMIN_PASSWORD") is None
)

# --- Rate limiting (session 5, Redis-backed since session 6) -----------
# Applied to POST /api/message only — see app/core/rate_limiter.py
# (RedisRateLimiter) for the sliding-window implementation shared across
# backend replicas via REDIS_URL above.
RATE_LIMIT_MAX_REQUESTS = int(os.environ.get("AI_LEADFLOW_RATE_LIMIT_MAX_REQUESTS", "20"))
RATE_LIMIT_WINDOW_SECONDS = float(os.environ.get("AI_LEADFLOW_RATE_LIMIT_WINDOW_SECONDS", "60"))
