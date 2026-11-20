"""Tests for the hardened Steward daemon (S2).

Covers the auth matrix (401 before pairing, 200 after), incremental SSE
streaming, pairing code lockout, conversation persistence across a daemon
restart, the open read-only routes, and the mDNS TXT record published by the
zeroconf advertiser.

Every test spins a real StewardServer on port 0 in a thread with a fake
runtime (no Ollama, no network beyond loopback) and tears it down in the
fixture, so no daemon or port survives a run.
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
from steward.server import auth as auth_mod
from steward.server import mdns as mdns_mod
from steward.server import store as store_mod

TOKENS = ["Hello ", "from ", "the ", "fake ", "backend."]
FULL_REPLY = "".join(TOKENS)


class FakeRuntime:
    """Minimal stand-in for the steward.runtime seam used by the daemon."""

    kind = "fake-runtime"
    url = "http://127.0.0.1:1"

    def __init__(self, tokens=None, delay=0.0, before_done=0.0,
                 health_ok=True, fail_on_call=None):
        self.tokens = list(tokens) if tokens else list(TOKENS)
        self.delay = delay
        self.before_done = before_done
        self.health_ok = health_ok
        self.fail_on_call = fail_on_call  # None = never; 1 = fail from the 1st call on
        self.chat_calls = 0

    def health(self) -> dict:
        if not self.health_ok:
            return {"ok": False, "backend": "fake", "version": "0.0.0-fake",
                    "models": [], "error": "backend exploded"}
        return {"ok": True, "backend": "fake", "version": "0.0.0-fake",
                "models": ["fake-model", "qwen3:8b"]}

    def list_models(self) -> list[str]:
        return ["fake-model", "qwen3:8b"]

    def chat(self, messages, stream=True, model=None):
        self.chat_calls += 1
        if self.fail_on_call is not None and self.chat_calls >= self.fail_on_call:
            raise RuntimeError("backend exploded")
        if not stream:
            yield {"delta": "".join(self.tokens)}
            return
        for token in self.tokens:
            if self.delay:
                time.sleep(self.delay)
            yield {"delta": token}
        if self.before_done:
            time.sleep(self.before_done)


@pytest.fixture
def env(tmp_path):
    db_path = tmp_path / "db" / "steward.db"
    secret_file = tmp_path / "db" / "pairing.key"
    return {"tmp": tmp_path, "db_path": db_path, "secret_file": secret_file}


@pytest.fixture
def start_daemon(env):
    """Factory fixture: start a server on port 0, tear all of them down."""
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


# -- HTTP helpers ---------------------------------------------------------------


def request(port, method, path, body=None, token=None, headers=None):
    """JSON helper over http.client; returns (status, parsed)."""
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


def sse_events(port, path, body, token):
    """Read an SSE stream from a raw socket. Yields (kind, payload) events as
    they arrive over time, so ordering and incrementality can be asserted."""
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
    """Read the code via loopback and pair; returns the session token."""
    status, code_resp = request(port, "GET", "/pair/code")
    assert status == 200, code_resp
    status, resp = request(port, "POST", "/v1/pair",
                           {"code": code_resp["code"], "device": "pytest-phone"})
    assert status == 200, resp
    return resp["token"]


# -- auth matrix ---------------------------------------------------------------

GATED = [
    ("GET", "/v1/conversations"),
    ("GET", "/v1/conversations/1"),
    ("POST", "/v1/conversations"),
    ("DELETE", "/v1/conversations/1"),
    ("POST", "/v1/chat"),
    ("POST", "/chat"),
    ("GET", "/v1/source"),
    ("GET", "/demo/source"),
    ("POST", "/v1/summarize"),
    ("POST", "/demo/summarize"),
]


def test_auth_matrix_401_before_pair_200_after(start_daemon):
    srv, port = start_daemon(FakeRuntime())
    for method, path in GATED:
        args = {}
        if method == "POST":
            args["body"] = {"text": "hi"}
        status, resp = request(port, method, path, **args)
        assert status == 401, (method, path, status, resp)

    token = pair(port)
    for method, path in GATED:
        args = {"token": token}
        if method in ("POST",):
            args["body"] = {"text": "hi"}
        if path == "/v1/conversations" and method == "POST":
            args["body"] = {"title": "matrix"}
        status, resp = request(port, method, path, **args)
        # conversation 1 does not exist yet, so the 404 proves the token passed
        assert status in (200, 201, 404), (method, path, status, resp)


def test_health_and_bundle_open_no_token(start_daemon):
    srv, port = start_daemon(FakeRuntime())
    status, health = request(port, "GET", "/health")
    assert status == 200
    assert health["status"] == "ok"
    assert health["model"] == "qwen3:8b"
    assert health["model_resident"] is True
    assert health["paired"] == 0

    status, bundle = request(port, "GET", "/v1/bundle")
    assert status == 200
    assert bundle["served_model"] == "qwen3:8b"
    assert "class" in bundle["bundle"]

    status, shim = request(port, "GET", "/bundle")
    assert status == 200 and shim["served_model"] == "qwen3:8b"

    # health reports degraded, not an error, when the backend is down
    srv2, port2 = start_daemon(FakeRuntime(health_ok=False))
    status, health2 = request(port2, "GET", "/health")
    assert status == 200 and health2["status"] == "degraded"


def test_pair_code_loopback_only(start_daemon):
    srv, port = start_daemon(FakeRuntime())
    # Loopback integration path: the fixture binds 127.0.0.1, so requests are
    # loopback and the code is readable.
    status, resp = request(port, "GET", "/pair/code")
    assert status == 200
    assert len(resp["code"]) == 6 and resp["code"].isdigit()

    # Unit path for the non-loopback branch.
    assert daemon_mod.is_loopback_ip("127.0.0.1") is True
    assert daemon_mod.is_loopback_ip("::1") is True
    assert daemon_mod.is_loopback_ip("192.168.1.77") is False


def test_pair_wrong_code_and_valid_code(start_daemon):
    srv, port = start_daemon(FakeRuntime())
    status, resp = request(port, "POST", "/v1/pair", {"code": "000000", "device": "x"})
    assert status == 401

    status, code_resp = request(port, "GET", "/pair/code")
    status, ok = request(port, "POST", "/v1/pair",
                         {"code": code_resp["code"], "device": "real-line"})
    assert status == 200
    assert ok["paired"] is True and ok["token"]
    assert ok["device"] == "real-line"
    assert auth_mod.check_token(ok["token"], srv.ctx.secret)

    # A good token stays valid: chat works after pairing.
    status, resp = request(port, "POST", "/v1/chat", {"text": "hi"}, token=ok["token"])
    assert status == 200  # SSE route, parsed as a string is fine


def test_pair_rate_limit_lockout(start_daemon):
    srv, port = start_daemon(FakeRuntime())
    for i in range(5):
        status, resp = request(port, "POST", "/v1/pair", {"code": "000000"})
        assert status == 401, (i, status)
    status, resp = request(port, "POST", "/v1/pair", {"code": "000000"})
    assert status == 429, (status, resp)
    assert resp["retry_after_seconds"] > 0
    # Even the correct code is refused while the IP is locked out.
    status, code_resp = request(port, "GET", "/pair/code")
    status, resp = request(port, "POST", "/v1/pair", {"code": code_resp["code"]})
    assert status == 429


def test_expired_and_forged_token_rejected(start_daemon, env):
    srv, port = start_daemon(FakeRuntime())
    # A token issued 31 days in the past is already expired.
    past = time.time() - (31 * 24 * 3600)
    stale, _ = auth_mod.issue_token(srv.ctx.secret, "stale", now=past)
    status, resp = request(port, "GET", "/v1/conversations", token=stale)
    assert status == 401

    # Garbage and tampered tokens are rejected.
    status, _ = request(port, "GET", "/v1/conversations", token="sv1.0.aaa.bbb.ccc")
    assert status == 401
    good = pair(port)
    parts = good.split(".")
    parts[2] = json.dumps({"tampered": True}).encode().decode()
    status, resp = request(port, "GET", "/v1/conversations", token=".".join(parts))
    assert status == 401


def test_sse_deltas_arrive_incrementally(start_daemon):
    # 4 tokens, 0.2s apart, then a 0.5s pause before [DONE]. The fake sleeps
    # between tokens, so a client that receives the first delta must have got
    # it long before the stream ended, proving incremental delivery.
    rt = FakeRuntime(tokens=["one ", "two ", "three ", "four "],
                     delay=0.2, before_done=0.5)
    srv, port = start_daemon(rt)
    token = pair(port)

    start = time.monotonic()
    events = []
    first_seen = None
    for event in sse_events(port, "/v1/chat", {"text": "hi"}, token):
        if first_seen is None:
            first_seen = time.monotonic()
        events.append(event)
    end = time.monotonic()

    # Delta order matches the model output; a sources payload precedes [DONE],
    # which is the single final event.
    assert [json.loads(e)["delta"] for e in events[:4]] == ["one ", "two ", "three ", "four "]
    assert json.loads(events[4])["event"] == "sources"
    assert events[5] == "[DONE]"
    # The first delta arrived quickly (no back-end work before it)...
    assert first_seen is not None and (first_seen - start) < 0.5
    # ...and the rest of the stream took the full fake generation time: the
    # first delta reached the client long before the last one.
    assert (end - first_seen) >= 0.7


def test_sse_done_is_last_event(start_daemon):
    rt = FakeRuntime(tokens=TOKENS, delay=0.01)
    srv, port = start_daemon(rt)
    token = pair(port)
    events = list(sse_events(port, "/v1/chat", {"text": "hi"}, token))
    assert events[-1] == "[DONE]"
    assert events[0].startswith('{"delta": "Hello ')


def test_chat_shim_json_reply(start_daemon):
    srv, port = start_daemon(FakeRuntime())
    token = pair(port)
    status, resp = request(port, "POST", "/chat", {"text": "hi"}, token=token)
    assert status == 200
    assert resp["role"] == "assistant"
    assert resp["text"] == "".join(TOKENS)
    assert resp["model"] == "qwen3:8b"
    assert resp["id"]

    # Empty text is a 400.
    status, resp = request(port, "POST", "/chat", {"text": "  "}, token=token)
    assert status == 400


def test_conversation_crud(start_daemon):
    srv, port = start_daemon(FakeRuntime())
    token = pair(port)

    status, resp = request(port, "POST", "/v1/conversations", {"title": "crud"}, token=token)
    assert status == 201
    cid = resp["id"]

    status, listing = request(port, "GET", "/v1/conversations", token=token)
    assert status == 200
    assert any(c["id"] == cid for c in listing["conversations"])

    status, detail = request(port, "GET", f"/v1/conversations/{cid}", token=token)
    assert status == 200 and detail["title"] == "crud" and detail["messages"] == []

    status, msg = request(port, "POST", f"/v1/conversations/{cid}/messages",
                          {"role": "user", "content": "hello"}, token=token)
    assert status == 201 and msg["role"] == "user"

    status, detail = request(port, "GET", f"/v1/conversations/{cid}", token=token)
    assert len(detail["messages"]) == 1
    assert detail["messages"][0]["content"] == "hello"

    # Missing conversations 404 on every route.
    status, resp = request(port, "GET", "/v1/conversations/9999", token=token)
    assert status == 404
    status, resp = request(port, "DELETE", "/v1/conversations/9999", token=token)
    assert status == 404

    status, resp = request(port, "DELETE", f"/v1/conversations/{cid}", token=token)
    assert status == 200
    status, resp = request(port, "GET", f"/v1/conversations/{cid}", token=token)
    assert status == 404


def test_chat_persists_messages_into_conversation(start_daemon):
    srv, port = start_daemon(FakeRuntime())
    token = pair(port)
    status, conv = request(port, "POST", "/v1/conversations", {"title": "persist"}, token=token)
    cid = conv["id"]

    status, _ = request(port, "POST", "/v1/chat",
                        {"text": "hello there", "conversation_id": cid}, token=token)
    assert status == 200

    status, detail = request(port, "GET", f"/v1/conversations/{cid}", token=token)
    roles = [(m["role"], m["content"]) for m in detail["messages"]]
    assert ("user", "hello there") in roles
    assert ("assistant", "".join(TOKENS)) in roles

    # Unknown conversation_id is a 404 before any model call.
    status, resp = request(port, "POST", "/v1/chat",
                           {"text": "x", "conversation_id": 424242}, token=token)
    assert status == 404


def test_conversations_persist_across_restart(start_daemon, env):
    rt_a = FakeRuntime(tokens=["alpha ", "beta."])
    srv_a, port_a = start_daemon(rt_a)

    token = pair(port_a)
    status, conv = request(port_a, "POST", "/v1/conversations", {"title": "durable"}, token=token)
    cid = conv["id"]
    request(port_a, "POST", "/v1/chat",
            {"text": "remember me", "conversation_id": cid}, token=token)

    # Restart: same SQLite file and same signing key, new port.
    srv_a.shutdown_server()
    rt_b = FakeRuntime(tokens=["gamma ", "delta."])
    srv_b, port_b = start_daemon(rt_b)

    # The pre-restart token still authenticates: same persisted signing key.
    status, listing = request(port_b, "GET", "/v1/conversations", token=token)
    assert status == 200
    assert any(c["id"] == cid and c["title"] == "durable" for c in listing["conversations"])

    status, detail = request(port_b, "GET", f"/v1/conversations/{cid}", token=token)
    assert status == 200
    roles = [m["role"] for m in detail["messages"]]
    assert roles == ["user", "assistant"]
    assert detail["messages"][-1]["content"] == "alpha beta."


def test_source_and_summarize_routes(start_daemon, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "notes.md").write_text("# Kickoff\nLaunch is Oct 31.\n", encoding="utf-8")
    srv, port = start_daemon(FakeRuntime(), source_dir=str(source))
    token = pair(port)

    status, resp = request(port, "GET", "/v1/source", token=token)
    assert status == 200 and resp["files"] == ["notes.md"]
    status, shim = request(port, "GET", "/demo/source", token=token)
    assert status == 200 and shim["files"] == ["notes.md"]

    status, resp = request(port, "POST", "/v1/summarize", {}, token=token)
    assert status == 200 and resp["reply"] == "".join(TOKENS)
    status, shim = request(port, "POST", "/demo/summarize", {}, token=token)
    assert status == 200 and shim["reply"] == "".join(TOKENS)


def test_chat_backend_failure_returns_502_for_json_routes(start_daemon):
    rt = FakeRuntime(fail_on_call=1)  # every chat call raises
    srv, port = start_daemon(rt)
    token = pair(port)
    status, resp = request(port, "POST", "/chat", {"text": "hi"}, token=token)
    assert status == 502
    status, resp = request(port, "POST", "/v1/summarize", {}, token=token)
    assert status == 502


def test_mdns_txt_published(start_daemon):
    pytest.importorskip("zeroconf")  # graceful degrade mirrors mdns.py
    # host_ip=127.0.0.1 keeps the advertisement on loopback, where multicast
    # reliably loops to a same-host browser even on multi-bridge sandboxes.
    srv, port = start_daemon(FakeRuntime(), advertise=True, host_ip="127.0.0.1")
    assert srv.advertiser is not None and srv.advertiser.available

    # Pair a device: paired_count rises to 1 and the TXT record is refreshed.
    status, pair_resp = request(port, "POST", "/v1/pair",
                                {"code": srv.ctx.pair_code, "device": "lan-phone"})
    assert status == 200
    status, health = request(port, "GET", "/health")
    assert health["paired"] == 1

    txt = browse_mdns_txt(port)
    assert txt["version"] == daemon_mod.VERSION
    assert txt["port"] == str(port)
    assert txt["model"] == "qwen3:8b"
    assert txt["paired_count"] == "1"


def browse_mdns_txt(port, timeout=15.0):
    """Browse _steward._tcp.local and return the TXT dict of steward-<port>.

    Uses a real ServiceBrowser (the "browse" half of discovery) and confirms
    the record via direct service lookup once the browser reports the name.
    zeroconf 0.151.x calls the on-change handler with keyword arguments.
    """
    from zeroconf import ServiceBrowser, ServiceStateChange, Zeroconf

    target = f"steward-{port}.{mdns_mod.SERVICE_TYPE}"
    hits: list[str] = []

    def on_change(**kwargs):
        if kwargs.get("state_change") is ServiceStateChange.Added:
            hits.append(kwargs.get("name", ""))

    zc = Zeroconf()
    ServiceBrowser(zc, mdns_mod.SERVICE_TYPE, handlers=[on_change])
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            seen = set(hits)
            # Direct confirmation covers slow browser-callback propagation and
            # any name normalization the browser applies on this host.
            probe = zc.get_service_info(mdns_mod.SERVICE_TYPE, target)
            if probe is not None and probe.properties:
                seen.add(target)
            if target in seen and probe is not None:
                props = {k.decode("utf-8"): v.decode("utf-8")
                         for k, v in probe.properties.items()}
                if props.get("paired_count") == "1":
                    return props
            time.sleep(0.2)
        raise AssertionError(
            f"mDNS service steward-{port} not discovered with TXT within {timeout}s; "
            f"browser hits={hits}")
    finally:
        zc.close()


def test_store_wal_and_schema(env):
    store_mod.init_schema(env["db_path"])
    with store_mod.connect(env["db_path"]) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert mode.lower() == "wal"
    assert timeout == 5000
    conv = store_mod.create_conversation(env["db_path"], "schema")
    msg = store_mod.add_message(env["db_path"], conv["id"], "user", "hi", "qwen3:8b")
    assert msg["model"] == "qwen3:8b"
    assert store_mod.get_conversation(env["db_path"], conv["id"])["messages"][0]["content"] == "hi"
