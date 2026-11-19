"""End-to-end tests for F8: chat runs the tool loop (server side).

Proves the wiring, not the tools themselves (the tools package is covered by
tests/test_tools.py). Three fail-closed invariants are the point of the card:

    * /v1/chat executes a tool-eligible reply through the daemon and streams
      the frozen tool_call + tool_result SSE events instead of returning the
      model's raw JSON blob as text;
    * a write tool emits approval_required, pauses the turn, and only executes
      after a human approves via POST /v1/approvals/{id}; a deny returns a
      model-visible denial and leaves no side effect;
    * the /chat shim runs the same loop and returns final JSON text.

Each test fails if the daemon wiring is removed (a reverted _handle_v1_chat
that streams raw model text has no tool_call event; a shim that does not run
the loop returns the raw JSON as text).

The daemon runs on port 0 with a scripted model runtime, so no Ollama and no
network beyond loopback, and everything is torn down in the fixture.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from pathlib import Path

import pytest

from steward.server import daemon as daemon_mod
from steward.tools.loop import _may_still_be_tool_call as may_still, summarize_result


def tool_json(name, args):
    return json.dumps({"tool": name, "args": args})


class ScriptedRuntime:
    """Model stand-in that answers from a fixed response list (last repeats)."""

    kind = "scripted"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def health(self):
        return {"ok": True, "models": ["qwen3:8b"]}

    def chat(self, messages, stream=True, model=None):
        self.calls.append(list(messages))
        reply = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if stream:
            yield {"delta": reply}
        else:
            yield {"delta": reply}


class ChunkedRuntime(ScriptedRuntime):
    """Yield replies a few characters at a time, like a real streaming model."""

    def chat(self, messages, stream=True, model=None):
        self.calls.append(list(messages))
        reply = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        for index in range(0, len(reply), 4):
            yield {"delta": reply[index:index + 4]}


@pytest.fixture
def env(tmp_path):
    return {"tmp": tmp_path, "db_path": tmp_path / "db" / "steward.db",
            "secret_file": tmp_path / "db" / "pairing.key"}


@pytest.fixture
def start_daemon(env):
    started = []

    def start(runtime, advertise=False, db_path=None, secret_file=None, **kw):
        srv = daemon_mod.create_server(
            ("127.0.0.1", 0), runtime=runtime,
            db_path=Path(db_path) if db_path else env["db_path"],
            secret_file=Path(secret_file) if secret_file else env["secret_file"],
            advertise=advertise, **kw)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        started.append((srv, thread))
        return srv, srv.server_address[1]

    yield start
    for srv, thread in started:
        srv.shutdown_server()
        thread.join(timeout=5)


def request(port, method, path, body=None, token=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = {"Content-Type": "application/json"}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    if headers:
        hdrs.update(headers)
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


def pair(port):
    status, code_resp = request(port, "GET", "/pair/code")
    assert status == 200, code_resp
    status, resp = request(port, "POST", "/v1/pair",
                           {"code": code_resp["code"], "device": "f8-pytest"})
    assert status == 200, resp
    return resp["token"]


class SseSession:
    """Open /v1/chat over a raw socket in a background thread and collect the
    parsed SSE events ([DONE] is consumed and not stored; nothing after it is
    needed)."""

    def __init__(self, port, token, body):
        self.port = port
        self.token = token
        self.body = body
        self.events: list = []
        self.error: Exception | None = None
        self.thread: threading.Thread | None = None

    def start(self):
        def run():
            sock = None
            try:
                sock = socket.create_connection(("127.0.0.1", self.port), timeout=15)
                payload = json.dumps(self.body).encode("utf-8")
                req = (
                    f"POST /v1/chat HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{self.port}\r\n"
                    f"Authorization: Bearer {self.token}\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(payload)}\r\n\r\n"
                ).encode("utf-8") + payload
                sock.sendall(req)
                buf = b""
                while b"\r\n\r\n" not in buf:
                    chunk = sock.recv(4096)
                    if not chunk:
                        raise RuntimeError("daemon closed before headers")
                    buf += chunk
                head, _, rest = buf.partition(b"\r\n\r\n")
                if b" 200 " not in head.split(b"\r\n", 1)[0]:
                    raise RuntimeError("non-200 SSE response: %r" % head)
                pending = rest

                def fill():
                    nonlocal pending
                    chunk = sock.recv(4096)
                    if not chunk:
                        return False
                    pending += chunk
                    return True

                while True:
                    end = pending.find(b"\n\n")
                    if end == -1:
                        if not fill():
                            break
                        continue
                    line = pending[:end].decode("utf-8", "replace")
                    pending = pending[end + 2:]
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    self.events.append(json.loads(data))
            except Exception as exc:  # noqa: BLE001 - surfaced via join()
                self.error = exc
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        return self

    def join(self, timeout=20.0):
        self.thread.join(timeout=timeout)
        assert self.thread.is_alive() is False, "SSE reader did not finish"
        assert self.error is None, "SSE reader failed: %r" % (self.error,)


def wait_for(events, predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for event in events:
            if predicate(event):
                return event
        time.sleep(0.02)
    raise AssertionError("event not seen in %.1fs; got %r" % (timeout, events))


# -- /v1/chat runs the tool loop and streams the frozen contract ----------------

def test_v1_chat_streams_tool_call_and_tool_result(tmp_path, start_daemon):
    target = tmp_path / "notes.txt"
    target.write_text("buy milk", encoding="utf-8")
    rt = ScriptedRuntime([
        tool_json("fs.read", {"path": str(target)}),
        "The note says: buy milk.",
    ])
    srv, port = start_daemon(rt)
    token = pair(port)

    session = SseSession(port, token, {"text": "read the note"}).start()
    call = wait_for(session.events, lambda e: e.get("event") == "tool_call")
    assert call["tool"] == "fs.read"
    assert call["risk"] == "read"
    assert call["args"] == {"path": str(target)}
    assert isinstance(call["id"], str) and len(call["id"]) == len("00000000-0000-0000-0000-000000000000")

    result = wait_for(session.events, lambda e: e.get("event") == "tool_result")
    assert result["id"] == call["id"]
    assert result["ok"] is True
    assert "bytes" in result["summary"]
    assert isinstance(result["ms"], int)

    session.join()
    tail = "".join(e["delta"] for e in session.events if "delta" in e)
    assert "buy milk" in tail
    # The raw tool JSON never reaches the delta stream (the defect's fix).
    for event in session.events:
        if "delta" in event:
            assert "fs.read" not in event["delta"]


def test_v1_chat_never_streams_tool_json_even_when_chunked(tmp_path, start_daemon):
    target = tmp_path / "notes.txt"
    target.write_text("x", encoding="utf-8")
    rt = ChunkedRuntime([
        tool_json("fs.read", {"path": str(target)}),
        "done reading",
    ])
    srv, port = start_daemon(rt)
    token = pair(port)

    session = SseSession(port, token, {"text": "read"}).start()
    wait_for(session.events, lambda e: e.get("event") == "tool_call")
    session.join()
    for event in session.events:
        if "delta" in event:
            assert "fs.read" not in event["delta"]
    assert "done reading" in "".join(e["delta"] for e in session.events if "delta" in e)


# -- approvals: required event, then approve executes / deny leaves nothing ----

def test_v1_chat_approval_required_then_approve_executes(tmp_path, start_daemon):
    target = tmp_path / "approved.txt"
    rt = ScriptedRuntime([
        tool_json("fs.write", {"path": str(target), "content": "made"}),
        "Wrote the file.",
    ])
    srv, port = start_daemon(rt)
    token = pair(port)

    session = SseSession(port, token, {"text": "write a file"}).start()
    req = wait_for(session.events, lambda e: e.get("event") == "approval_required")
    assert req["tool"] == "fs.write" and req["risk"] == "write"
    assert req["args"] == {"path": str(target), "content": "made"}
    assert "T" in req["expires_at"]  # iso8601
    assert isinstance(req["approval_id"], int)

    # The turn is paused on the approval; a separate POST decides it and the
    # stream resumes, exactly the frozen contract.
    status, resp = request(port, "POST", f"/v1/approvals/{req['approval_id']}",
                           {"approve": True}, token=token)
    assert status == 200 and resp["ok"] is True

    result = wait_for(session.events, lambda e: e.get("event") == "tool_result")
    assert result["id"] == req["id"]
    assert result["ok"] is True
    session.join()
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "made"


def test_v1_chat_denied_approval_leaves_no_side_effect(tmp_path, start_daemon):
    target = tmp_path / "denied.txt"
    rt = ScriptedRuntime([
        tool_json("fs.write", {"path": str(target), "content": "nope"}),
        "I did not write the file.",
    ])
    srv, port = start_daemon(rt)
    token = pair(port)

    session = SseSession(port, token, {"text": "write a file"}).start()
    req = wait_for(session.events, lambda e: e.get("event") == "approval_required")
    status, resp = request(port, "POST", f"/v1/approvals/{req['approval_id']}",
                           {"approve": False}, token=token)
    assert status == 200 and resp["approval"]["status"] == "denied"

    result = wait_for(session.events, lambda e: e.get("event") == "tool_result")
    assert result["id"] == req["id"] and result["ok"] is False
    session.join()
    assert not target.exists()  # deny must leave no side effect


# -- /chat shim runs the same loop, JSON-only -----------------------------------

def test_chat_shim_runs_loop_and_returns_json_text(tmp_path, start_daemon):
    target = tmp_path / "notes.txt"
    target.write_text("hello", encoding="utf-8")
    rt = ScriptedRuntime([
        tool_json("fs.read", {"path": str(target)}),
        "The file says hello.",
    ])
    srv, port = start_daemon(rt)
    token = pair(port)

    status, resp = request(port, "POST", "/chat", {"text": "read it"}, token=token)
    assert status == 200
    assert resp["role"] == "assistant"
    assert resp["text"] == "The file says hello."
    assert "fs.read" not in resp["text"]  # no JSON blob in the reply


# -- streaming hold predicate + summaries (unit level) --------------------------

def test_may_still_be_tool_call_predicate():
    assert may_still('{"tool": "fs.read", "args": {"path": "/x"}')
    assert may_still("{")
    assert may_still('')
    assert may_still('{"tool": "fs.')
    assert may_still('```json\n{"tool": "fs.read", "args": {"path": "/x"}}')
    assert not may_still("please ")
    assert not may_still("The file")
    assert not may_still('{"tool": 5, "args": {}}')   # complete non-tool JSON
    assert not may_still('{"tool": "p", "args": {}} trailing text')


def test_summarize_result_short_lines():
    assert summarize_result("fs.read", {"ok": True,
                                        "output": {"path": "/x/n.txt", "bytes": 4}}) \
        == "read n.txt (4 bytes)"
    assert summarize_result("fs.write", {"ok": False, "error": "denied"}) == "denied"
    assert summarize_result("shell.run", {"ok": True, "output": {
        "exit_code": 0, "stdout": "ok\nmore", "stderr": ""}}) == "command exited 0: ok"
    assert summarize_result("http.fetch", {"ok": True, "output": {
        "status": 200, "bytes": 12}}) == "http 200 (12 bytes)"
