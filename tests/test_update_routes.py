"""Daemon update routes: GET /v1/update/check and POST /v1/update/apply.

Covers the full daemon surface of the OTA card: bearer gating (mutating
routes require a token, like every other mutating route), a real end-to-end
one-click apply against a loopback update source (download, verify, apply,
probe), a tampered-manifest refusal, and the startup check recording a
status when the source is unreachable instead of failing the daemon. The
daemon boots normally in every test, so an unreachable or tampered source
is never a failed unit.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import threading
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from steward import license as lic  # noqa: E402
from steward import ota as ota_mod    # noqa: E402
from steward.server import auth as auth_mod   # noqa: E402
from steward.server import daemon as daemon_mod  # noqa: E402


class FakeRuntime:
    """Minimal stand-in for the steward.runtime seam (no Ollama, no network)."""

    kind = "fake-runtime"
    url = "http://127.0.0.1:1"

    def health(self) -> dict:
        return {"ok": True, "backend": "fake", "version": "0.0.0-fake",
                "models": ["fake-model"]}

    def chat(self, messages, stream=True, model=None):
        if not stream:
            yield {"delta": "hi"}
            return
        for token in ("hi ", "there."):
            yield {"delta": token}


@pytest.fixture
def env(tmp_path):
    db_path = tmp_path / "db" / "steward.db"
    return {"tmp": tmp_path, "db_path": db_path,
            "secret_file": tmp_path / "db" / "pairing.key"}


@pytest.fixture
def start_daemon(env):
    started = []

    def start(runtime=None, advertise=False, **kw):
        srv = daemon_mod.create_server(
            ("127.0.0.1", 0), runtime=runtime or FakeRuntime(),
            db_path=env["db_path"],
            secret_file=env["secret_file"], advertise=advertise, **kw)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        started.append((srv, thread))
        return srv, srv.server_address[1]

    yield start
    for srv, thread in started:
        srv.shutdown_server()
        thread.join(timeout=5)


# -- HTTP + signing helpers -----------------------------------------------


def request(port, method, path, body=None, token=None, headers=None):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    hdrs = {"Content-Type": "application/json"}
    if token:
        hdrs["Authorization"] = "Bearer " + token
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


def pair_token(srv) -> str:
    """A valid bearer token for the running daemon."""
    return auth_mod.issue_token(srv.ctx.secret, "test-device")[0]


@pytest.fixture
def vendor(monkeypatch):
    pem, pub = lic.generate_keypair()
    monkeypatch.setattr(lic, "VENDOR_PUBLIC_KEY_HEX", pub)
    return {"pub": pub, "priv": lic.private_bytes_from_pem(pem)}


@pytest.fixture(autouse=True)
def no_global_models(monkeypatch):
    """Route tests exercise the app payload and the pointer, not the catalog
    select. Applying via the daemon would swap the process-global in-memory
    catalog and poison later selector/wizard tests, so the model install is a
    recorded no-op here."""

    monkeypatch.setattr(ota_mod, "_install_models",
                        lambda entries: [m["id"] for m in entries])


def app_block(version="1.1.0", key=None, payload=None) -> dict:
    return {
        "version": version,
        "released": "2026-10-28",
        "notes": "route-test app update",
        "min_source_version": "1.0.0",
        "payloads": {key or "linux-aarch64": payload or {
            "url": "steward-app.bin", "sha256": "0" * 64, "size": 0}},
    }


def manifest_doc(vendor, version="c1", **extra) -> dict:
    payload = {
        "version": version,
        "released": "2026-10-28",
        "app_version": "1.1.0",
        "models": [{
            "id": "qwen-x:7b", "family": "Test", "params_b": 7.0,
            "active_b": 7.0, "q4_gb": 5.0, "tok_range": [20, 100],
            "license": "Apache-2.0", "kind": "dense", "note": "route test",
        }],
        "note": "signed route-test manifest",
    }
    payload.update(extra)
    return lic.make_envelope(payload, vendor["priv"])


def make_app_binary(path: Path, version: str) -> bytes:
    body = ("#!/usr/bin/env python3\nimport sys\n"
            "print('steward %s')\n" % version)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path.read_bytes()


class _RoutesHandler(http.server.BaseHTTPRequestHandler):
    routes: dict = {}

    def log_message(self, format, *args):  # quiet the test server
        pass

    def do_GET(self):  # noqa: N802
        body = self.routes.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(routes: dict):
    _RoutesHandler.routes = routes
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RoutesHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, "http://127.0.0.1:%d" % server.server_address[1]


# -- route gating -----------------------------------------------------------


def test_check_and_apply_require_bearer(start_daemon):
    srv, port = start_daemon()
    status, body = request(port, "GET", "/v1/update/check")
    assert status == 401
    status, body = request(port, "POST", "/v1/update/apply")
    assert status == 401
    # The same routes answer when the token is present (even with a source
    # that cannot resolve: the refusal is a body, not a 401 gate change).
    status, _ = request(port, "GET", "/v1/update/check", token=pair_token(srv))
    assert status == 200


# -- /v1/update/check ---------------------------------------------------------


def test_check_reports_update_over_http(start_daemon, vendor, monkeypatch, env):
    key = ota_mod.platform_key(env["tmp"])
    blob = make_app_binary(env["tmp"] / "steward-app.bin", "1.3.0")
    doc = manifest_doc(vendor, app=app_block(
        version="1.3.0", key=key, payload={"url": "steward-app.bin",
                                           "sha256": hashlib.sha256(blob).hexdigest(),
                                           "size": len(blob)}))
    server, base = serve({
        "/manifest.json": json.dumps(doc, sort_keys=True).encode("utf-8"),
        "/steward-app.bin": blob,
    })
    srv, port = start_daemon()
    srv.ctx.ota_source = base + "/manifest.json"
    status, body = request(port, "GET", "/v1/update/check", token=pair_token(srv))
    assert status == 200
    assert body["ok"] is True
    assert body["state"] == "ok"
    assert body["app"]["latest_version"] == "1.3.0"
    assert body["app"]["available"] is True
    assert body["catalog"]["latest_version"] == "c1"
    # The check is recorded on the daemon as well.
    assert srv.ctx.update_status.get("state") == "ok"
    server.shutdown()


def test_check_unreachable_source_is_a_normal_status(start_daemon):
    srv, port = start_daemon()
    srv.ctx.ota_source = "http://127.0.0.1:1/nope.json"   # nothing listens
    status, body = request(port, "GET", "/v1/update/check", token=pair_token(srv))
    assert status == 200
    assert body["ok"] is False
    assert body["state"] == "error"
    assert "error" in body
    # The daemon itself is fine: same process still answers /health and the
    # API, so an unreachable source never breaks the unit.
    hstatus, hbody = request(port, "GET", "/health")
    assert hstatus == 200
    assert hbody["status"] == "ok"
    assert srv.ctx.update_status.get("state") == "error"


def test_startup_check_records_status_and_never_raises(start_daemon):
    srv, _port = start_daemon()
    srv.ctx.ota_source = "http://127.0.0.1:1/unreachable.json"
    result = daemon_mod.run_startup_update_check(srv.ctx)   # must not raise
    assert result["state"] == "error"
    assert not result.get("ok")
    assert srv.ctx.update_status["state"] == "error"
    assert srv.ctx.update_status.get("checked_at")


# -- /v1/update/apply ---------------------------------------------------------


def test_apply_end_to_end_one_click(start_daemon, vendor, env):
    key = ota_mod.platform_key(env["tmp"])
    blob = make_app_binary(env["tmp"] / "steward-app.bin", "1.1.0")
    doc = manifest_doc(vendor, app=app_block(
        key=key, payload={"url": "steward-app.bin",
                          "sha256": hashlib.sha256(blob).hexdigest(),
                          "size": len(blob)}))
    server, base = serve({
        "/manifest.json": json.dumps(doc, sort_keys=True).encode("utf-8"),
        "/steward-app.bin": blob,
    })
    srv, port = start_daemon()
    srv.ctx.ota_source = base + "/manifest.json"

    status, body = request(port, "POST", "/v1/update/apply", token=pair_token(srv))
    assert status == 200
    assert body["ok"] is True
    assert body["state"] == "applied"
    assert body["app"]["installed"] is True
    assert body["app"]["version"] == "1.1.0"
    # The pointer now records the applied app version.
    st = ota_mod.current_state(env["tmp"])
    assert st["app"]["version"] == "1.1.0"
    # A second check reports nothing new is available.
    status, body = request(port, "GET", "/v1/update/check", token=pair_token(srv))
    assert body["app"]["available"] is False
    server.shutdown()


def test_apply_refuses_tampered_manifest(start_daemon, vendor, env):
    key = ota_mod.platform_key(env["tmp"])
    blob = make_app_binary(env["tmp"] / "steward-app.bin", "9.9.9")
    doc = manifest_doc(vendor, app=app_block(
        version="9.9.9", key=key,
        payload={"url": "steward-app.bin",
                 "sha256": hashlib.sha256(blob).hexdigest(),
                 "size": len(blob)}))
    tampered = json.loads(json.dumps(doc))
    tampered["payload"]["app"]["version"] = "9.9.9-evil"   # no re-sign

    server, base = serve({
        "/manifest.json": json.dumps(tampered, sort_keys=True).encode("utf-8"),
        "/steward-app.bin": blob,
    })
    srv, port = start_daemon()
    srv.ctx.ota_source = base + "/manifest.json"

    status, body = request(port, "POST", "/v1/update/apply", token=pair_token(srv))
    assert status == 200
    assert body["ok"] is False
    assert body["state"] == "refused"
    assert "signature" in body["error"].lower()
    # Nothing was applied: no app entry, no catalog file.
    assert ota_mod.current_state(env["tmp"]) is None
    server.shutdown()


# -- apply progress (SSE), manual command, and restart intent -----------------


def make_big_binary(path: Path, version: str, bytes: int) -> bytes:
    """A large, *valid* steward-like payload: NUL-padding would make the
    health probe fail (Python rejects NUL in source), which is not what this
    test wants. The padding ride in a comment so the probe still runs."""
    body = (b"#!/usr/bin/env python3\nimport sys\n"
            b"# pad " + b"x" * max(0, bytes) + b"\n"
            b"print('steward %s')\n" % version.encode())
    path.write_bytes(body)
    path.chmod(0o755)
    return path.read_bytes()


def test_apply_streams_sse_progress(start_daemon, vendor, env):
    """A client asking for text/event-stream gets real download progress
    events plus a final result, not a single JSON blob at the end."""
    key = ota_mod.platform_key(env["tmp"])
    # A payload bigger than the 1 MiB read chunk so progress is observable,
    # while still being a runnable binary the health probe accepts.
    blob = make_big_binary(env["tmp"] / "steward-app.bin", "1.1.0",
                           2 * 1024 * 1024)
    import hashlib as _h
    doc = manifest_doc(vendor, app=app_block(
        version="1.1.0", key=key, payload={"url": "steward-app.bin",
                                           "sha256": _h.sha256(blob).hexdigest(),
                                           "size": len(blob)}))
    server, base = serve({
        "/manifest.json": json.dumps(doc, sort_keys=True).encode("utf-8"),
        "/steward-app.bin": blob,
    })
    srv, port = start_daemon()
    srv.ctx.ota_source = base + "/manifest.json"

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    conn.request("POST", "/v1/update/apply",
                 headers={"Authorization": "Bearer " + pair_token(srv),
                          "Accept": "text/event-stream"})
    resp = conn.getresponse()
    data = resp.read().decode("utf-8", "replace")
    conn.close()

    assert resp.status == 200
    assert resp.getheader("Content-Type", "").startswith("text/event-stream")
    events = [line[6:].strip() for line in data.splitlines()
              if line.startswith("data: ") and line != "data: [DONE]"]
    payloads = [json.loads(e) for e in events[:-1]]
    # A progress event with numbers arrived, plus a phase apply.
    progress = [p for p in payloads if p.get("event") == "progress"]
    assert progress and progress[0]["total"] == len(blob)
    assert any(p.get("event") == "phase"
               and p.get("name") == "apply" for p in payloads)
    result = payloads[-1]["result"]
    assert result["ok"] is True and result["state"] == "applied"
    server.shutdown()


def test_apply_reports_restart_intent_and_manual_command(start_daemon, vendor, env):
    """On an applied app swap the route tells the client the daemon is about
    to be replaced (reconnecting state, not a dead screen) and always carries
    the manual command as the escape hatch."""
    key = ota_mod.platform_key(env["tmp"])
    blob = make_app_binary(env["tmp"] / "steward-app.bin", "1.1.1")
    doc = manifest_doc(vendor, app=app_block(
        version="1.1.1", key=key, payload={"url": "steward-app.bin",
                                           "sha256": hashlib.sha256(blob).hexdigest(),
                                           "size": len(blob)}))
    server, base = serve({
        "/manifest.json": json.dumps(doc, sort_keys=True).encode("utf-8"),
        "/steward-app.bin": blob,
    })
    srv, port = start_daemon()
    srv.ctx.ota_source = base + "/manifest.json"

    starred = []
    srv.ctx.restart_app_fn = lambda result: starred.append(result)

    status, body = request(port, "POST", "/v1/update/apply", token=pair_token(srv))
    assert status == 200
    assert body["state"] == "applied"
    assert body["restart_intent"]["pending"] is True
    assert body["restart_intent"]["version"] == "1.1.1"
    assert "steward update apply" in body["manual_command"]
    assert body["manual_command"] == body["restart_intent"]["manual"]
    # The restart hook fired on a background thread (allow one moment).
    import time as _t
    deadline = _t.time() + 3
    while not starred and _t.time() < deadline:
        _t.sleep(0.05)
    assert starred, "restart_app_fn never called"
    server.shutdown()


def test_apply_offer_restart_intent_without_function_is_honest(
        start_daemon, vendor, env):
    """Without a restart hook (tests, embedded daemons) the client is still
    told restart_intent so it can reconnect; nothing is exec'd."""
    key = ota_mod.platform_key(env["tmp"])
    blob = make_app_binary(env["tmp"] / "steward-app.bin", "1.3.0")
    doc = manifest_doc(vendor, app=app_block(
        version="1.3.0", key=key, payload={"url": "steward-app.bin",
                                           "sha256": hashlib.sha256(blob).hexdigest(),
                                           "size": len(blob)}))
    server, base = serve({
        "/manifest.json": json.dumps(doc, sort_keys=True).encode("utf-8"),
        "/steward-app.bin": blob,
    })
    srv, port = start_daemon()
    srv.ctx.ota_source = base + "/manifest.json"

    status, body = request(port, "POST", "/v1/update/apply", token=pair_token(srv))
    assert status == 200
    assert body["state"] == "applied"
    assert body["restart_intent"]["pending"] is True
    server.shutdown()


def test_build_restart_argv_keeps_serve_flags():
    argv = daemon_mod.build_restart_argv(
        "/data/ota/app/steward-new", ["--port", "3891", "--model", "qwen3:8b"])
    assert argv[0] == "/data/ota/app/steward-new"
    assert argv[1] == "serve"
    assert "--port" in argv and "3891" in argv
