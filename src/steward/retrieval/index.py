"""End to end local retrieval: chunk, embed, store, search.

Pipeline for "make it know the user's stuff":

    chunk_document()  split a file into stable literal markdown chunks
    OllamaEmbedder()  embed chunks and queries through the local Ollama backend
    RetrievalIndex()  SQLite store plus cosine search (numpy, pure-python fallback)

The index is incremental: a file is skipped when (mtime, size) are unchanged,
deleted files are pruned, and ignored names and binary extensions are never
scanned. Search returns citations: the file path, the char offset of the chunk
inside that file, a score and a 200-char snippet, so a downstream agent can
quote the exact source text.

Everything is local. Vectors live as float32 BLOBs in <data>/db/index.db and
never leave the box. The embedder defaults to nomic-embed-text on the local
Ollama (http://localhost:11434, the v1 backend) and pulls it once if absent
(~275 MB, well under the 20 GB guard).

Import surface: stdlib plus steward.paths and steward.runtime (BackendError).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import time
import urllib.error
import urllib.request
from pathlib import Path

from steward.paths import data_root
from steward.runtime import BackendError

try:
    import numpy as np  # type: ignore

    _HAS_NUMPY = True
except Exception:  # pragma: no cover - host without numpy
    np = None
    _HAS_NUMPY = False

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_EMBED_MODEL = "nomic-embed-text"
EMBED_MODEL_SIZE_MB = 275

TARGET_CHARS = 800
OVERLAP_CHARS = 120
MAX_FILE_BYTES = 2 * 1024 * 1024
SNIPPET_CHARS = 200
MMR_LAMBDA = 0.7

# Extensions treated as indexable text. Everything else is ignored, which
# covers every binary format (images, pdfs, archives, executables).
DEFAULT_TEXT_EXTENSIONS = frozenset({".md", ".markdown", ".txt"})

# Directory names never scanned, wherever they appear in a walk.
IGNORED_DIR_NAMES = frozenset({
    "node_modules", "venv", ".venv", "__pycache__", "site-packages",
    "dist", "build", ".tox", ".eggs", ".pytest_cache",
})

_HEADING_RE = re.compile(r"^#{1,6}[ \t]+\S")


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------


def chunk_document(text: str, target: int = TARGET_CHARS,
                   overlap: int = OVERLAP_CHARS) -> list[tuple[str, int]]:
    """Split a document into stable chunks with literal source offsets.

    Returns a list of (slice_text, char_offset) pairs. Every element is a
    literal, contiguous slice of the input, so for each pair the identity
    text[offset:offset + len(slice_text)] == slice_text always holds. That is
    what makes citations trustworthy: the hit's offset points at exactly the
    source text of its chunk.

    Rules:
      markdown ATX heading        -> its own chunk
      paragraph (blank line separated) -> one chunk
      paragraph longer than target -> split with `overlap` shared characters
        between consecutive pieces

    Deterministic: identical input produces identical output, so chunk ids
    derived from (doc, ordinal, text) are stable across runs.
    """
    lines = text.splitlines(keepends=True)
    offsets = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line)

    chunks: list[tuple[str, int]] = []
    block: list[str] = []
    block_start = 0

    def flush() -> None:
        nonlocal block, block_start
        if block:
            segment = "".join(block)
            chunks.extend(_split_long(segment, block_start, target, overlap))
        block = []
        block_start = 0

    for i, line in enumerate(lines):
        if _HEADING_RE.match(line):
            flush()
            chunks.extend(_split_long(line, offsets[i], target, overlap))
        elif not line.strip():
            flush()
        else:
            if not block:
                block_start = offsets[i]
            block.append(line)
    flush()
    return chunks


def _split_long(text: str, base: int, target: int,
                overlap: int) -> list[tuple[str, int]]:
    if len(text) <= target:
        return [(text, base)]
    step = max(1, target - overlap)
    n = len(text)
    out = []
    start = 0
    while start < n:
        end = min(start + target, n)
        out.append((text[start:end], base + start))
        if end == n:
            break
        start += step
    return out


def chunk_id(doc_id: str, ordinal: int, text: str) -> str:
    """Stable content-derived id: same doc, ordinal and text always collide."""
    digest = hashlib.sha256()
    digest.update(doc_id.encode("utf-8"))
    digest.update(b"::")
    digest.update(str(ordinal).encode("ascii"))
    digest.update(b"::")
    digest.update(text.encode("utf-8", errors="replace"))
    return digest.hexdigest()[:24]


def _snippet(text: str, limit: int = SNIPPET_CHARS) -> str:
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean
    return clean[:limit] + "..."


# --------------------------------------------------------------------------
# Embedding (Ollama backend)
# --------------------------------------------------------------------------


class OllamaEmbedder:
    """Embeds texts via the local Ollama /api/embed endpoint.

    Batches texts into one HTTP call; falls back to one /api/embeddings call
    per text on servers that predate /api/embed. ensure_model() pulls
    nomic-embed-text once when it is not already resident. Transport is stdlib
    urllib only; there are no third-party dependencies.
    """

    def __init__(self, url: str = DEFAULT_OLLAMA_URL, model: str = DEFAULT_EMBED_MODEL,
                 batch: int = 32, timeout: float = 120.0) -> None:
        self.url = url.rstrip("/")
        self.model = model or DEFAULT_EMBED_MODEL
        self.batch = max(1, int(batch))
        self.timeout = timeout
        self._model_checked = False

    def ensure_model(self) -> dict:
        """Report and guarantee the embed model is resident (pull once)."""
        if self._model_checked:
            return {"ok": True, "model": self.model, "resident": True, "pulled": False}
        try:
            tags = self._get_json("/api/tags")
        except Exception as exc:  # noqa: BLE001 - surfaced as BackendError
            raise BackendError("embed backend unreachable at %s: %s" % (self.url, exc)) from exc
        names = {m.get("name", "") for m in tags.get("models", [])}
        present = self.model in names or self.model + ":latest" in names
        if present:
            self._model_checked = True
            return {"ok": True, "model": self.model, "resident": True, "pulled": False}
        self._pull()
        self._model_checked = True
        return {"ok": True, "model": self.model, "resident": True, "pulled": True}

    def _pull(self) -> None:
        try:
            self._post_json("/api/pull", {"model": self.model, "stream": False}, timeout=600.0)
        except Exception as exc:  # noqa: BLE001 - surfaced as BackendError
            raise BackendError(
                "could not pull %s (~%d MB): %s" % (self.model, EMBED_MODEL_SIZE_MB, exc)
            ) from exc

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), self.batch):
            out.extend(self._embed_batch(texts[start:start + self.batch]))
        return out

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        try:
            data = self._post_json("/api/embed", {"model": self.model, "input": list(texts)})
            got = [e for e in data.get("embeddings", []) if isinstance(e, list) and e]
            if len(got) == len(texts):
                return [[float(x) for x in vec] for vec in got]
        except Exception:  # noqa: BLE001 - batched endpoint may not exist
            pass
        # Older servers: one /api/embeddings call per text.
        out = []
        for text in texts:
            data = self._post_json("/api/embeddings", {"model": self.model, "prompt": text})
            vec = data.get("embedding")
            if not isinstance(vec, list) or not vec:
                raise BackendError("embedding call returned no vector for a chunk")
            out.append([float(x) for x in vec])
        return out

    # -- urllib transport ----------------------------------------------------

    def _request(self, method: str, path: str, body: bytes | None,
                 timeout: float, headers: dict | None):
        req = urllib.request.Request(self.url + path, data=body, method=method)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        return urllib.request.urlopen(req, timeout=timeout)

    def _get_json(self, path: str, timeout: float | None = None) -> dict:
        with self._request("GET", path, None, timeout or self.timeout, None) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post_json(self, path: str, payload: dict, timeout: float | None = None) -> dict:
        body = json.dumps(payload).encode("utf-8")
        with self._request("POST", path, body, timeout or self.timeout,
                           {"Content-Type": "application/json"}) as resp:
            return json.loads(resp.read().decode("utf-8"))


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents(
    path TEXT PRIMARY KEY,
    mtime INTEGER NOT NULL,
    size INTEGER NOT NULL,
    sha256_chunk TEXT NOT NULL,
    indexed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks(
    id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL REFERENCES documents(path) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    offset INTEGER NOT NULL,
    vec BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
"""


def _pack_vector(v: list[float]) -> bytes:
    """Normalize to unit length and pack as little-endian float32 bytes."""
    if np is not None:
        arr = np.asarray(v, dtype=np.float32)
        norm = float(np.linalg.norm(arr))
        if norm:
            arr = arr / norm
        return arr.tobytes()
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return struct.pack("<%df" % len(v), *(x / norm for x in v))


# --------------------------------------------------------------------------


class RetrievalIndex:
    """SQLite-backed local embedding index over user-owned files.

    Public surface for other cards:

        index_paths(paths=None, progress=None) -> dict
        search(query, k=8) -> list[dict]
        stats() -> dict

    data_root defaults to the Steward data root (see steward.paths); the store
    lives at <data_root>/db/index.db. Pass an OllamaEmbedder for a real backend
    or a fake (deterministic, hash-based) embedder in tests.
    """

    def __init__(self, data_root_path: str | Path | None = None,
                 embedder=None,
                 text_extensions: set[str] | None = None,
                 max_file_bytes: int = MAX_FILE_BYTES) -> None:
        root = Path(data_root_path) if data_root_path is not None else data_root()
        self.data_root = Path(root)
        self.db_path = self.data_root / "db" / "index.db"
        self.embedder = embedder if embedder is not None else OllamaEmbedder()
        self.text_extensions = {
            ext.lower() if ext.startswith(".") else "." + ext.lower()
            for ext in (text_extensions or DEFAULT_TEXT_EXTENSIONS)
        }
        self.max_file_bytes = max_file_bytes
        self._revision = 0
        self._cache: dict | None = None

    # -- connection helpers ---------------------------------------------------

    def _connect(self, readonly: bool = False) -> sqlite3.Connection:
        (self.data_root / "db").mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        if not readonly:
            conn.executescript(_SCHEMA)
        return conn

    def _delete_doc(self, conn: sqlite3.Connection, doc_id: str) -> None:
        conn.execute("DELETE FROM documents WHERE path=?", (doc_id,))

    # -- indexing -------------------------------------------------------------

    def index_paths(self, paths=None, progress=None) -> dict:
        """Index directories or files, incrementally.

        paths: iterable of dirs (walked, ignore rules applied) or files. When
        None, falls back to config.retrieval_dirs. Each candidate file is
        indexed unless (mtime, size) match what is already stored; documents
        whose files have disappeared are pruned.

        progress: callable(done:int, total:int, path:str, action:str) called
        once per handled file for the daemon status line. Action is one of
        indexed, reindexed, skipped, pruned, failed.

        Returns a summary dict with counts and timing.
        """
        t0 = time.monotonic()
        roots = self._resolve_roots(paths)
        files = self._gather(roots)
        conn = self._connect()
        try:
            existing = {r["path"]: r for r in
                        conn.execute("SELECT path, mtime, size FROM documents").fetchall()}
            to_prune = [p for p in existing if not Path(p).exists()]
            total = len(files) + len(to_prune)
            summary = {
                "indexed": 0, "reindexed": 0, "skipped": 0, "pruned": 0,
                "failed": 0, "files_seen": len(files), "roots": [str(r) for r in roots],
                "last_error": None,
            }
            done = 0
            for doc_id in to_prune:
                self._delete_doc(conn, doc_id)
                summary["pruned"] += 1
                done += 1
                if progress:
                    progress(done, total, doc_id, "pruned")
            self.embedder.ensure_model()
            for fp in files:
                doc_id = str(fp)
                row = existing.get(doc_id)
                try:
                    st = fp.stat()
                except OSError as exc:
                    summary["failed"] += 1
                    summary["last_error"] = str(exc)
                    done += 1
                    if progress:
                        progress(done, total, doc_id, "failed")
                    continue
                mtime = int(st.st_mtime)
                size = st.st_size
                if row is not None and row["mtime"] == mtime and row["size"] == size:
                    summary["skipped"] += 1
                    action = "skipped"
                else:
                    try:
                        n_chunks = self._index_file(conn, fp, mtime, size)
                        if row is None:
                            summary["indexed"] += 1
                            action = "indexed"
                        else:
                            summary["reindexed"] += 1
                            action = "reindexed"
                    except Exception as exc:  # noqa: BLE001 - one bad file stops nothing
                        summary["failed"] += 1
                        summary["last_error"] = str(exc)
                        action = "failed"
                done += 1
                if progress:
                    progress(done, total, doc_id, action)
            counts = self._counts(conn)
            summary.update({
                "documents": counts["documents"], "chunks": counts["chunks"],
                "seconds": round(time.monotonic() - t0, 3),
            })
            self._revision += 1
            conn.commit()
            return summary
        finally:
            conn.close()

    def _index_file(self, conn: sqlite3.Connection, path: Path,
                    mtime: int, size: int) -> int:
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        text = self._decode(raw)
        chunks = chunk_document(text)
        texts = [c for c, _ in chunks]
        vecs = self.embedder.embed(texts) if texts else []
        doc_id = str(path)
        with conn:  # one transaction per file
            self._delete_doc(conn, doc_id)
            conn.execute(
                "INSERT INTO documents(path, mtime, size, sha256_chunk, indexed_at)"
                " VALUES(?, ?, ?, ?, ?)",
                (doc_id, mtime, size, digest, time.time()),
            )
            for ordinal, ((chunk_text, offset), vec) in enumerate(zip(chunks, vecs)):
                conn.execute(
                    "INSERT INTO chunks(id, doc_id, ordinal, text, offset, vec)"
                    " VALUES(?, ?, ?, ?, ?, ?)",
                    (chunk_id(doc_id, ordinal, chunk_text), doc_id, ordinal,
                     chunk_text, offset, _pack_vector(vec)),
                )
            dim = len(vecs[0]) if vecs else self._stored_dim(conn)
            if dim:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('vector_dim', ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(dim),),
                )
        return len(chunks)

    @staticmethod
    def _decode(raw: bytes) -> str:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            # Non-UTF-8 bytes: decode lossy so indexing never dies. Citation
            # offsets then refer to this decoded text, not the raw bytes.
            return raw.decode("utf-8", errors="replace")

    def _stored_dim(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT value FROM meta WHERE key='vector_dim'").fetchone()
        return int(row["value"]) if row else 0

    def _counts(self, conn: sqlite3.Connection) -> dict:
        docs = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
        chunks = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        return {"documents": docs, "chunks": chunks}

    def _resolve_roots(self, paths) -> list[Path]:
        if paths is None:
            from steward.config import Config

            cfg = Config.load()
            paths = cfg.retrieval_dirs
        roots: list[Path] = []
        for entry in paths:
            path = Path(entry).expanduser() if not isinstance(entry, Path) else entry
            roots.append(path)
        return roots

    def _gather(self, roots: list[Path]) -> list[Path]:
        found: set[Path] = set()
        for root in roots:
            if root.is_file():
                if self._candidate(root):
                    found.add(root)
                continue
            if not root.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not self._ignored_dir(d)]
                for name in filenames:
                    candidate = Path(dirpath) / name
                    if self._candidate(candidate):
                        found.add(candidate)
        return sorted(found, key=str)

    def _ignored_dir(self, name: str) -> bool:
        return name.startswith(".") or name in IGNORED_DIR_NAMES

    def _candidate(self, path: Path) -> bool:
        if path.name.startswith("."):
            return False
        if path.suffix.lower() not in self.text_extensions:
            return False
        try:
            if path.stat().st_size > self.max_file_bytes:
                return False
        except OSError:
            return False
        return True

    # -- search ---------------------------------------------------------------

    def search(self, query: str, k: int = 8) -> list[dict]:
        """Cosine search over stored chunks with MMR-lite dedupe.

        Returns up to k hits dicts with keys: path, ordinal, offset, score,
        snippet, text, chunk_id. The offset is a char offset into the source
        file and points at the first character of the chunk's text, so a
        downstream agent can cut the exact passage and cite it.
        """
        if not query or not query.strip() or k <= 0:
            return []
        qvec = self.embedder.embed([query])[0]
        qvec_norm = self._normalize(qvec)
        cache = self._load_cache()
        n = len(cache["ids"])
        if n == 0:
            return []
        dim = cache["dim"]
        scores: list[float] = []

        if _HAS_NUMPY and np is not None and cache["matrix"] is not None:
            q = np.asarray(qvec_norm, dtype=np.float32)
            scores = [float(x) for x in (cache["matrix"] @ q)]
            index_of = list(range(n))
            index_of.sort(key=lambda i: -scores[i])
        else:
            q = qvec_norm
            if cache["vecs"]:
                scores = [sum(a * b for a, b in zip(vec, q)) for vec in cache["vecs"]]
            else:
                scores = [0.0] * n
            index_of = list(range(n))
            index_of.sort(key=lambda i: -scores[i])

        candidates = index_of[:max(k * 6, 50)]

        def sim(i: int, j: int) -> float:
            if _HAS_NUMPY and np is not None and cache["matrix"] is not None:
                return float(cache["matrix"][i] @ cache["matrix"][j])
            a, b = cache["vecs"][i], cache["vecs"][j]
            return sum(x * y for x, y in zip(a, b)) if dim else 0.0

        chosen = _mmr_select(candidates, scores, sim, k)
        hits = []
        for i in chosen:
            hits.append({
                "path": cache["paths"][i],
                "ordinal": cache["ordinals"][i],
                "offset": cache["offsets"][i],
                "score": round(scores[i], 6),
                "snippet": _snippet(cache["texts"][i]),
                "text": cache["texts"][i],
                "chunk_id": cache["ids"][i],
            })
        hits.sort(key=lambda h: -h["score"])
        return hits

    @staticmethod
    def _normalize(v: list[float]):
        if np is not None:
            arr = np.asarray(v, dtype=np.float32)
            norm = float(np.linalg.norm(arr))
            return (arr / norm) if norm else arr
        norm = math.sqrt(sum(x * x for x in v))
        return [x / norm for x in v] if norm else v

    def _load_cache(self) -> dict:
        cache = self._cache
        if cache is not None and cache.get("revision") == self._revision:
            return cache
        cache = {
            "revision": self._revision, "ids": [], "paths": [], "ordinals": [],
            "offsets": [], "texts": [], "matrix": None, "vecs": [], "dim": 0,
        }
        if not self.db_path.exists():
            self._cache = cache
            return cache
        conn = self._connect(readonly=True)
        try:
            rows = conn.execute(
                "SELECT id, doc_id, ordinal, text, offset, vec"
                " FROM chunks ORDER BY doc_id, ordinal"
            ).fetchall()
            meta = {r["key"]: r["value"] for r in
                    conn.execute("SELECT key, value FROM meta").fetchall()}
        finally:
            conn.close()
        dim = int(meta.get("vector_dim", 0))
        ids, paths, ordinals, offsets, texts, blobs = [], [], [], [], [], []
        for row in rows:
            ids.append(row["id"])
            paths.append(row["doc_id"])
            ordinals.append(row["ordinal"])
            offsets.append(row["offset"])
            texts.append(row["text"])
            blobs.append(row["vec"])
        matrix = None
        vecs = []
        if blobs:
            if np is not None:
                try:
                    matrix = np.vstack(
                        [np.frombuffer(b, dtype=np.float32) for b in blobs]
                    ).astype(np.float32, copy=False)
                except ValueError as exc:
                    raise BackendError(
                        "stored chunk vectors have inconsistent dimensions: %s" % exc
                    ) from exc
            else:
                fmt = "<%df" % dim if dim else None
                if fmt is None:
                    raise BackendError("vector_dim missing from index metadata")
                vecs = [list(struct.unpack(fmt, b)) for b in blobs]
        cache.update({
            "ids": ids, "paths": paths, "ordinals": ordinals,
            "offsets": offsets, "texts": texts,
            "matrix": matrix, "vecs": vecs, "dim": dim,
        })
        self._cache = cache
        return cache

    # -- stats ----------------------------------------------------------------

    def stats(self) -> dict:
        """Document, chunk, dimension and on-disk size counts."""
        if not self.db_path.exists():
            return {
                "db_path": str(self.db_path), "documents": 0, "chunks": 0,
                "vector_dim": 0, "db_size_bytes": 0, "indexed_at": None,
            }
        conn = self._connect(readonly=True)
        try:
            counts = self._counts(conn)
            meta = {r["key"]: r["value"] for r in
                    conn.execute("SELECT key, value FROM meta").fetchall()}
            row = conn.execute("SELECT MAX(indexed_at) AS t FROM documents").fetchone()
        finally:
            conn.close()
        size = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.db_path) + suffix)
            if p.exists():
                size += p.stat().st_size
        return {
            "db_path": str(self.db_path),
            "documents": counts["documents"],
            "chunks": counts["chunks"],
            "vector_dim": int(meta.get("vector_dim", 0)),
            "db_size_bytes": size,
            "indexed_at": row["t"] if row else None,
        }


# --------------------------------------------------------------------------
# MMR-lite
# --------------------------------------------------------------------------


def _mmr_select(candidates: list[int], scores: list[float], sim,
                k: int, lam: float = MMR_LAMBDA) -> list[int]:
    """Greedy Maximal Marginal Relevance over candidate chunk indices.

    Each pick maximizes lam * relevance - (1 - lam) * max similarity to items
    already picked, so near-duplicate chunks and same-document repeats are
    spread apart. Falls back to plain relevance ranking when k covers all
    candidates.
    """
    if len(candidates) <= k:
        return list(candidates)
    remaining = list(candidates)
    chosen: list[int] = []
    while remaining and len(chosen) < k:
        best_idx: int | None = None
        best_val: float | None = None
        for i in remaining:
            marginal = scores[i]
            if chosen:
                marginal = lam * scores[i] - (1 - lam) * max(sim(i, j) for j in chosen)
            if best_val is None or marginal > best_val:
                best_val = marginal
                best_idx = i
        assert best_idx is not None
        chosen.append(best_idx)
        remaining.remove(best_idx)
    return chosen
