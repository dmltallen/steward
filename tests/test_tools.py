"""Tests for the Steward tools package (S9).

Covers the full approval surface: schema validation before any handler runs,
write/exec/net gating with approve, deny and timeout, the /v1/approvals HTTP
contract, sandbox containment (traversal, symlink escape, absolute-outside,
sensitive paths), the https exact-host allowlist, the ICS calendar rule, the
shell disable gate, and the 4-hop agent loop including the "a denied approval
leaves the tool unexecuted" invariant.

Every server used here (approvals HTTP, local HTTPS stubs) runs on port 0 and
is torn down in the fixture, so nothing survives a run.
"""

from __future__ import annotations

import dataclasses
import http.server
import json
import os
import shlex
import ssl
import subprocess
import threading
import time
from pathlib import Path

import pytest

from steward.tools import (
    APPROVAL_RISKS,
    HOP_CAP,
    RISK_EXEC,
    RISK_NET,
    RISK_READ,
    RISK_WRITE,
    ToolSettings,
    build_registry,
    build_settings,
    execute_tool,
    extract_tool_call,
    run_loop,
    validate_args,
)
from steward.tools import http as http_mod
from steward.tools import sandbox as sandbox_mod
from steward.tools.approvals import (
    ApprovalManager,
    ApprovalsHTTPServer,
    approvals_router,
    args_hash,
)

VALID_ICS = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
    "BEGIN:VEVENT\r\nSUMMARY:Demo\r\nDTSTART:20261026T090000Z\r\nEND:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


# -- helpers ------------------------------------------------------------------


@pytest.fixture
def ctx(tmp_path):
    """A throwaway sandbox scope: fs root, approvals db+log, registry."""
    root = tmp_path / "root"
    root.mkdir(parents=True, exist_ok=True)
    settings = build_settings(cfg=None, overrides={
        "fs_roots": [root],
        "allow_shell": False,
        "http_allowlist": [],
        "approval_ttl_s": 120,
        "approval_timeout_s": 120,
        "approval_db": str(tmp_path / "db" / "approvals.db"),
        "approval_log": str(tmp_path / "logs" / "approvals.log"),
        "data_dir": str(root),
    })
    registry = build_registry(settings)
    manager = ApprovalManager(settings.approval_db, ttl_s=settings.approval_ttl_s,
                              log_path=settings.approval_log)
    scope = {"tmp": tmp_path, "root": root, "settings": settings,
             "registry": registry, "manager": manager}
    return scope


def settings_with(tmp_path, root=None, *, allow_shell=False, http_allowlist=None,
                  approval_ttl_s=120, approval_timeout_s=None, tls_context=None, **over):
    base = root if root is not None else tmp_path / "root"
    base.mkdir(parents=True, exist_ok=True)
    over.update({
        "fs_roots": [base], "allow_shell": allow_shell,
        "http_allowlist": list(http_allowlist or []),
        "approval_ttl_s": approval_ttl_s,
        "approval_timeout_s": approval_timeout_s if approval_timeout_s is not None
        else approval_ttl_s,
        "approval_db": str(tmp_path / "approvals.db"),
        "approval_log": str(tmp_path / "approvals.log"),
        "data_dir": str(base), "http_tls_context": tls_context,
    })
    return build_settings(cfg=None, overrides=over), base


def manager_for(settings):
    return ApprovalManager(settings.approval_db, ttl_s=settings.approval_ttl_s,
                           log_path=settings.approval_log)


def decide_later(manager, decision, delay=0.05):
    """Approve/deny the newest pending approval shortly after it appears."""

    def run():
        deadline = time.time() + 5
        while time.time() < deadline:
            pending = manager.list_all("pending")
            if pending:
                manager.decide(pending[0]["id"], decision)
                return
            time.sleep(0.01)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


class ScriptedRuntime:
    """Model stand-in that answers from a fixed response list (last repeats)."""

    kind = "scripted"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []  # full message list seen on every model call

    def health(self):
        return {"ok": True, "models": ["qwen3:8b"]}

    def chat(self, messages, stream=True, model=None):
        self.calls.append(list(messages))
        reply = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if not stream:
            yield {"delta": reply}
            return
        yield {"delta": reply}


def tool_call(name, args):
    return json.dumps({"tool": name, "args": args})


class _HttpsStub(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        type(self).hits += 1
        server = self.server
        if getattr(server, "redirect_url", None) is not None and self.path == "/start":
            self.send_response(302)
            self.send_header("Location", server.redirect_url)
            self.end_headers()
            return
        body = b"stub body ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # quiet
        pass


def start_https_stub(tmp_path, redirect_url=None):
    """Run a self-signed local HTTPS server on port 0. Returns (server, port)."""
    hclass = type("HttpsStubInstance", (_HttpsStub,), {"hits": 0})
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(key), "-out", str(cert), "-days", "1",
         "-subj", "/CN=localhost",
         "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost"],
        check=True, capture_output=True)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), hclass)
    tls_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    server.socket = tls_ctx.wrap_socket(server.socket, server_side=True)
    server.redirect_url = redirect_url
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def insecure_tls():
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


@pytest.fixture
def approvals_http(ctx):
    """An ApprovalsHTTPServer on port 0 backed by ctx's manager."""
    server = ApprovalsHTTPServer(ctx["manager"])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def http_json(port, method, path, body=None):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    payload = json.dumps(body) if body is not None else None
    conn.request(method, path, body=payload,
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8", "replace")
    conn.close()
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = raw
    return resp.status, parsed


# -- registry catalog and schema validation -----------------------------------

def test_registry_catalog_risks_are_correct(ctx):
    reg = ctx["registry"]
    assert set(reg) == {"fs.list", "fs.read", "fs.write", "calendar.read",
                        "calendar.write", "shell.run", "http.fetch"}
    assert reg["fs.list"].risk == RISK_READ
    assert reg["fs.read"].risk == RISK_READ
    assert reg["fs.write"].risk == RISK_WRITE
    assert reg["calendar.read"].risk == RISK_READ
    assert reg["calendar.write"].risk == RISK_WRITE
    assert reg["shell.run"].risk == RISK_EXEC
    assert reg["http.fetch"].risk == RISK_NET
    assert APPROVAL_RISKS == frozenset({RISK_WRITE, RISK_EXEC, RISK_NET})
    for spec in reg.values():
        assert spec.description and spec.schema and callable(spec.handler)


def test_validate_args_rejects_bad_calls(ctx):
    write_schema = ctx["registry"]["fs.write"].schema
    assert validate_args(write_schema, {"path": "/x", "content": "hi"}) == []
    errors = validate_args(write_schema, {"content": "hi"})
    assert any("required" in e and "path" in e for e in errors)
    errors = validate_args(write_schema, {"path": "/x", "surprise": 1})
    assert any("unexpected property" in e for e in errors)
    errors = validate_args(write_schema, {"path": 123})
    assert any("expected type" in e for e in errors)

    shell_schema = ctx["registry"]["shell.run"].schema
    assert validate_args(shell_schema, {"command": "ls"}) == []
    assert validate_args(shell_schema, {"command": "ls", "timeout": 5}) == []
    assert any("minimum" in e for e in validate_args(shell_schema, {"command": "ls", "timeout": 0}))
    assert any("maximum" in e for e in validate_args(shell_schema, {"command": "ls", "timeout": 61}))


def test_execute_tool_unknown_and_schema_failure_never_run(ctx):
    settings = ctx["settings"]
    result = execute_tool(ctx["registry"], settings, None, "does.not.exist",
                          {"path": "/x"})
    assert result["ok"] is False and "unknown tool" in result["error"]
    # Wrong-typed args fail schema validation before any read happens.
    target = ctx["root"] / "exists.txt"
    target.write_text("data", encoding="utf-8")
    result = execute_tool(ctx["registry"], settings, None, "fs.read", {"path": 99})
    assert result["ok"] is False
    assert "schema validation failed" in result["error"]
    assert "output" not in result  # the handler never ran


def test_execute_tool_read_runs_without_approval(ctx):
    file_path = ctx["root"] / "hello.txt"
    file_path.write_text("hello world", encoding="utf-8")
    result = execute_tool(ctx["registry"], ctx["settings"], None,
                          "fs.read", {"path": str(file_path)})
    assert result["ok"] is True
    assert result["output"]["content"] == "hello world"
    assert result["output"]["bytes"] == 11


# -- fs sandbox containment ----------------------------------------------------

def test_fs_list_and_write_and_read_back(ctx):
    reg, settings, manager = ctx["registry"], ctx["settings"], ctx["manager"]
    res = execute_tool(reg, settings, None, "fs.list", {"path": str(ctx["root"])})
    assert res["ok"] is True and res["output"]["entries"] == []
    t = decide_later(manager, True)
    res = execute_tool(reg, settings, manager, "fs.write",
                       {"path": str(ctx["root"] / "n.txt"), "content": "note"},
                       approval_timeout_s=2.0)
    t.join(timeout=5)
    assert res["ok"] is True and res["output"]["written"] is True
    res = execute_tool(reg, settings, None, "fs.list", {"path": str(ctx["root"])})
    assert [e["name"] for e in res["output"]["entries"]] == ["n.txt"]
    res = execute_tool(reg, settings, None, "fs.read", {"path": str(ctx["root"] / "n.txt")})
    assert res["ok"] is True and res["output"]["content"] == "note"


def test_fs_write_creates_parent_dirs_inside_root(ctx):
    settings = ctx["settings"]
    target = ctx["root"] / "a" / "b" / "deep.txt"
    t = decide_later(ctx["manager"], True)
    result = execute_tool(ctx["registry"], settings, ctx["manager"], "fs.write",
                          {"path": str(target), "content": "deep"},
                          approval_timeout_s=2.0)
    t.join(timeout=5)
    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == "deep"
    assert settings.roots()[0].resolve() in target.resolve().parents


def test_traversal_and_absolute_outside_refused(ctx):
    settings = ctx["settings"]
    for bad in (str(ctx["root"] / ".." / ".." / "etc" / "passwd"), "/etc/passwd"):
        result = execute_tool(ctx["registry"], settings, None, "fs.read", {"path": bad})
        assert result["ok"] is False, bad
        assert "escapes" in result["error"] or "root" in result["error"], result["error"]


def test_relative_path_refused(ctx):
    result = execute_tool(ctx["registry"], ctx["settings"], None,
                          "fs.read", {"path": "relative/path.txt"})
    assert result["ok"] is False and "absolute" in result["error"]


def test_symlink_escape_refused(ctx, tmp_path):
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = ctx["root"] / "innocent.txt"
    link.symlink_to(outside)
    result = execute_tool(ctx["registry"], ctx["settings"], None,
                          "fs.read", {"path": str(link)})
    assert result["ok"] is False
    assert "escape" in result["error"] or "root" in result["error"], result["error"]

    write_link = ctx["root"] / "write-link.txt"
    write_link.symlink_to(outside)
    result = execute_tool(ctx["registry"], ctx["settings"], None,
                          "fs.write", {"path": str(write_link), "content": "x"})
    assert result["ok"] is False
    assert outside.read_text(encoding="utf-8") == "secret"  # untouched


def test_sensitive_paths_refused(ctx):
    settings = ctx["settings"]
    (ctx["root"] / ".env").write_text("API_KEY=x", encoding="utf-8")
    (ctx["root"] / ".env.production").write_text("API_KEY=y", encoding="utf-8")
    (ctx["root"] / ".ssh").mkdir()
    (ctx["root"] / ".ssh" / "config").write_text("Host x", encoding="utf-8")
    (ctx["root"] / "id_rsa").write_text("PRIVATE", encoding="utf-8")
    (ctx["root"] / "keypair.pem").write_text("PRIVATE", encoding="utf-8")
    for name in (".env", ".env.production", ".ssh/config", "id_rsa", "keypair.pem"):
        result = execute_tool(ctx["registry"], settings, None, "fs.read",
                              {"path": str(ctx["root"] / name)})
        assert result["ok"] is False, name
        message = result["error"]
        assert "sensitive" in message or "private key" in message, (name, message)
    # Reads of a path that merely contains a sensitive component are refused.
    (ctx["root"] / ".ssh").mkdir(exist_ok=True)
    result = execute_tool(ctx["registry"], settings, None, "fs.read",
                          {"path": str(ctx["root"] / ".ssh" / "config")})
    assert result["ok"] is False


def test_sensitive_write_refused_without_side_effect(ctx):
    result = execute_tool(ctx["registry"], ctx["settings"], None, "fs.write",
                          {"path": str(ctx["root"] / ".env"), "content": "x=1"})
    assert result["ok"] is False and "sensitive" in result["error"]
    assert not (ctx["root"] / ".env").exists()


def test_binary_read_refused(ctx):
    binary = ctx["root"] / "blob.bin"
    binary.write_bytes(b"\x00\x01\x02text")
    result = execute_tool(ctx["registry"], ctx["settings"], None,
                          "fs.read", {"path": str(binary)})
    assert result["ok"] is False and "binary" in result["error"]


# -- calendar ------------------------------------------------------------------

def test_calendar_write_and_read_round_trip(ctx):
    target = ctx["root"] / "demo.ics"
    t = decide_later(ctx["manager"], True)
    result = execute_tool(ctx["registry"], ctx["settings"], ctx["manager"],
                          "calendar.write", {"path": str(target), "content": VALID_ICS},
                          approval_timeout_s=2.0)
    t.join(timeout=5)
    assert result["ok"] is True and result["output"]["format"] == "ics"
    result = execute_tool(ctx["registry"], ctx["settings"], None, "calendar.read",
                          {"path": str(target)})
    assert result["ok"] is True and result["output"]["content"] == VALID_ICS


def test_calendar_invalid_ics_refused(ctx):
    # Content validation is a handler rule, exercised directly (the approval
    # gate on calendar.write is covered by the denied side-effect test above).
    handler = ctx["registry"]["calendar.write"].handler
    for content in ("this is not a calendar", "BEGIN:VCALENDAR\nno end"):
        target = ctx["root"] / "bad.ics"
        with pytest.raises(sandbox_mod.ToolError) as exc:
            handler(ctx["settings"], {"path": str(target), "content": content})
        assert "ICS" in str(exc.value) or "VCALENDAR" in str(exc.value)
        assert not target.exists()


def test_calendar_write_denied_leaves_no_file(ctx):
    settings = ctx["settings"]
    target = ctx["root"] / "denied.ics"
    t = decide_later(ctx["manager"], False)
    result = execute_tool(ctx["registry"], settings, ctx["manager"], "calendar.write",
                          {"path": str(target), "content": VALID_ICS},
                          approval_timeout_s=2.0)
    t.join(timeout=5)
    assert result["ok"] is False and "denied" in result["error"]
    assert not target.exists()
    assert ctx["manager"].get(result["approval_id"])["status"] == "denied"


# -- shell ---------------------------------------------------------------------

def test_shell_disabled_refuses_and_never_runs(tmp_path):
    marker = tmp_path / "should-not-exist.txt"
    settings, root = settings_with(tmp_path, allow_shell=False)
    reg = build_registry(settings)
    result = execute_tool(reg, settings, None, "shell.run",
                          {"command": "touch %s" % str(marker)})
    assert result["ok"] is False
    assert "disabled" in result["error"] and "allow_shell" in result["error"]
    assert not marker.exists()  # the command never ran


def test_shell_enabled_approval_required_then_runs(ctx):
    settings = ctx["settings"]
    settings.allow_shell = True
    t = decide_later(ctx["manager"], True)
    result = execute_tool(ctx["registry"], settings, ctx["manager"], "shell.run",
                          {"command": "printf hello"}, approval_timeout_s=2.0)
    t.join(timeout=5)
    assert result["ok"] is True
    assert result["output"]["exit_code"] == 0
    assert result["output"]["stdout"] == "hello"


def test_shell_runs_without_shell_interpretation(ctx):
    settings = ctx["settings"]
    settings.allow_shell = True
    real_home = os.environ.get("HOME", "")
    t = decide_later(ctx["manager"], True)
    result = execute_tool(ctx["registry"], settings, ctx["manager"], "shell.run",
                          {"command": "echo $HOME"}, approval_timeout_s=2.0)
    t.join(timeout=5)
    assert result["ok"] is True
    # The token reaches the program literally: no shell to expand $HOME.
    assert result["output"]["stdout"].strip() == "$HOME"
    assert real_home not in result["output"]["stdout"]


def test_shell_enabled_sees_nonzero_exit(ctx):
    settings = ctx["settings"]
    settings.allow_shell = True
    t = decide_later(ctx["manager"], True)
    result = execute_tool(ctx["registry"], settings, ctx["manager"], "shell.run",
                          {"command": "sh -c 'exit 3'"}, approval_timeout_s=2.0)
    t.join(timeout=5)
    assert result["ok"] is True
    assert result["output"]["exit_code"] == 3


# -- http allowlist ------------------------------------------------------------

def test_url_policy_scans(tmp_path):
    ok = sandbox_mod.check_url_policy
    allowed = ["example.com"]
    ok("https://example.com/", allowed)
    ok("https://example.com:443/x", allowed)
    ok("https://EXAMPLE.com/", allowed)  # host is normalized to lowercase
    with pytest.raises(sandbox_mod.ToolError, match="https only"):
        ok("http://example.com/", allowed)
    with pytest.raises(sandbox_mod.ToolError, match="allowlist"):
        ok("https://sub.example.com/", allowed)    # subdomain is not exact
    with pytest.raises(sandbox_mod.ToolError, match="allowlist"):
        ok("https://notexample.com/", allowed)     # lookalike suffix is not exact
    with pytest.raises(sandbox_mod.ToolError, match="credentials"):
        ok("https://user:pass@example.com/", [h.lower() for h in allowed])


def test_fetch_allowlisted_local_https_ok(tmp_path):
    server, port = start_https_stub(tmp_path)
    try:
        settings, root = settings_with(tmp_path, http_allowlist=["127.0.0.1"],
                                       tls_context=insecure_tls())
        reg = build_registry(settings)
        manager = ApprovalManager(settings.approval_db, ttl_s=6,
                                  log_path=settings.approval_log)
        t = decide_later(manager, True)
        result = execute_tool(reg, settings, manager, "http.fetch",
                              {"url": "https://127.0.0.1:%d/data" % port},
                              approval_timeout_s=3.0)
        t.join(timeout=6)
        assert result["ok"] is True
        assert result["output"]["status"] == 200
        assert result["output"]["body"] == "stub body ok"
    finally:
        server.shutdown()
        server.server_close()


def test_fetch_outside_allowlist_refused_without_network(tmp_path):
    server, port = start_https_stub(tmp_path)
    try:
        hits_before = server.RequestHandlerClass.hits
        settings, root = settings_with(tmp_path, http_allowlist=["allowed.invalid"],
                                       tls_context=insecure_tls())
        reg = build_registry(settings)
        result = execute_tool(reg, settings, None, "http.fetch",
                              {"url": "https://127.0.0.1:%d/data" % port})
        assert result["ok"] is False
        assert "not in the fetch allowlist" in result["error"]
        assert server.RequestHandlerClass.hits == hits_before  # never connected
    finally:
        server.shutdown()
        server.server_close()


def test_fetch_redirect_to_nonallowlisted_host_refused(tmp_path):
    server, port = start_https_stub(tmp_path,
                                    redirect_url="https://not-in-allowlist.example/away")
    try:
        settings, root = settings_with(tmp_path, http_allowlist=["127.0.0.1"],
                                       tls_context=insecure_tls())
        reg = build_registry(settings)
        manager = ApprovalManager(settings.approval_db, ttl_s=6,
                                  log_path=settings.approval_log)
        t = decide_later(manager, True)
        result = execute_tool(reg, settings, manager, "http.fetch",
                              {"url": "https://127.0.0.1:%d/start" % port},
                              approval_timeout_s=3.0)
        t.join(timeout=6)
        assert result["ok"] is False
        assert "redirect refused" in result["error"]
        assert "allowlist" in result["error"]
        assert server.RequestHandlerClass.hits == 1  # only the /start hop
    finally:
        server.shutdown()
        server.server_close()


def test_fetch_follows_allowlisted_redirect(tmp_path):
    server, port = start_https_stub(tmp_path)
    server.redirect_url = "https://127.0.0.1:%d/data" % port  # same allowlisted host
    try:
        settings, root = settings_with(tmp_path, http_allowlist=["127.0.0.1"],
                                       tls_context=insecure_tls())
        reg = build_registry(settings)
        manager = ApprovalManager(settings.approval_db, ttl_s=6,
                                  log_path=settings.approval_log)
        t = decide_later(manager, True)
        result = execute_tool(reg, settings, manager, "http.fetch",
                              {"url": "https://127.0.0.1:%d/start" % port},
                              approval_timeout_s=3.0)
        t.join(timeout=6)
        assert result["ok"] is True
        assert result["output"]["status"] == 200
        assert result["output"]["body"] == "stub body ok"
    finally:
        server.shutdown()
        server.server_close()


# -- approvals: store and decisions ---------------------------------------------

def test_approval_created_with_full_row_and_hash(ctx):
    manager = ctx["manager"]
    rec = manager.create("fs.write", {"path": "/tmp/x", "content": "hi"}, "write")
    assert rec["status"] == "pending"
    assert rec["tool"] == "fs.write" and rec["risk"] == "write"
    assert rec["args"] == {"path": "/tmp/x", "content": "hi"}
    assert rec["args_hash"] == args_hash({"path": "/tmp/x", "content": "hi"})
    assert set(rec) >= {"id", "tool", "args", "risk", "created_at", "expires_at",
                        "decision"}
    refetched = manager.get(rec["id"])
    assert refetched["status"] == "pending" and refetched["decision"] is None
    # pending list shows it; decided list does not
    assert [a["id"] for a in manager.list_all("pending")] == [rec["id"]]
    assert manager.list_all("approved") == []


def test_approval_approve_and_deny(ctx):
    manager = ctx["manager"]
    rec = manager.create("fs.write", {"path": "/a"}, "write")
    out = manager.decide(rec["id"], True, decided_by="human-1")
    assert out["ok"] is True and out["approval"]["status"] == "approved"
    assert out["approval"]["decided_by"] == "human-1"
    assert out["approval"]["args_hash"] == rec["args_hash"]

    rec2 = manager.create("http.fetch", {"url": "https://x"}, "net")
    out = manager.decide(rec2["id"], False)
    assert out["ok"] is True and out["approval"]["status"] == "denied"


def test_approval_wait_wakes_on_decide(ctx):
    manager = ctx["manager"]
    rec = manager.create("fs.write", {"path": "/a"}, "write")
    result = {}
    thread = threading.Thread(target=lambda: result.update(
        decision=manager.wait(rec["id"], 5.0)), daemon=True)
    thread.start()
    time.sleep(0.05)
    manager.decide(rec["id"], True, decided_by="prompt-user")
    thread.join(timeout=5)
    assert result == {"decision": ("approved", "user")}


def test_approval_timeout_denies(ctx):
    manager = ctx["manager"]
    rec = manager.create("fs.write", {"path": "/a"}, "write")
    decision, reason = manager.wait(rec["id"], 0.3)
    assert decision == "denied" and reason == "timeout"
    assert manager.get(rec["id"])["status"] == "denied"


def test_approval_expiry_denies(ctx):
    manager = ctx["manager"]
    manager.create("shell.run", {"command": "ls"}, "exec")
    # A fresh manager with a 1-second ttl drives the expiry path fast.
    short = ApprovalManager(str(ctx["tmp"] / "short.db"), ttl_s=1,
                            log_path=str(ctx["tmp"] / "short.log"))
    row = short.create("fs.write", {"path": "/a"}, "write")
    time.sleep(1.1)
    assert short.get(row["id"])["status"] == "expired"
    out = short.decide(row["id"], True)  # an expired pending auto-denies
    assert out["ok"] is True and "expired" in out.get("note", "")
    assert short.get(row["id"])["status"] == "denied"
    assert short.get(row["id"])["reason"] == "expired"


def test_approval_unknown_and_double_decide(ctx):
    manager = ctx["manager"]
    assert manager.decide(9999, True)["ok"] is False
    rec = manager.create("fs.write", {"path": "/a"}, "write")
    manager.decide(rec["id"], True)
    again = manager.decide(rec["id"], True)
    assert again["ok"] is False and "already" in again["error"]
    bad = manager.decide(rec["id"], 7)
    assert bad["ok"] is False


def test_approval_decision_logged_with_args_hash(ctx):
    manager = ctx["manager"]
    rec = manager.create("fs.write", {"path": "/log-target", "content": "z"}, "write")
    manager.decide(rec["id"], False, decided_by="tester")
    log = Path(ctx["settings"].approval_log).read_text(encoding="utf-8")
    lines = [json.loads(line) for line in log.splitlines() if line.strip()]
    created = [l for l in lines if l.get("action") == "created"]
    decided = [l for l in lines if l.get("action") == "decision"]
    assert created and decided
    assert decided[0]["args_hash"] == rec["args_hash"]
    assert decided[0]["args_hash"].startswith("sha256:")
    assert decided[0]["decision"] == "denied" and decided[0]["decided_by"] == "tester"
    assert created[0]["args_hash"] == rec["args_hash"]


# -- approvals: HTTP contract ----------------------------------------------------

def test_approvals_http_contract(approvals_http, ctx):
    manager = ctx["manager"]
    port = approvals_http.server_address[1]
    rec = manager.create("calendar.write", {"path": "/cal.ics"}, "write")

    status, resp = http_json(port, "GET", "/v1/approvals")
    assert status == 200
    assert any(a["id"] == rec["id"] and a["status"] == "pending" for a in resp["approvals"])
    assert resp["pending"] >= 1

    status, resp = http_json(port, "POST", "/v1/approvals/%d" % rec["id"],
                             {"approve": True})
    assert status == 200 and resp["ok"] is True
    assert resp["approval"]["status"] == "approved"
    assert resp["approval"]["args_hash"] == rec["args_hash"]

    status, resp = http_json(port, "GET", "/v1/approvals")
    assert any(a["id"] == rec["id"] and a["status"] == "approved" for a in resp["approvals"])

    status, resp = http_json(port, "POST", "/v1/approvals/999999", {"approve": True})
    assert status == 404

    rec2 = manager.create("http.fetch", {"url": "https://x"}, "net")
    status, resp = http_json(port, "POST", "/v1/approvals/%d" % rec2["id"],
                             {"decision": "deny"})
    assert status == 200 and resp["approval"]["status"] == "denied"

    status, resp = http_json(port, "POST", "/v1/approvals/%d" % rec2["id"], {})
    assert status == 409


def test_approvals_router_contract_without_server(ctx):
    handle = approvals_router(ctx["manager"])
    rec = ctx["manager"].create("fs.write", {"path": "/x"}, "write")
    assert handle("GET", ["v1", "approvals"], None)[0] == 200
    status, payload = handle("POST", ["v1", "approvals", str(rec["id"])],
                             {"approve": "approve"})
    assert status == 200 and payload["ok"] is True
    status, payload = handle("GET", ["nope"], None)
    assert status == 404


# -- execute_tool approval gating (no side effects on deny) ---------------------

def test_write_approved_runs(ctx):
    target = ctx["root"] / "approved.txt"
    t = decide_later(ctx["manager"], True)
    result = execute_tool(ctx["registry"], ctx["settings"], ctx["manager"],
                          "fs.write", {"path": str(target), "content": "made"},
                          approval_timeout_s=2.0)
    t.join(timeout=5)
    assert result["ok"] is True
    assert result["approval_id"]
    assert target.read_text(encoding="utf-8") == "made"


def test_write_denied_leaves_no_side_effect(ctx):
    target = ctx["root"] / "denied.txt"
    t = decide_later(ctx["manager"], False)
    result = execute_tool(ctx["registry"], ctx["settings"], ctx["manager"],
                          "fs.write", {"path": str(target), "content": "nope"},
                          approval_timeout_s=2.0)
    t.join(timeout=5)
    assert result["ok"] is False and "denied" in result["error"]
    assert not target.exists()
    assert ctx["manager"].get(result["approval_id"])["status"] == "denied"


def test_write_timeout_denies_and_leaves_no_side_effect(ctx):
    target = ctx["root"] / "timed.txt"
    result = execute_tool(ctx["registry"], ctx["settings"], ctx["manager"],
                          "fs.write", {"path": str(target), "content": "x"},
                          approval_timeout_s=0.2)
    assert result["ok"] is False
    assert "denied" in result["error"] and "timeout" in result["error"]
    assert not target.exists()


def test_write_without_approval_manager_refused(ctx):
    target = ctx["root"] / "no-manager.txt"
    result = execute_tool(ctx["registry"], ctx["settings"], None,
                          "fs.write", {"path": str(target), "content": "x"})
    assert result["ok"] is False
    assert "no approval manager" in result["error"]
    assert not target.exists()


# -- loop -----------------------------------------------------------------------

def test_loop_model_tool_model_returns_final_text(ctx):
    settings = ctx["settings"]
    file_path = ctx["root"] / "notes.txt"
    file_path.write_text("buy milk", encoding="utf-8")
    runtime = ScriptedRuntime([
        tool_call("fs.read", {"path": str(file_path)}),
        "The file says: buy milk.",
    ])
    final = run_loop(runtime, ctx["registry"], settings, None,
                     [{"role": "user", "content": "read the file"}],
                     system_prompt="test")
    assert final == "The file says: buy milk."
    # The second model call carried the tool result as role=tool.
    second_call = runtime.calls[1]
    assert second_call[-1]["role"] == "tool"
    tool_payload = json.loads(second_call[-1]["content"])
    assert tool_payload["ok"] is True and tool_payload["tool"] == "fs.read"
    assert tool_payload["output"]["content"] == "buy milk"
    # The user message is still first in the thread, system prompt included.
    assert runtime.calls[1][0]["role"] == "system"


def test_loop_hop_cap_stops_after_four(ctx):
    settings = ctx["settings"]
    file_path = ctx["root"] / "cap.txt"
    file_path.write_text("x", encoding="utf-8")
    orig = ctx["registry"]["fs.read"].handler
    counter = {"runs": 0}

    def counting(settings, args):
        counter["runs"] += 1
        return orig(settings, args)

    reg = dict(ctx["registry"])
    reg["fs.read"] = dataclasses.replace(reg["fs.read"], handler=counting)
    runtime = ScriptedRuntime([tool_call("fs.read", {"path": str(file_path)})])
    final = run_loop(runtime, reg, settings, None,
                     [{"role": "user", "content": "loop forever"}],
                     system_prompt="test")
    assert counter["runs"] == HOP_CAP == 4      # exactly four executions
    assert final == tool_call("fs.read", {"path": str(file_path)})  # model's 6th reply
    # The hop-limit tool message is the last message of the final model call.
    last_messages = runtime.calls[-1]
    assert last_messages[-1]["role"] == "tool"
    assert "__hop_limit" in last_messages[-1]["content"]


def test_loop_denied_approval_injects_result_and_tool_never_ran(ctx):
    settings = ctx["settings"]
    target = ctx["root"] / "loop-denied.txt"
    runtime = ScriptedRuntime([
        tool_call("fs.write", {"path": str(target), "content": "x"}),
        "I could not write that file.",
    ])
    t = decide_later(ctx["manager"], False)
    final = run_loop(runtime, ctx["registry"], settings, ctx["manager"],
                     [{"role": "user", "content": "write the file"}],
                     system_prompt="test", approval_timeout_s=2.0)
    t.join(timeout=5)
    assert final == "I could not write that file."
    assert not target.exists()  # denied: no side effect anywhere
    tool_payload = json.loads(runtime.calls[1][-1]["content"])
    assert tool_payload["ok"] is False and "denied" in tool_payload["error"]


def test_loop_approved_write_runs(ctx):
    settings = ctx["settings"]
    target = ctx["root"] / "loop-approved.txt"
    runtime = ScriptedRuntime([
        tool_call("fs.write", {"path": str(target), "content": "yes"}),
        "Done.",
    ])
    t = decide_later(ctx["manager"], True)
    final = run_loop(runtime, ctx["registry"], settings, ctx["manager"],
                     [{"role": "user", "content": "save it"}],
                     system_prompt="test", approval_timeout_s=2.0)
    t.join(timeout=5)
    assert final == "Done."
    assert target.read_text(encoding="utf-8") == "yes"
    tool_payload = json.loads(runtime.calls[1][-1]["content"])
    assert tool_payload["ok"] is True


def test_loop_timeout_deny_blocks_briefly_then_finalizes(ctx):
    settings = ctx["settings"]
    target = ctx["root"] / "loop-timed.txt"
    runtime = ScriptedRuntime([
        tool_call("fs.write", {"path": str(target), "content": "x"}),
        "The write was not approved, moving on.",
    ])
    start = time.monotonic()
    final = run_loop(runtime, ctx["registry"], settings, ctx["manager"],
                     [{"role": "user", "content": "write"}],
                     system_prompt="test", approval_timeout_s=0.2)
    elapsed = time.monotonic() - start
    assert final == "The write was not approved, moving on."
    assert not target.exists()
    assert elapsed < 5  # it blocked briefly for the approval, then denied


def test_loop_unknown_tool_and_schema_failure_are_results(ctx):
    settings = ctx["settings"]
    runtime = ScriptedRuntime([
        tool_call("not.a.tool", {}),
        '{"tool": "fs.read", "args": {"path": 123}}',
        "final answer",
    ])
    final = run_loop(runtime, ctx["registry"], settings, None,
                     [{"role": "user", "content": "go"}], system_prompt="test")
    assert final == "final answer"
    tool_roles = [json.loads(m["content"]) for m in runtime.calls[2]
                  if m["role"] == "tool"]
    assert tool_roles[0]["ok"] is False and "unknown tool" in tool_roles[0]["error"]
    assert tool_roles[1]["ok"] is False and "schema validation failed" in tool_roles[1]["error"]


def test_extract_tool_call_variants():
    assert extract_tool_call('{"tool": "fs.list", "args": {"path": "/a"}}') == \
        ("fs.list", {"path": "/a"})
    assert extract_tool_call("read the file please") is None
    assert extract_tool_call("```json\n{\"tool\": \"fs.read\", \"args\": {\"path\": \"/b\"}}\n```") == \
        ("fs.read", {"path": "/b"})
    assert extract_tool_call('{"tool": "fs.read", "args": "nope"}') is None
    assert extract_tool_call("{not json") is None
    assert extract_tool_call("") is None


def test_system_prompt_describes_every_tool(ctx):
    from steward.tools.loop import tool_system_prompt
    prompt = tool_system_prompt(ctx["registry"])
    for name in sorted(ctx["registry"]):
        assert name in prompt
    assert "fs.list" in prompt and "approve" in prompt.lower()
