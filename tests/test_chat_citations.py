"""Daemon chat auto-citation wiring (F4): grounding in the retrieval index.

The citation layer lives in steward.citations (pure library, covered by
tests/test_citations.py). This file locks the daemon's chat-path wiring in its
STEWARD-CITE-WIRE form: a chat turn is grounded in the daemon's retrieval
index when the index has documents and the request opts in (cite, default
True), the model is asked to cite [n], and the resolved citations come back as
a structured sources payload (a list of {n, path, start_line, end_line,
snippet} dicts) alongside the final text. The reply text itself carries only
the [n] markers the model used, never a Sources block: the client renders the
list from the sources payload.

Fail-closed invariants (each fails if the wiring is removed or made eager):

    * no index, an empty index, a backend error, or an opted-out request
      (cite=False) leaves the turn completely ungrounded: plain assistant
      chat and an empty sources payload;
    * a grounded turn with a cited reply resolves those markers to the source
      path and line range in the sources payload;
    * a grounded turn whose reply cites nothing resolves to an empty sources
      payload;
    * a no-hits grounded turn (documents present, nothing above the floor)
      still grounds with the honest no-coverage instruction and resolves to
      an empty sources payload.

The daemon runs on scripted model runtimes with injected fake retrieval
indexes, so no Ollama and no network beyond loopback. Everything is torn down
in the fixture (ports stay free).
"""

from __future__ import annotations

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


def _run_grounded(messages, runtime_reply, hits, cite=True):
    """Exercise _run_tool_loop against a stubbed context, returning
    (final_text, sources). Uses a minimal DaemonContext so resolve/ground logic
    is identical to the live server class."""
    ctx = _ctx_with_index(FakeIndex(hits=hits))
    messages = [{"role": "user", "content": m} if isinstance(m, str) else m
                for m in messages]
    handler = _StubHandler(ctx)
    handler.ctx.runtime = ScriptedRuntime([runtime_reply])
    return handler._run_tool_loop(messages, emit=None, cite=cite)


def _ctx_with_index(index):
    root = Path("/tmp/steward-chat-cite-test")
    ctx = daemon_mod.DaemonContext(
        runtime=object(), model="qwen3:8b", source_dir="/tmp/none",
        db_path=root / "db" / "steward.db", secret=b"x" * 32, version="test",
        retrieval_index=index)
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
    handler.ctx.runtime = ScriptedRuntime(["plain reply"])
    final, sources = handler._run_tool_loop(
        [{"role": "user", "content": "hi"}], emit=None)
    assert final == "plain reply"
    assert sources == []


def test_empty_index_leaves_turn_ungrounded():
    ctx = _ctx_with_index(FakeIndex(hits=[], documents=0))
    handler = _StubHandler(ctx)
    handler.ctx.runtime = ScriptedRuntime(["reply"])
    final, sources = handler._run_tool_loop(
        [{"role": "user", "content": "q"}], emit=None)
    assert final == "reply"
    assert sources == []


def test_backend_error_leaves_turn_ungrounded(tmp_path):
    idx = FakeIndex(hits=[make_hit(tmp_path / "a.md", "content")])
    idx.raise_on_search = True
    ctx = _ctx_with_index(idx)
    handler = _StubHandler(ctx)
    handler.ctx.runtime = ScriptedRuntime(["reply"])
    final, sources = handler._run_tool_loop(
        [{"role": "user", "content": "q"}], emit=None)
    assert final == "reply"
    assert sources == []


def test_no_hits_grounds_with_honest_instruction(tmp_path):
    """Documents exist but nothing scores above the floor: the turn is still
    grounded with the honest no-coverage instruction and resolves to an empty
    sources payload (never a fabricated source)."""
    path = tmp_path / "notes.md"
    path.write_text("Meeting at 10am on Thursday.\n", encoding="utf-8")
    ctx = _ctx_with_index(FakeIndex(
        hits=[make_hit(path, "Meeting at 10am on Thursday.", score=0.1)]))
    seen = {}

    class RecordingRuntime:
        kind = "scripted"

        def health(self):
            return {"ok": True, "models": ["qwen3:8b"]}

        def chat(self, messages, stream=True, model=None):
            seen["last"] = list(messages)
            yield {"delta": "Nothing in your files covers that."}

    handler = _StubHandler(ctx)
    handler.ctx.runtime = RecordingRuntime()
    final, sources = handler._run_tool_loop(
        [{"role": "user", "content": "When is the meeting?"}], emit=None)
    sys_prompt = seen["last"][0]["content"]
    assert "no passage that covers this" in sys_prompt  # honest no-coverage
    assert "Do not invent an answer" in sys_prompt
    assert final == "Nothing in your files covers that."
    assert sources == []


def test_grounded_reply_resolves_sources(tmp_path):
    path = tmp_path / "stellar-drive.md"
    path.write_text("The Stellar Drive desktop unit costs $1,299.\n"
                    "It carries a five year warranty.\n", encoding="utf-8")
    hits = [make_hit(path, "The Stellar Drive desktop unit costs $1,299.",
                     score=0.85, offset=0,
                     snippet="The Stellar Drive desktop unit costs $1,299.")]
    final, sources = _run_grounded(["How much does the Stellar Drive cost?"],
                                   "The Stellar Drive costs $1,299 [1].", hits)
    assert "[1]" in final
    assert "Sources:" not in final  # the reply text carries only the marker
    assert sources == [{
        "n": 1,
        "path": str(path),
        "start_line": 1,
        "end_line": 1,
        "snippet": "The Stellar Drive desktop unit costs $1,299.",
    }]


def test_grounded_reply_with_no_citation_resolves_empty(tmp_path):
    path = tmp_path / "x.md"
    path.write_text("some text here\n", encoding="utf-8")
    hits = [make_hit(path, "some text here", score=0.9)]
    final, sources = _run_grounded(["q"], "I do not know.", hits)
    assert final == "I do not know."
    assert sources == []


def test_fabricated_marker_dropped_not_cited(tmp_path):
    path = tmp_path / "x.md"
    path.write_text("some text here\n", encoding="utf-8")
    hits = [make_hit(path, "some text here", score=0.9)]
    # model cites [9] which is out of range 1..1 -> must not resolve
    final, sources = _run_grounded(["q"], "Truth [9].", hits)
    assert final == "Truth [9]."
    assert sources == []


def test_cite_opt_out_leaves_turn_plain(tmp_path):
    """cite=False skips grounding entirely: no source block in the model
    prompt, plain chat, empty sources payload."""
    path = tmp_path / "x.md"
    path.write_text("some text here\n", encoding="utf-8")
    hits = [make_hit(path, "some text here", score=0.9)]
    ctx = _ctx_with_index(FakeIndex(hits=hits))
    seen = {}

    class RecordingRuntime:
        kind = "scripted"

        def health(self):
            return {"ok": True, "models": ["qwen3:8b"]}

        def chat(self, messages, stream=True, model=None):
            seen["last"] = list(messages)
            yield {"delta": "plain"}

    handler = _StubHandler(ctx)
    handler.ctx.runtime = RecordingRuntime()
    final, sources = handler._run_tool_loop(
        [{"role": "user", "content": "q"}], emit=None, cite=False)
    assert final == "plain"
    assert sources == []
    assert "[1]" not in seen["last"][0]["content"]


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
    final, sources = handler._run_tool_loop(
        [{"role": "user", "content": "When is the meeting?"}], emit=None)
    sys_prompt = seen["last"][0]["content"]
    assert "[1]" in sys_prompt and "Thursday" in sys_prompt
    assert final == "It is Thursday [1]."
    assert "Sources:" not in final
    assert sources == [{
        "n": 1,
        "path": str(path),
        "start_line": 1,
        "end_line": 1,
        "snippet": "Meeting at 10am on Thursday.",
    }]
