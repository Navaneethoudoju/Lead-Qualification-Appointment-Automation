"""
Embeddings client abstraction — used to turn knowledge-base chunks and user
questions into vectors for the semantic RAG upgrade (session 6, replacing
the TF-IDF retriever in vector_store.py.orig.bak).

Three backends, in priority order:

1. **MiniLM (local, free, default once installed)** — `sentence-transformers`
   running `all-MiniLM-L6-v2` locally. This is the project's documented
   "must complete" embeddings choice: no API key, no per-call cost, no
   network at *query* time (only once, to download the ~80MB model weights
   the first time it runs). 384-dimensional, real semantic similarity
   (paraphrases like "when does the clinic operate" and "what are your
   working hours" score highly even though they share almost no words).
   Selected automatically whenever `sentence-transformers` is importable,
   unless `AI_LEADFLOW_EMBEDDINGS_BACKEND` explicitly picks something else.
2. **OpenAI** (`text-embedding-3-small`, over HTTPS via stdlib `urllib` —
   same no-SDK-dependency approach as llm_client.py's Anthropic calls) —
   used if `OPENAI_API_KEY` is set and MiniLM isn't available/selected.
   1536-dimensional. Kept as an option for anyone who'd rather not host the
   model locally (e.g. small/low-memory containers).
3. **Mock** — deterministic, offline, hashing-based bag-of-words vectorizer
   (stdlib only, no network, no model download). Automatic fallback when
   neither of the above is available, so the whole platform — including the
   test suite — stays runnable with zero external dependencies. It will NOT
   catch synonym/paraphrase matches the way real embeddings do; that's the
   actual capability MiniLM/OpenAI add.

All three implement the same interface (embed_one / embed_many), so
vector_store.py never needs to know which one is active — it just reads
`.mode` for collection namespacing (see vector_store.py) so mock, MiniLM,
and OpenAI vectors (different dimensions, not comparable) never get mixed
in one Chroma collection.

`sentence-transformers` isn't in requirements.txt's *unconditional* install
list (it pulls in `torch`, a large dependency) — see requirements.txt for
the exact install line. Import is lazy (inside `_load_minilm`) so this
module — and everything that imports it — still loads fine without the
package installed; it just falls through to OpenAI/mock.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.error
import urllib.request
from typing import Optional

OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"
OPENAI_MODEL = "text-embedding-3-small"
MINILM_MODEL_NAME = "all-MiniLM-L6-v2"
MOCK_DIMENSIONS = 256

_WORD_RE = re.compile(r"[a-zA-Z0-9']+")

# Same idea as the old TF-IDF store's stoplist: without filtering these out,
# short common words dominate the hashed vector purely because they're
# frequent, drowning out the actual content words (e.g. "root canal cost"
# vs. "what does the clinic charge"). Kept only for the offline mock
# embedder — real embedding models handle this natively.
_STOPWORDS = {
    "a", "an", "the", "is", "are", "am", "was", "were", "be", "been", "being",
    "i", "you", "he", "she", "it", "we", "they", "me", "him", "her", "us", "them",
    "my", "your", "his", "its", "our", "their", "mine", "yours", "ours", "theirs",
    "this", "that", "these", "those",
    "and", "or", "but", "if", "so", "because", "as", "of", "at", "by", "for",
    "with", "about", "against", "between", "into", "through", "during",
    "before", "after", "above", "below", "to", "from", "up", "down", "in",
    "out", "on", "off", "over", "under", "again", "further", "then", "once",
    "do", "does", "did", "doing", "done",
    "can", "could", "will", "would", "shall", "should", "may", "might", "must",
    "what's", "who's", "what", "which", "who", "whom", "when", "where", "why", "how",
    "not", "no", "nor", "too", "very", "just", "there",
    "have", "has", "had", "having",
    "get", "got", "please", "thanks", "thank", "hi", "hello",
}


class EmbeddingsError(Exception):
    pass


# Model registry keyed by mode name, used by _load_minilm's caller to decide
# whether sentence-transformers is even worth importing.
_VALID_BACKENDS = {"minilm", "openai", "mock"}


class EmbeddingsClient:
    """Public interface used by vector_store.py.

    Backend selection (see module docstring for the full rationale):
      1. `AI_LEADFLOW_EMBEDDINGS_BACKEND` env var, if set, forces one of
         "minilm" / "openai" / "mock" — raises EmbeddingsError immediately
         if that backend's dependency/credential isn't actually available,
         rather than silently falling back to something the caller didn't
         ask for.
      2. Otherwise: MiniLM if `sentence-transformers` is importable, else
         OpenAI if `OPENAI_API_KEY` is set, else mock.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        openai_model: str = OPENAI_MODEL,
        minilm_model: str = MINILM_MODEL_NAME,
    ):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.openai_model = openai_model
        self.minilm_model_name = minilm_model
        self._mock = MockEmbedder(dimensions=MOCK_DIMENSIONS)
        self._minilm_model = None  # lazy-loaded SentenceTransformer instance

        forced = os.environ.get("AI_LEADFLOW_EMBEDDINGS_BACKEND")
        if forced:
            if forced not in _VALID_BACKENDS:
                raise EmbeddingsError(
                    f"AI_LEADFLOW_EMBEDDINGS_BACKEND={forced!r} is not one of {sorted(_VALID_BACKENDS)}"
                )
            if forced == "minilm":
                self._minilm_model = self._load_minilm()  # raises if unavailable
                self.mode = "minilm"
            elif forced == "openai":
                if not self.api_key:
                    raise EmbeddingsError("AI_LEADFLOW_EMBEDDINGS_BACKEND=openai but OPENAI_API_KEY is not set")
                self.mode = "openai"
            else:
                self.mode = "mock"
            return

        try:
            self._minilm_model = self._load_minilm()
            self.mode = "minilm"
        except EmbeddingsError:
            self.mode = "openai" if self.api_key else "mock"

    def embed_one(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self.mode == "minilm":
            return self._minilm_embed(texts)
        if self.mode == "openai":
            return self._openai_embed(texts)
        return self._mock.embed_many(texts)

    # ---- MiniLM (local, sentence-transformers) --------------------------

    def _load_minilm(self):
        """Lazily imports sentence-transformers and loads all-MiniLM-L6-v2.
        Import + model load happens here (not at module level) so this file
        — and every caller of it — still imports cleanly when
        sentence-transformers/torch aren't installed. Raises EmbeddingsError
        (never ImportError) so callers only need to catch one exception
        type regardless of *why* MiniLM isn't available (missing package,
        or the model weights failing to download because there's no
        network).
        """
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise EmbeddingsError(
                "sentence-transformers is not installed — run "
                "`pip install sentence-transformers` (see requirements.txt) "
                "to enable local MiniLM embeddings."
            ) from e
        try:
            return SentenceTransformer(self.minilm_model_name)
        except Exception as e:  # noqa: BLE001 - surfaces network/download errors uniformly
            raise EmbeddingsError(
                f"Failed to load MiniLM model {self.minilm_model_name!r} "
                f"(first run downloads ~80MB from huggingface.co — needs network "
                f"once, then it's cached locally): {e}"
            ) from e

    def _minilm_embed(self, texts: list[str]) -> list[list[float]]:
        # normalize_embeddings=True makes the output unit-length, so
        # ChromaVectorStore's cosine-distance -> similarity math (see
        # vector_store.py) works identically to the OpenAI/mock paths.
        vectors = self._minilm_model.encode(
            texts, normalize_embeddings=True, convert_to_numpy=True
        )
        return [v.tolist() for v in vectors]

    # ---- Real OpenAI API path ------------------------------------------

    def _openai_embed(self, texts: list[str]) -> list[list[float]]:
        body = json.dumps({"model": self.openai_model, "input": texts}).encode("utf-8")
        req = urllib.request.Request(
            OPENAI_EMBEDDINGS_URL,
            data=body,
            method="POST",
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise EmbeddingsError(f"OpenAI embeddings API error {e.code}: {e.read().decode('utf-8', 'ignore')}") from e
        except urllib.error.URLError as e:
            raise EmbeddingsError(f"OpenAI embeddings API unreachable: {e}") from e

        # OpenAI returns items possibly out of input order; `index` tells us
        # where each embedding belongs.
        ordered = [None] * len(texts)
        for item in payload["data"]:
            ordered[item["index"]] = item["embedding"]
        return ordered


class MockEmbedder:
    """
    Deterministic, offline, dependency-free pseudo-embedding.

    Uses the hashing trick (each token hashed into one of `dimensions`
    buckets, +1/-1 sign from a second hash bit) instead of raw TF-IDF, so
    it at least generalizes slightly better than exact-word matching for
    minor spelling/tokenization variance, while requiring no model weights
    and no network access. This is explicitly NOT a substitute for real
    semantic embeddings — see module docstring.
    """

    def __init__(self, dimensions: int = MOCK_DIMENSIONS):
        self.dimensions = dimensions

    def _tokenize(self, text: str) -> list[str]:
        return [
            w.lower()
            for w in _WORD_RE.findall(text)
            if len(w) > 1 and w.lower() not in _STOPWORDS
        ]

    def _hash_token(self, vec: list[float], tok: str, weight: float = 1.0) -> None:
        h = hashlib.sha256(tok.encode("utf-8")).digest()
        bucket = int.from_bytes(h[:4], "little") % self.dimensions
        sign = 1.0 if (h[4] & 1) == 0 else -1.0
        vec[bucket] += sign * weight

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dimensions
        tokens = self._tokenize(text)
        for tok in tokens:
            self._hash_token(vec, tok)
        # Also hash adjacent-word bigrams (e.g. "root_canal"): this lets a
        # domain phrase match as a unit, which single hashed words alone
        # can't capture, and partially compensates for this embedder having
        # no real notion of term importance the way TF-IDF or a trained
        # model would.
        for a, b in zip(tokens, tokens[1:]):
            self._hash_token(vec, f"{a}_{b}", weight=0.5)
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]
