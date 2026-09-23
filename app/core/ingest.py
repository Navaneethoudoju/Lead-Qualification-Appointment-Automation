"""
Knowledge base ingestion pipeline (Section 4.1 of the architecture doc):
Load documents -> chunk -> (embed, implicitly via SimpleVectorStore) -> store.

Chunking strategy: split markdown on '##' headings first (natural semantic
boundaries for this kind of content), then further split any chunk longer
than `max_words` on paragraph breaks, keeping a small overlap between
adjacent chunks so context isn't lost at boundaries.
"""
from __future__ import annotations

import os
import re

from .embeddings_client import EmbeddingsClient
from .vector_store import Chunk, ChromaVectorStore

HEADING_RE = re.compile(r"(?m)^##\s+(.*)$")


def _split_by_heading(text: str) -> list[tuple[str, str]]:
    """Returns [(heading, section_text), ...]. Content before first '##' gets heading '_intro'."""
    matches = list(HEADING_RE.finditer(text))
    if not matches:
        return [("_full", text)]

    sections = []
    if matches[0].start() > 0:
        sections.append(("_intro", text[: matches[0].start()].strip()))

    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        heading = m.group(1).strip()
        body = text[start:end].strip()
        sections.append((heading, body))
    return sections


def _chunk_words(text: str, max_words: int = 400, overlap: int = 40) -> list[str]:
    words = text.split()
    if len(words) <= max_words:
        return [text]
    chunks = []
    start = 0
    while start < len(words):
        end = min(start + max_words, len(words))
        chunks.append(" ".join(words[start:end]))
        if end == len(words):
            break
        start = end - overlap
    return chunks


def load_and_chunk_dir(kb_dir: str, max_words: int = 400) -> list[Chunk]:
    """Loads every .md file in kb_dir and returns a flat list of Chunk objects."""
    chunks: list[Chunk] = []
    if not os.path.isdir(kb_dir):
        return chunks

    for fname in sorted(os.listdir(kb_dir)):
        if not fname.endswith(".md"):
            continue
        source = fname
        path = os.path.join(kb_dir, fname)
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()

        # Title = first '# ' line if present
        title_match = re.search(r"(?m)^#\s+(.*)$", raw)
        doc_title = title_match.group(1).strip() if title_match else fname

        sections = _split_by_heading(raw)
        for sec_idx, (heading, body) in enumerate(sections):
            if not body.strip():
                continue
            # Prepend the heading to the indexed text: headings often carry
            # the key topic words (e.g. "What are your working hours?")
            # that the body prose doesn't repeat, and dropping them would
            # otherwise starve retrieval of the exact terms a user searches for.
            indexed_prefix = f"{heading}\n" if heading not in ("_intro", "_full") else ""
            for part_idx, part in enumerate(_chunk_words(body, max_words=max_words)):
                indexed_text = indexed_prefix + part
                cid = f"{source}:{sec_idx}:{part_idx}"
                chunks.append(
                    Chunk(
                        id=cid,
                        text=indexed_text,
                        metadata={
                            "source": source,
                            "doc_title": doc_title,
                            "section": heading,
                        },
                    )
                )
    return chunks


def build_vector_store_from_dir(
    kb_dir: str,
    embeddings: EmbeddingsClient | None = None,
    persist_dir: str | None = None,
) -> ChromaVectorStore:
    kwargs = {"embeddings": embeddings or EmbeddingsClient()}
    if persist_dir:
        kwargs["persist_dir"] = persist_dir
    store = ChromaVectorStore(**kwargs)
    store.add_documents(load_and_chunk_dir(kb_dir))
    return store
