"""
Semantic vector store for RAG — ChromaDB + embeddings.

Session 6 upgrade: this used to be a pure-Python TF-IDF + cosine-similarity
retriever (see vector_store.py.orig.bak). TF-IDF only matches on shared
words, so paraphrases like "When does the clinic operate?" vs "What are
your working hours?" could fail to match even though they mean the same
thing. This version chunks -> embeds (via EmbeddingsClient) -> stores in a
persistent Chroma collection -> queries by real vector similarity, per the
project's own documented upgrade path.

Interface is unchanged from the TF-IDF version (add_documents / query /
clear / len), so rag_engine.py only needed to change how it reads the
similarity score (Chroma returns a cosine *distance*; we convert to a
similarity score of 1 - distance so "higher is more relevant" still holds).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import chromadb

from .embeddings_client import EmbeddingsClient

_WORD_RE = re.compile(r"[a-zA-Z0-9']+")

DEFAULT_CHROMA_DIR = os.environ.get("AI_LEADFLOW_CHROMA_DIR", "chroma_data")
DEFAULT_COLLECTION_NAME = os.environ.get("AI_LEADFLOW_CHROMA_COLLECTION", "leadflow_kb")


def tokenize(text: str) -> list[str]:
    """Kept for callers (rag_engine.py's shared-term sanity check) that
    still want a lightweight word list; no longer used for scoring."""
    return [w.lower() for w in _WORD_RE.findall(text) if len(w) > 1]


@dataclass
class Chunk:
    id: str
    text: str
    metadata: dict = field(default_factory=dict)


class ChromaVectorStore:
    """
    Persistent, embedding-based vector store.

    - add_documents(chunks): embeds and upserts a list of Chunk objects
    - query(text, top_k): returns [(Chunk, score), ...] sorted by cosine
      similarity, highest first (score in roughly [-1, 1], higher = closer)
    """

    def __init__(
        self,
        embeddings: EmbeddingsClient | None = None,
        persist_dir: str = DEFAULT_CHROMA_DIR,
        collection_name: str = DEFAULT_COLLECTION_NAME,
    ):
        self.embeddings = embeddings or EmbeddingsClient()
        self._client = chromadb.PersistentClient(path=persist_dir)
        # cosine space + fresh collection per (persist_dir, embedding mode):
        # mock and real embeddings are NOT comparable vectors, so mixing
        # them in one collection would silently produce garbage similarity
        # scores. Namespacing the collection by embedding mode avoids that.
        self._collection_name = f"{collection_name}__{self.embeddings.mode}"
        self._collection = self._client.get_or_create_collection(
            name=self._collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        self._chunk_cache: dict[str, Chunk] = {}

    def add_documents(self, chunks: list[Chunk]) -> None:
        if not chunks:
            return
        ids = [c.id for c in chunks]
        texts = [c.text for c in chunks]
        # Chroma rejects empty metadata dicts, so chunks with no metadata
        # get a harmless placeholder key rather than failing ingestion.
        metadatas = [dict(c.metadata) or {"_chunk": True} for c in chunks]
        vectors = self.embeddings.embed_many(texts)
        # upsert (not add): re-ingesting the same knowledge base — e.g. on
        # every app restart — must overwrite, not duplicate, existing chunks.
        self._collection.upsert(ids=ids, embeddings=vectors, documents=texts, metadatas=metadatas)
        for c in chunks:
            self._chunk_cache[c.id] = c

    def clear(self) -> None:
        self._client.delete_collection(self._collection_name)
        self._collection = self._client.get_or_create_collection(
            name=self._collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        self._chunk_cache.clear()

    def query(self, text: str, top_k: int = 3) -> list[tuple[Chunk, float]]:
        if self._collection.count() == 0:
            return []
        qvec = self.embeddings.embed_one(text)
        result = self._collection.query(
            query_embeddings=[qvec],
            n_results=min(top_k, self._collection.count()),
            include=["documents", "metadatas", "distances"],
        )
        out: list[tuple[Chunk, float]] = []
        ids = result["ids"][0]
        docs = result["documents"][0]
        metas = result["metadatas"][0]
        dists = result["distances"][0]
        for cid, doc, meta, dist in zip(ids, docs, metas, dists):
            chunk = self._chunk_cache.get(cid) or Chunk(id=cid, text=doc, metadata=meta or {})
            similarity = 1.0 - dist  # cosine distance -> cosine similarity
            out.append((chunk, similarity))
        return out

    def __len__(self) -> int:
        return self._collection.count()
