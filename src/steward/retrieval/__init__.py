"""Local retrieval index: chunk, embed, store, search the user's own files.

The public surface for other cards is the RetrievalIndex class:

    index_paths(paths, progress=None)  incremental index of dirs or files
    search(query, k=8)                 cosine search with MMR-lite dedupe
    stats()                            document, chunk and db size counts

See index.py for the pipeline: a markdown-aware chunker, an Ollama embedder
(nomic-embed-text by default, pulled once if absent), a SQLite store under
<data>/db/index.db, and numpy-accelerated cosine search with a pure-python
fallback. Everything is local; vectors never leave the box.
"""

from steward.retrieval.index import (
    DEFAULT_EMBED_MODEL,
    MAX_FILE_BYTES,
    OVERLAP_CHARS,
    OllamaEmbedder,
    RetrievalIndex,
    TARGET_CHARS,
    chunk_document,
    chunk_id,
)

__all__ = [
    "DEFAULT_EMBED_MODEL",
    "MAX_FILE_BYTES",
    "OllamaEmbedder",
    "OVERLAP_CHARS",
    "RetrievalIndex",
    "TARGET_CHARS",
    "chunk_document",
    "chunk_id",
]
