"""
RAG Engine: retrieval -> grounded LLM answer, with the non-negotiable rule
from the design doc: never fabricate facts. If retrieval finds nothing
relevant enough, the engine returns grounded=False and the orchestrator
routes to escalation (Section 5.1: "RAG retrieval returns no relevant chunks").

Session 6 upgrade: retrieval now comes from ChromaVectorStore (real
embeddings), not TF-IDF. The old version required a minimum number of
literally-shared words between the question and the retrieved chunk, as a
defense against spurious single-word matches in the TF-IDF store. That
check is dropped here: with real semantic embeddings, the whole point is
that a relevant chunk can share *zero* exact words with the question (e.g.
"When does the clinic operate?" vs. a chunk titled "Working Hours"), so a
shared-word requirement would throw away exactly the matches the upgrade
was for. Relevance is now gated purely on the cosine-similarity score.
"""
from __future__ import annotations

from .llm_client import LLMClient
from .vector_store import ChromaVectorStore

# Cosine similarity threshold below which we treat retrieval as "no match".
# Real embedding models (e.g. text-embedding-3-small) typically put a
# genuinely relevant chunk well above 0.3 for short FAQ-style knowledge
# bases; unrelated chunks usually sit below 0.15. The mock hashing embedder
# (see embeddings_client.py) produces lower absolute scores even for good
# matches, so this threshold is intentionally conservative rather than tuned
# tightly to one embedding backend.
MIN_RELEVANCE_SCORE = 0.15


class RAGEngine:
    def __init__(self, vector_store: ChromaVectorStore, llm: LLMClient, top_k: int = 3):
        self.vector_store = vector_store
        self.llm = llm
        self.top_k = top_k

    def answer(self, question: str) -> dict:
        """
        Returns:
          {
            "answer": str,
            "grounded": bool,
            "sources": [{"source": ..., "section": ..., "score": float}, ...]
          }
        """
        results = self.vector_store.query(question, top_k=self.top_k)
        relevant = [(chunk, score) for chunk, score in results if score >= MIN_RELEVANCE_SCORE]

        if not relevant:
            return {
                "answer": "I don't have that information in our knowledge base. Let me connect you with a team member who can help.",
                "grounded": False,
                "sources": [],
            }

        context_texts = [c.text for c, _ in relevant]
        result = self.llm.grounded_answer(question, context_texts)

        sources = [
            {
                "source": c.metadata.get("source"),
                "section": c.metadata.get("section"),
                "score": round(score, 4),
            }
            for c, score in relevant
        ]
        return {
            "answer": result["answer"],
            "grounded": result.get("grounded", True),
            "sources": sources,
        }
