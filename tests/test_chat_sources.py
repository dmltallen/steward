"""SSE wire contract for retrieval-grounded chat sources (STEWARD-CITE-WIRE).

A /v1/chat turn that is retrieval-grounded must stream the model answer and
then, before [DONE], a structured sources payload:

    data: {"event":"sources","sources":[{"n":1,"path":"...","start_line":1,"end_line":1,"snippet":"..."}]}

and a no-hits turn must stream an empty list:

    data: {"event":"sources","sources":[]}

The reply text carries only the [n] markers the model used. Tool events from
the F8 tool-loop contract coexist with the sources event in the same stream.

Every test spins a real StewardServer on port 0 with a scripted runtime and an
injected fake retrieval index (no Ollama, no network beyond loopback) and
tears it down in the fixture, so no daemon or port survives a run.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
from pathlib import Path

import pytest

from steward.server import daemon as daemon_mod


class FakeIndex:
    """Deterministic RetrievalIndex stand-in answering one fixed hit list."""

    def __init__(self, hits=None, documents=1):
        self.hits = list(hits) if hits else []
        self.documents = documents

    def stats(self):
        return {"documents": self.documents, "chunks": 1, "vector_dim": 0,
                "db_size_bytes": 0, "indexed_at": None,
                "db_path": "/tmp/fake-index.db"}

    def search(self, query, k=8):
        return [dict(h) for h in self.hits]


def make_hit(path, text, score=0.9, offset=0, snippet=None):
    return {"path": str(path), "ordinal": 0, "offset": offset, "score": score,
            "snippet": snippet if snippet is not None else text.splitlines()[0],
            "text": text, "chunk_id": "c0"}


class FakeRuntime:
    """Model stand-in: yields one delta per token, or one reply per stream."""

    kind = "fake-runtime"

    def __init__(self, tokens=None):
        self.tokens = list(tokens) if tokens else ["Hello ", "there."]

    def health(self):
        return {"ok": True, "backend": "fake", "version": "0.0.0-fake",
                "models": ["qwen3:8b"]}

    def chat(self, messages, stream=True, model=None):
        if not stream:
            yield {"delta": "".join(self.tokens)}
            return
        for token in self.tokens:
            yield {"delta": token}


class ScriptedRuntime:
    """Model stand-in that hands out one full reply per chat() call, so the
    agent loop can hop: tool reply, then the cited prose reply."""

    kind = "scripted"

    def __init__(self, replies):
        self.replies = list(replies)

    def health(self):
        return {"ok": True, "models": ["qwen3:8b"]}

    def chat(self, messages, stream=True, model=None):
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if stream:
            for i in range(0, len(reply), 11):
                yield {"delta": reply[i:i + 11]}
        else:
            yield {"delta": reply}


@pytest.fixture
def start_daemon(tmp_path):
    """Factory fixture: start a server on port 0 with an injected index."""
    started = []

    def start(runtime, index, roots=None):
        db_path = tmp_path / "db" / "steward.db"
        # Containment: a grounded turn only cites files inside a configured
        # retrieval root, so the harness pins the roots to the folders that
        # hold its corpus files (standing in for Config.retrieval_dirs).
        if roots is None:
            roots = sorted({str(Path(h["path"]).parent)
                            for h in getattr(index, "hits", []) or []}) or [str(tmp_path)]
        srv = daemon_mod.create_server(
            ("127.0.0.1", 0), runtime=runtime,
            db_path=db_path,
            secret=b"k" * 32,
            advertise=False,
            retrieval_index=index,
            retrieval_roots=roots)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        started.append((srv, thread))
        return srv, srv.server_address[1]

    yield start
    for srv, thread in started:
        srv.shutdown_server()
        thread.join(timeout=5)


# -- HTTP helpers ---------------------------------------------------------------


def request(port, method, path, body=None, token=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = {"Content-Type": "application/json"}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    payload = json.dumps(body) if body is not None else None
    conn.request(method, path, body=payload, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read().decode("utf-8", "replace")
    conn.close()
    try:
        parsed = json.loads(data) if data else {}
    except json.JSONDecodeError:
        parsed = data
    return resp.status, parsed


def sse_events(port, path, body, token):
    """Read an SSE stream from a raw socket, yielding payload strings as data:."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    payload = json.dumps(body).encode("utf-8")
    req = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Authorization: Bearer {token}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n\r\n"
    ).encode("utf-8") + payload
    sock.sendall(req)
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += sock.recv(4096)
    head, _, rest = buf.partition(b"\r\n\r\n")
    assert b" 200 " in head.split(b"\r\n", 1)[0], head.decode("utf-8", "replace")
    pending = rest

    def fill():
        nonlocal pending
        chunk = sock.recv(4096)
        pending += chunk
        return bool(chunk)

    while True:
        end = pending.find(b"\n\n")
        if end == -1:
            if not fill():
                break
            continue
        line = pending[:end].decode("utf-8", "replace")
        pending = pending[end + 2:]
        if line.startswith("data: "):
            yield line[6:]
        if line.startswith("data: [DONE]"):
            while fill():  # drain to EOF
                pass
            break
    sock.close()


def pair(port):
    status, code_resp = request(port, "GET", "/pair/code")
    assert status == 200, code_resp
    status, resp = request(port, "POST", "/v1/pair",
                           {"code": code_resp["code"], "device": "pytest-phone"})
    assert status == 200, resp
    return resp["token"]


def collect_events(port, token, body):
    """All SSE payloads as parsed JSON (or the raw string for [DONE])."""
    out = []
    for raw in sse_events(port, "/v1/chat", body, token):
        out.append(json.loads(raw) if raw != "[DONE]" else "[DONE]")
    return out


# -- tests ----------------------------------------------------------------------


def test_grounded_turn_streams_sources_before_done(start_daemon, tmp_path):
    drive = tmp_path / "stellar-drive.md"
    drive.write_text("The Stellar Drive desktop unit costs $1,299.\n"
                     "It carries a five year warranty.\n", encoding="utf-8")
    hits = [make_hit(drive, "The Stellar Drive desktop unit costs $1,299.",
                     score=0.85, offset=0,
                     snippet="The Stellar Drive desktop unit costs $1,299.")]
    rt = FakeRuntime(tokens=["The Stellar Drive costs $1,299 [1]."])
    srv, port = start_daemon(rt, FakeIndex(hits=hits))
    token = pair(port)

    events = collect_events(port, token, {"text": "How much does it cost?"})

    # deltas first, then the sources event, then [DONE]
    assert events[0] == {"delta": "The Stellar Drive costs $1,299 [1]."}
    assert events[-1] == "[DONE]"
    sources_event = [e for e in events if isinstance(e, dict)
                     and e.get("event") == "sources"]
    assert len(sources_event) == 1
    assert sources_event[0] == {
        "event": "sources",
        "sources": [{
            "n": 1,
            "path": str(drive),
            "start_line": 1,
            "end_line": 1,
            "snippet": "The Stellar Drive desktop unit costs $1,299.",
        }],
    }
    # the sources event is the final data frame before [DONE]
    assert events[-2] == sources_event[0]


def test_no_hits_turn_streams_empty_sources(start_daemon, tmp_path):
    # documents exist, but nothing scores above the 0.5 floor
    drive = tmp_path / "stellar-drive.md"
    drive.write_text("The Stellar Drive desktop unit costs $1,299.\n", encoding="utf-8")
    hits = [make_hit(drive, "The Stellar Drive desktop unit costs $1,299.", score=0.1)]
    rt = FakeRuntime(tokens=["Nothing in your files covers that."])
    srv, port = start_daemon(rt, FakeIndex(hits=hits))
    token = pair(port)

    events = collect_events(port, token, {"text": "What is 2 + 2?"})

    assert events[-1] == "[DONE]"
    sources_event = [e for e in events if isinstance(e, dict)
                     and e.get("event") == "sources"]
    assert len(sources_event) == 1
    assert sources_event[0] == {"event": "sources", "sources": []}
    assert events[-2] == sources_event[0]


def test_reply_text_stays_clean_grounded(start_daemon, tmp_path):
    """The reply text carries the [n] marker the model used, never a Sources
    block: the client renders the list from the sources event."""
    path = tmp_path / "notes.md"
    path.write_text("Meeting at 10am on Thursday.\n", encoding="utf-8")
    hits = [make_hit(path, "Meeting at 10am on Thursday.", score=0.9)]
    rt = FakeRuntime(tokens=["It is Thursday [1]."])
    srv, port = start_daemon(rt, FakeIndex(hits=hits))
    token = pair(port)

    events = collect_events(port, token, {"text": "When is the meeting?"})

    deltas = "".join(e["delta"] for e in events if isinstance(e, dict)
                     and "delta" in e)
    assert deltas == "It is Thursday [1]."
    assert "Sources:" not in deltas
    so = [e for e in events if isinstance(e, dict) and e.get("event") == "sources"][0]
    assert so["sources"][0]["path"] == str(path)
    assert so["sources"][0]["start_line"] == 1


def test_cite_false_streams_empty_sources_and_stays_clean(start_daemon, tmp_path):
    drive = tmp_path / "stellar-drive.md"
    drive.write_text("The Stellar Drive desktop unit costs $1,299.\n", encoding="utf-8")
    hits = [make_hit(drive, "The Stellar Drive desktop unit costs $1,299.", score=0.9)]
    rt = FakeRuntime(tokens=["Plain answer, no citations."])
    srv, port = start_daemon(rt, FakeIndex(hits=hits))
    token = pair(port)

    # cite:false opts the turn out of grounding entirely
    events = collect_events(port, token, {"text": "hi", "cite": False})
    assert events[-1] == "[DONE]"
    sources_event = [e for e in events if isinstance(e, dict)
                     and e.get("event") == "sources"]
    assert sources_event == [{"event": "sources", "sources": []}]


def test_tool_events_and_sources_coexist_in_one_stream(start_daemon, tmp_path):
    """The F8 tool-event contract is unchanged: tool_call + tool_result stream
    in the same SSE channel as the new sources event, deltas coming after."""
    drive = tmp_path / "stellar-drive.md"
    drive.write_text("The Stellar Drive desktop unit costs $1,299.\n", encoding="utf-8")
    hits = [make_hit(drive, "The Stellar Drive desktop unit costs $1,299.",
                     score=0.85, offset=0,
                     snippet="The Stellar Drive desktop unit costs $1,299.")]
    # hop 1: a tool call, hop 2: the cited prose reply
    tool_json = json.dumps({"tool": "fs.list", "args": {"path": str(tmp_path)}})
    rt = ScriptedRuntime(replies=[tool_json, "Listed your root; it costs $1,299 [1]."])
    srv, port = start_daemon(rt, FakeIndex(hits=hits))
    token = pair(port)

    events = collect_events(port, token, {"text": "Show my files and the price"})

    kinds = [e.get("event") if isinstance(e, dict) else e for e in events]
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    assert "sources" in kinds
    # the sources event is the last data frame before [DONE]
    assert kinds[-2] == "sources"
    assert events[-1] == "[DONE]"
    # the tool result is a real fs.list over the data root
    tr = [e for e in events if isinstance(e, dict) and e.get("event") == "tool_result"][0]
    assert tr["ok"] is True
    # the sources event resolved the [1] the reply used
    so = [e for e in events if isinstance(e, dict) and e.get("event") == "sources"][0]
    assert so["sources"][0]["n"] == 1
    assert so["sources"][0]["path"] == str(drive)


def test_ungrounded_turn_streams_empty_sources(start_daemon, tmp_path):
    """No retrieval index configured at all: plain chat, empty sources event."""
    rt = FakeRuntime(tokens=["just a chat"])
    srv, port = start_daemon(rt, FakeIndex(hits=[], documents=0))
    token = pair(port)

    events = collect_events(port, token, {"text": "hi"})
    assert events[-1] == "[DONE]"
    sources_event = [e for e in events if isinstance(e, dict)
                     and e.get("event") == "sources"]
    assert sources_event == [{"event": "sources", "sources": []}]
