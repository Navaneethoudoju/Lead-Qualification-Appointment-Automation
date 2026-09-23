import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.embeddings_client import EmbeddingsClient
from app.core.ingest import build_vector_store_from_dir
from app.core.vector_store import Chunk, ChromaVectorStore

KB_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "knowledge_base", "clinic"))

# Real embeddings (OPENAI_API_KEY) demonstrate true semantic matching -
# e.g. matching "operate" to "working hours" with no shared words. The
# offline mock embedder (see embeddings_client.py) is a hashing fallback
# that does NOT do this by design; that gap is exactly what real
# embeddings are for. Tests that need real semantic matching are skipped
# without a configured key rather than pretending the mock can do it.
HAS_REAL_EMBEDDINGS = bool(os.environ.get("OPENAI_API_KEY"))


def _temp_store(**kwargs) -> tuple[ChromaVectorStore, str]:
    persist_dir = tempfile.mkdtemp(prefix="chroma_test_")
    store = ChromaVectorStore(persist_dir=persist_dir, **kwargs)
    return store, persist_dir


class TestVectorStore(unittest.TestCase):
    def setUp(self):
        self.store, self.persist_dir = _temp_store()

    def tearDown(self):
        shutil.rmtree(self.persist_dir, ignore_errors=True)

    def test_query_ranks_relevant_chunk_first(self):
        self.store.add_documents(
            [
                Chunk(id="1", text="The clinic is open Monday to Saturday, 9am to 7pm."),
                Chunk(id="2", text="Dental cleaning costs eight hundred rupees."),
                Chunk(id="3", text="We offer physiotherapy sessions and assessments."),
            ]
        )
        results = self.store.query("What days and hours is the clinic open?", top_k=1)
        self.assertEqual(results[0][0].id, "1")

    def test_empty_store_returns_nothing(self):
        self.assertEqual(self.store.query("anything"), [])

    def test_len_reflects_indexed_chunk_count(self):
        self.assertEqual(len(self.store), 0)
        self.store.add_documents([Chunk(id="1", text="hello world")])
        self.assertEqual(len(self.store), 1)

    def test_clear_empties_the_store(self):
        self.store.add_documents([Chunk(id="1", text="hello world")])
        self.store.clear()
        self.assertEqual(len(self.store), 0)
        self.assertEqual(self.store.query("hello"), [])


class TestKnowledgeBaseIngestion(unittest.TestCase):
    def setUp(self):
        self.persist_dir = tempfile.mkdtemp(prefix="chroma_kb_test_")

    def tearDown(self):
        shutil.rmtree(self.persist_dir, ignore_errors=True)

    def test_ingests_all_clinic_docs(self):
        store = build_vector_store_from_dir(KB_DIR, persist_dir=self.persist_dir)
        self.assertGreater(len(store), 10)

    def test_exact_phrase_questions_retrieve_relevant_chunks(self):
        """Cases where the question shares enough real content words with
        the target chunk that even the offline mock embedder (hashing,
        no true synonym understanding) should retrieve it correctly. This
        is the floor of what retrieval must do regardless of embedding
        backend."""
        store = build_vector_store_from_dir(KB_DIR, persist_dir=self.persist_dir)
        cases = {
            "What are your working hours?": {"faq.md", "policies.md"},
            "Do you accept insurance?": {"faq.md"},
            "What is your cancellation policy?": {"faq.md", "policies.md"},
            "Where is the clinic located?": {"about.md", "faq.md", "policies.md", "services.md"},
        }
        for question, acceptable_sources in cases.items():
            results = store.query(question, top_k=1)
            self.assertTrue(results, f"No results for: {question}")
            self.assertIn(
                results[0][0].metadata["source"],
                acceptable_sources,
                f"Unexpected source for '{question}': got {results[0][0].metadata['source']}",
            )


@unittest.skipUnless(
    HAS_REAL_EMBEDDINGS,
    "Requires OPENAI_API_KEY - true synonym/paraphrase matching needs a real embedding model, "
    "not the offline mock. Set OPENAI_API_KEY to run this in CI against the real API.",
)
class TestSemanticParaphraseMatching(unittest.TestCase):
    """The actual capability session 6 added: matching paraphrases that
    share no exact words, which TF-IDF (and the offline mock embedder)
    cannot do."""

    def setUp(self):
        self.persist_dir = tempfile.mkdtemp(prefix="chroma_semantic_test_")

    def tearDown(self):
        shutil.rmtree(self.persist_dir, ignore_errors=True)

    def test_paraphrased_question_matches_working_hours(self):
        store = build_vector_store_from_dir(
            KB_DIR, embeddings=EmbeddingsClient(), persist_dir=self.persist_dir
        )
        results = store.query("When does the clinic operate?", top_k=1)
        self.assertTrue(results)
        self.assertEqual(results[0][0].metadata["source"], "faq.md")

    def test_root_canal_cost_matches_services(self):
        store = build_vector_store_from_dir(
            KB_DIR, embeddings=EmbeddingsClient(), persist_dir=self.persist_dir
        )
        results = store.query("How much does a root canal cost?", top_k=1)
        self.assertTrue(results)
        self.assertEqual(results[0][0].metadata["source"], "services.md")


if __name__ == "__main__":
    unittest.main(verbosity=2)
