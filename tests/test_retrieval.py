"""Tests for the local retrieval index (src/steward/retrieval).

The corpus is synthetic and the embedder is a fake (deterministic bag-of-words
hash vectors), so the suite needs no model and no network. It asserts the
properties DONE requires:

  chunk ids stable across runs, incremental reindex by mtime + size only,
  deleted files pruned, ignore patterns honored, and citations pointing at the
  exact source text (char offset slice equality).

The Ollama embedder itself is exercised only against a real backend in the
measurement step; here ensure_model() and embed() are the fake's responsibility.
The suite runs with numpy available (uv --with numpy) and also covers the
pure-python cosine fallback.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
from pathlib import Path

import pytest

import steward.retrieval.index as index_mod
from steward.retrieval import (
    OVERLAP_CHARS,
    TARGET_CHARS,
    RetrievalIndex,
    chunk_document,
    chunk_id,
)
from steward.retrieval.index import _mmr_select


class FakeEmbedder:
    """Deterministic bag-of-words vectors: shared words raise cosine."""

    def __init__(self, dim: int = 64) -> None:
        self.dim = dim
        self.calls = 0  # total texts embedded
        self.inputs: list[str] = []

    def ensure_model(self) -> dict:
        return {"ok": True, "model": "fake-embedder", "resident": True, "pulled": False}

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += len(texts)
        self.inputs.extend(texts)
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            for word in re.findall(r"[A-Za-z0-9_]+", text.lower()):
                h = int.from_bytes(hashlib.sha256(word.encode("utf-8")).digest()[:8],
                                   "little")
                vec[h % self.dim] += 1.0
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm for x in vec])
        return out


def write(corpus: Path, rel: str, text: str) -> Path:
    path = corpus / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    touch = max(1_500_000_000, int(path.stat().st_mtime))
    os.utime(path, (touch, touch))
    return path


def write_bytes(corpus: Path, rel: str, raw: bytes) -> Path:
    path = corpus / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def make_index(tmp_path: Path, embedder=None) -> RetrievalIndex:
    dbfolder = tmp_path / "data"
    return RetrievalIndex(data_root_path=dbfolder,
                          embedder=embedder or FakeEmbedder())


def db_rows(index: RetrievalIndex, sql: str, args=()):
    conn = sqlite3.connect(str(index.db_path))
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


# -- chunker ------------------------------------------------------------------


def test_chunks_are_literal_contiguous_source_slices():
    text = (
        "# Hardware\n\n"
        "The bazoo flagship is expensive.\r\n\r\n"
        "## Garage\n\n"
        "The workshop hides behind the house.\n\n"
        + "p" * 2000
    )
    chunks = chunk_document(text)
    assert chunks, "a non-empty document must produce chunks"
    for slice_text, offset in chunks:
        assert text[offset:offset + len(slice_text)] == slice_text
        assert len(slice_text) <= TARGET_CHARS
    kinds = [slice_text for slice_text, _ in chunks]
    assert any(k.startswith("#") for k in kinds), "headings become their own chunks"


def test_heading_is_its_own_chunk_and_paragraphs_follow():
    text = "# Title\n\nFirst paragraph here.\n\nSecond paragraph here.\n"
    chunks = chunk_document(text)
    firsts = [slice_text for slice_text, _ in chunks]
    assert firsts[0] == "# Title\n"
    assert any("First paragraph" in s for s in firsts)
    assert any("Second paragraph" in s for s in firsts)
    assert len(firsts) == 3


def test_long_paragraph_split_with_overlap():
    paragraph = "word " * 600  # 3000 chars, well over the 800 target
    text = "# Long\n\n" + paragraph + "\n"
    chunks = chunk_document(text, target=TARGET_CHARS, overlap=OVERLAP_CHARS)
    texts = [s for s, _ in chunks]
    long_pieces = [s for s in texts if len(s) > TARGET_CHARS * 0.9]
    assert len(long_pieces) >= 3, "a long paragraph must be split several times"
    # consecutive long pieces share exactly `overlap` characters, and every
    # piece is a literal slice of the source
    for a, b in zip(long_pieces, long_pieces[1:]):
        assert a[-OVERLAP_CHARS:] == b[:OVERLAP_CHARS], "overlap must match exactly"
        assert a[-OVERLAP_CHARS:] in text
    for slice_text, offset in chunks:
        assert text[offset:offset + len(slice_text)] == slice_text


def test_chunk_ids_stable_across_runs_and_roots():
    text = "# Doc\n\nSame content, stable identity.\n"
    first = [chunk_id("doc", i, s) for i, (s, _) in enumerate(chunk_document(text))]
    again = [chunk_id("doc", i, s) for i, (s, _) in enumerate(chunk_document(text))]
    assert first == again
    assert len(set(first)) == len(first)


# -- indexing -----------------------------------------------------------------


def test_index_synthetic_corpus_and_stats(tmp_path):
    corpus = tmp_path / "corpus"
    write(corpus, "a.md", "# A\n\napple banana.\n\ncherry.\n")
    write(corpus, "b.md", "# B\n\ndate elderberry.\n")
    index = make_index(tmp_path)
    summary = index.index_paths([corpus])
    assert summary["indexed"] == 2
    assert summary["skipped"] == 0
    stats = index.stats()
    assert stats["documents"] == 2
    assert stats["chunks"] == 5  # a.md heading + 2 paragraphs, b.md heading + 1
    assert stats["vector_dim"] == 64
    assert Path(stats["db_path"]).exists()
    assert stats["db_size_bytes"] > 0


def test_incremental_reindex_skips_unchanged(tmp_path):
    corpus = tmp_path / "corpus"
    write(corpus, "a.md", "# A\n\nword word word\n")
    write(corpus, "b.md", "# B\n\nother other\n")
    embedder = FakeEmbedder()
    index = make_index(tmp_path, embedder)
    first = index.index_paths([corpus])
    embedded_first = embedder.calls
    assert first["indexed"] == 2

    second = index.index_paths([corpus])
    assert second["skipped"] == 2
    assert second["indexed"] == 0
    assert embedder.calls == embedded_first, "unchanged files must not be re-embedded"
    assert index.stats()["chunks"] == 4  # each of two files: heading + paragraph


def test_edited_file_reembedded_only(tmp_path):
    corpus = tmp_path / "corpus"
    write(corpus, "a.md", "# A\n\nstable file.\n")
    b = write(corpus, "b.md", "# B\n\nfirst second third.\n")
    embedder = FakeEmbedder()
    index = make_index(tmp_path, embedder)
    index.index_paths([corpus])
    embedded_first = embedder.calls

    os.utime(b, (1_600_000_000, 1_600_000_000))
    b.write_text("# B changed\n\nonly one paragraph now.\n", encoding="utf-8")
    os.utime(b, (1_600_000_010, 1_600_000_010))

    summary = index.index_paths([corpus])
    assert summary["reindexed"] == 1
    assert summary["skipped"] == 1
    new_embeds = embedder.calls - embedded_first
    stats = index.stats()
    assert new_embeds == 2, "only the edited file's heading and paragraph re-embed"
    assert stats["documents"] == 2
    assert stats["chunks"] == 4  # a.md (2) + edited b.md (2): heading + paragraph each
    stored = sorted(r[0] for r in db_rows(index, "SELECT text FROM chunks WHERE doc_id=?", (str(b),)))
    assert stored == ["# B changed\n", "only one paragraph now.\n"]


def test_deleted_file_pruned(tmp_path):
    corpus = tmp_path / "corpus"
    a = write(corpus, "a.md", "# A\n\nkeep me.\n")
    b = write(corpus, "b.md", "# B\n\ndelete me.\n")
    embedder = FakeEmbedder()
    index = make_index(tmp_path, embedder)
    index.index_paths([corpus])
    assert index.stats()["documents"] == 2
    calls_before = embedder.calls

    b.unlink()
    summary = index.index_paths([corpus])
    assert summary["pruned"] == 1
    assert summary["documents"] == 1
    assert db_rows(index, "SELECT path FROM documents") == [(str(a),)]
    assert db_rows(index, "SELECT COUNT(*) FROM chunks")[0][0] == 2  # a.md heading + paragraph
    assert embedder.calls == calls_before, "pruning embeds nothing"


def test_ignore_patterns(tmp_path):
    corpus = tmp_path / "corpus"
    write(corpus, "notes/ok.md", "# Ok\n\nkeep this.\n")
    write(corpus, "notes/.hidden.md", "# Hidden\n\ndropped.\n")
    write(corpus, "notes/.git/config", "[core]\n")
    write(corpus, "notes/node_modules/lib.js", "var x = 1;\n")
    write(corpus, "notes/venv/lib/python/site.py", "import os\n")
    write_bytes(corpus, "notes/picture.png", b"\x89PNG\r\n\x1a\n")
    write_bytes(corpus, "notes/report.pdf", b"%PDF-1.4 fake")
    big = corpus / "notes" / "big.md"
    big.write_text("# big\n\n" + "x" * (2 * 1024 * 1024), encoding="utf-8")
    index = make_index(tmp_path)
    summary = index.index_paths([corpus])
    assert summary["indexed"] == 1
    assert index.stats()["documents"] == 1
    stored = db_rows(index, "SELECT path FROM documents")
    assert stored == [(str(corpus / "notes" / "ok.md"),)]


def test_unicode_and_empty_files(tmp_path):
    corpus = tmp_path / "corpus"
    unicode_md = write(corpus, "unicode.md", "# Topics resume\n\nindex cafe plan.\n")
    write(corpus, "empty.md", "")
    index = make_index(tmp_path)
    index.index_paths([corpus])
    stats = index.stats()
    assert stats["documents"] == 2
    assert stats["chunks"] == 2  # unicode.md heading + paragraph; empty.md has none
    hits = index.search("index cafe", k=3)
    assert hits, "unicode query must find the unicode file"
    assert hits[0]["path"] == str(unicode_md)


def test_progress_callback_reports_actions_and_total(tmp_path):
    corpus = tmp_path / "corpus"
    write(corpus, "a.md", "# A\n\napple.\n")
    b = write(corpus, "b.md", "# B\n\nbanana.\n")
    calls = []
    index = make_index(tmp_path)
    index.index_paths(
        [corpus],
        progress=lambda done, total, path, action: calls.append((done, total, path, action)),
    )
    assert [c[0] for c in calls] == [1, 2]
    assert calls[0][1] == calls[1][1] == 2
    assert {c[3] for c in calls} == {"indexed"}
    b.unlink()
    calls.clear()
    index.index_paths(
        [corpus],
        progress=lambda done, total, path, action: calls.append((done, total, path, action)),
    )
    assert calls[0][3] == "pruned"
    assert [c[0] for c in calls] == [1, 2]


# -- search -------------------------------------------------------------------


def test_search_returns_citation_that_slices_to_exact_source(tmp_path):
    corpus = tmp_path / "corpus"
    doc = write(corpus, "doc.md",
                "# Prices\n\nThe bazoo flagship costs three thousand.\n\n# Garage\n\nThe workshop hides behind the house.\n")
    index = make_index(tmp_path)
    index.index_paths([corpus])
    hits = index.search("bazoo flagship costs", k=8)
    assert hits, "a matching phrase must be found"
    top = hits[0]
    source = doc.read_text(encoding="utf-8")
    assert top["path"] == str(doc)
    assert "bazoo flagship" in top["snippet"]
    assert top["offset"] >= 0
    assert source[top["offset"]:top["offset"] + len(top["text"])] == top["text"]
    assert len(top["snippet"]) <= 200
    assert 0.0 < top["score"] <= 1.0
    assert len(hits) <= 8


def test_search_mmr_spreads_duplicate_chunks_across_docs(tmp_path):
    corpus = tmp_path / "corpus"
    shared_para = "red green blue sky sun moon earth star"
    a = write(corpus, "a.md",
              "# Alpha\n\n%s\n\n%s\n\n%s\n" % (shared_para, shared_para, shared_para))
    b = write(corpus, "b.md", "# Beta\n\nred green blue sky sun moon over valley\n")
    index = make_index(tmp_path)
    index.index_paths([corpus])
    hits = index.search("red green blue sky sun moon", k=2)
    assert len(hits) == 2
    paths = {h["path"] for h in hits}
    assert paths == {str(a), str(b)}, \
        "MMR must push the distinct doc ahead of the exact duplicate chunk"


def test_mmr_select_dedupes_deterministically():
    scores = [0.9, 0.9, 0.85]

    def sim(i, j):
        if i == j:
            return 1.0
        if {i, j} == {0, 1}:
            return 0.99
        return 0.1

    chosen = _mmr_select([0, 1, 2], scores, sim, 2)
    assert chosen[0] in (0, 1)
    assert chosen[1] == 2, "second pick must be the distinct candidate"
    assert len(chosen) == 2


def test_search_empty_index_and_bad_k(tmp_path):
    index = make_index(tmp_path)
    assert index.search("anything") == []

    corpus = tmp_path / "corpus"
    write(corpus, "a.md", "# A\n\napple.\n")
    index = make_index(tmp_path)
    index.index_paths([corpus])
    assert index.search("apple", k=0) == []
    assert index.search("   ", k=3) == []


def test_pure_python_fallback_matches_numpy_ranking(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    write(corpus, "a.md", "# A\n\nred green blue sky.\n")
    write(corpus, "b.md", "# B\n\nred sky over mountain.\n")
    write(corpus, "c.md", "# C\n\nalpha beta gamma.\n")
    index = make_index(tmp_path)
    index.index_paths([corpus])

    query = "red green sky"
    numpy_top = index.search(query, k=2)
    assert len(numpy_top) == 2

    # Force the pure-python cosine path and rebuild the cache without numpy.
    monkeypatch.setattr(index_mod, "np", None)
    monkeypatch.setattr(index_mod, "_HAS_NUMPY", False)
    index._cache = None
    pure_top = index.search(query, k=2)
    assert [h["path"] for h in pure_top] == [h["path"] for h in numpy_top], \
        "pure-python and numpy cosine must rank the same hits"
    assert numpy_top[0]["path"] != numpy_top[1]["path"]


def test_chunk_ids_persist_across_two_separate_index_builds(tmp_path):
    corpus = tmp_path / "corpus"
    write(corpus, "a.md", "# A\n\nsame words here.\n\nmore words.\n")
    write(corpus, "b.md", "# B\n\nanother note.\n")
    index_one = make_index(tmp_path / "one")
    index_two = make_index(tmp_path / "two")
    index_one.index_paths([corpus])
    index_two.index_paths([corpus])
    one = db_rows(index_one, "SELECT id, doc_id, ordinal, text FROM chunks ORDER BY id")
    two = db_rows(index_two, "SELECT id, doc_id, ordinal, text FROM chunks ORDER BY id")
    assert one == two, "chunk ids must be identical across independent builds"
    assert len(one) == 5


def test_config_retrieval_dirs_default_root(tmp_path, monkeypatch):
    from steward.config import Config

    corpus = tmp_path / "corpus"
    write(corpus, "a.md", "# A\n\nfrom config roots.\n")
    monkeypatch.setenv("STEWARD_HOME", str(tmp_path / "home"))
    Config(retrieval_dirs=[str(corpus)]).save()
    index = RetrievalIndex(data_root_path=tmp_path / "home", embedder=FakeEmbedder())
    summary = index.index_paths()  # no explicit paths: uses config.retrieval_dirs
    assert summary["indexed"] == 1
    assert index.stats()["documents"] == 1
