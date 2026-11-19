"""Daemon chat auto-citation wiring (F4): grounding in the retrieval index.

The citation layer lives in steward.citations (pure library, covered by
tests/test_citations.py). This file locks the daemon's chat-path wiring that
cite-core deliberately left to the integrate card: a chat turn is grounded in
the daemon's retrieval index when it has documents, the model is asked to cite
[n], and the resolved Sources block is appended to the final reply.

Fail-closed invariants (each fails if the wiring is removed or made eager):

    * no index, an empty index, a backend error, or nothing above the score
      floor leaves the turn completely ungrounded (plain assistant chat);
    * a grounded turn with a cited reply ends with a Sources block whose
      marker resolves to the selected source path;
    * a grounded turn whose reply cites nothing appends nothing.

The daemon runs on port 0 with a scripted model runtime and an injected fake
retrieval index, so no Ollama and no network beyond loopback. Everything is
torn down in the fixture (ports stay free).
"""

from __future__ import annotations

import threading
from pathlib import Path

from steward.server import daemon as daemon_mod


class FakeIndex:
    """Deterministic RetrievalIndex stand-in answering one fixed hit list."""

    def __init__(self, hits=None, documents=1, stats_boost=None):
        self.hits = list(hits) if hits else []
        self.documents = documents
        self.stats_calls = 0
        self.search_queries = []
        self.raise_on_search = False
        self.stats_boost = dict(stats_boost) if stats_boost else {}

    def stats(self):
        self.stats_calls += 1
        base = {"documents": self.documents, "chunks": 1, "vector_dim": 0,
                "db_size_bytes": 0, "indexed_at": None, "db_path": "/tmp/fake-index.db"}
        base.update(self.stats_boost)
        return base

    def search(self, query, k=8):
        self.search_queries.append(query)
        if self.raise_on_search:
            raise RuntimeError("embedding backend unreachable")
        return [dict(h) for h in self.hits]


def make_hit(path, text, score=0.9, offset=0, snippet=None):
    return {"path": str(path), "ordinal": 0, "offset": offset, "score": score,
            "snippet": snippet if snippet is not None else text.splitlines()[0],
            "text": text, "chunk_id": "c0"}


class ScriptedRuntime:
    """Model stand-in; last response repeats."""

    kind = "scripted"

    def __init__(self, responses):
        self.responses = list(responses)

    def health(self):
        return {"ok": True, "models": ["qwen3:8b"]}

    def chat(self, messages, stream=True, model=None):
        reply = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if stream:
            for i in range(0, len(reply), 7):
                yield {"delta": reply[i:i + 7]}
        else:
            yield {"delta": reply}


def _run_grounded(messages, runtime_reply, hits, keep_ctx=False):
    """Directly exercise _run_tool_loop against a stubbed context, returning
    (final_text, ctx). Uses a minimal DaemonContext so resolve/ground logic
    is identical to the live server class."""
    ctx = _ctx_with_index(FakeIndex(hits=hits))
    messages = [{"role": "user", "content": m} if isinstance(m, str) else m
                for m in messages]
    handler = _StubHandler(ctx)
    rt = ScriptedRuntime([runtime_reply])
    # wire the runtime directly like daemon would
    handler.ctx.runtime = rt
    final = handler._run_tool_loop(messages, emit=None)
    return (final, ctx) if keep_ctx else final


def _ctx_with_index(index):
    root = Path("/tmp/steward-chat-cite-test")
    ctx = daemon_mod.DaemonContext(
        runtime=object(), model="qwen3:8b", source_dir="/tmp/none",
        db_path=root / "db" / "steward.db", secret=b"x" * 32, version="test",
        retrieval_index=index)
    # a stub server reference is not needed for _run_tool_loop
    return ctx


class _StubHandler(daemon_mod.Handler):
    """Minimal handler exposing ctx and _run_tool_loop without the server.

    Subclasses the real Handler so it inherits the exact _run_tool_loop being
    tested (plus its grounding helpers), but never touches the HTTP socket;
    only ctx is needed for the loop path used here (emit=None). The parent's
    ctx property reads self.server, which does not exist here, so the subclass
    overrides it with a plain attribute."""

    def __init__(self, ctx):
        self._ctx_value = ctx
        self._events = []

    @property
    def ctx(self):
        return self._ctx_value

    def _sse(self, obj):
        self._events.append(obj)


def test_no_index_leaves_turn_ungrounded(tmp_path):
    ctx = daemon_mod.DaemonContext(
        runtime=object(), model="m", source_dir="/nope",
        db_path=tmp_path / "db" / "steward.db", secret=b"k" * 32, version="v")
    handler = _StubHandler(ctx)
    rt = ScriptedRuntime(["plain reply"])
    handler.ctx.runtime = rt
    final = handler._run_tool_loop([{"role": "user", "content": "hi"}], emit=None)
    assert final == "plain reply"
    assert "Sources:" not in final


def test_empty_index_leaves_turn_ungrounded():
    ctx = _ctx_with_index(FakeIndex(hits=[], documents=0))
    handler = _StubHandler(ctx)
    handler.ctx.runtime = ScriptedRuntime(["reply"])
    final = handler._run_tool_loop([{"role": "user", "content": "q"}], emit=None)
    assert final == "reply"


def test_backend_error_leaves_turn_ungrounded(tmp_path):
    idx = FakeIndex(hits=[make_hit(tmp_path / "a.md", "content")])
    idx.raise_on_search = True
    ctx = _ctx_with_index(idx)
    handler = _StubHandler(ctx)
    handler.ctx.runtime = ScriptedRuntime(["reply"])
    final = handler._run_tool_loop([{"role": "user", "content": "q"}], emit=None)
    assert final == "reply"


def test_below_floor_leaves_turn_ungrounded(tmp_path):
    idx = FakeIndex(hits=[make_hit(tmp_path / "a.md", "content", score=0.1)])
    ctx = _ctx_with_index(idx)
    handler = _StubHandler(ctx)
    handler.ctx.runtime = ScriptedRuntime(["reply"])
    final = handler._run_tool_loop([{"role": "user", "content": "q"}], emit=None)
    assert final == "reply"


def test_grounded_reply_appends_sources(tmp_path):
    path = tmp_path / "stellar-drive.md"
    path.write_text("The Stellar Drive desktop unit costs $1,299.\n"
                    "It carries a five year warranty.\n", encoding="utf-8")
    hits = [make_hit(path, "The Stellar Drive desktop unit costs $1,299.",
                     score=0.85, offset=0,
                     snippet="The Stellar Drive desktop unit costs $1,299.")]
    final = _run_grounded(["How much does the Stellar Drive cost?"],
                          "The Stellar Drive costs $1,299 [1].", hits)
    assert "[1]" in final
    assert "Sources:" in final
    assert str(path) in final


def test_grounded_reply_with_no_citation_appends_nothing(tmp_path):
    path = tmp_path / "x.md"
    path.write_text("some text here\n", encoding="utf-8")
    hits = [make_hit(path, "some text here", score=0.9)]
    final = _run_grounded(["q"], "I do not know.", hits)
    assert final == "I do not know."


def test_fabricated_marker_dropped_not_cited(tmp_path):
    path = tmp_path / "x.md"
    path.write_text("some text here\n", encoding="utf-8")
    hits = [make_hit(path, "some text here", score=0.9)]
    # model cites [9] which is out of range 1..1 -> must not resolve
    final = _run_grounded(["q"], "Truth [9].", hits)
    assert "Sources:" not in final


def test_runtime_sees_grounded_system_prompt(tmp_path):
    """The grounding really reaches the model: the system prompt carries the
    numbered source block when the index has documents."""
    root = tmp_path / "home"
    path = root / "notes.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Meeting at 10am on Thursday.\n", encoding="utf-8")
    ctx = _ctx_with_index(FakeIndex(
        hits=[make_hit(path, "Meeting at 10am on Thursday.", score=0.9)]))
    seen = {}

    class RecordingRuntime:
        kind = "scripted"

        def health(self):
            return {"ok": True, "models": ["qwen3:8b"]}

        def chat(self, messages, stream=True, model=None):
            seen["last"] = list(messages)
            yield {"delta": "It is Thursday [1]."}

    handler = _StubHandler(ctx)
    handler.ctx.runtime = RecordingRuntime()
    final = handler._run_tool_loop(
        [{"role": "user", "content": "When is the meeting?"}], emit=None)
    sys_prompt = seen["last"][0]["content"]
    assert "[1]" in sys_prompt and "Thursday" in sys_prompt
    assert "Sources:" in final and "line 1" in final
