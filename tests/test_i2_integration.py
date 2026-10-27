"""I2 integration: all daemon route families coexist on the merged tree.

This is the I2 collision proof the merge card promises: S5 mounts the webapp,
S8 adds /v1/briefs, S9 adds /v1/approvals, S12 adds /doctor/bundle, and the
S2 base routes (/health, /v1/bundle, /v1/chat, /v1/conversations) all still
answer from the same handler. Every test boots a real StewardServer on port 0
with a fake runtime and tears it down, so no daemon or port survives.

Under test:

  1. Open routes: /health, /v1/bundle, and the webapp root all answer without
     a token.
  2. /v1/briefs lists stored briefs and /v1/briefs/today returns today's
     brief (S8 memory store through the daemon).
  3. /v1/approvals lists pending + decided and POST /v1/approvals/{id}
     decides a row (S9 approvals through the daemon contract).
  4. /doctor/bundle builds a redacted support bundle and reports members
     (S12 diagnostics through the daemon).
  5. Every gated route rejects a missing token with 401 before running.
"""

from __future__ import annotations

import http.client
import json
import threading
import zipfile
from pathlib import Path

import pytest

from steward.server import daemon as daemon_mod


class FakeRuntime:
    kind = "fake-runtime"
    url = "http://127.0.0.1:1"

    def __init__(self):
        self.health_ok = True

    def health(self):
        return {"ok": self.health_ok, "backend": "fake", "version": "0.0.0-fake",
                "models": ["fake-model", "qwen3:8b"]}

    def list_models(self):
        return ["fake-model", "qwen3:8b"]

    def chat(self, messages, stream=True, model=None):
        if not stream:
            yield {"delta": "fake reply"}
            return
        for token in ["fake ", "reply"]:
            yield {"delta": token}


@pytest.fixture
def env(tmp_path):
    return {
        "tmp": tmp_path,
        "db_path": tmp_path / "db" / "steward.db",
        "secret_file": tmp_path / "db" / "pairing.key",
    }


@pytest.fixture
def start_daemon(env):
    started = []

    def start(runtime, **kw):
        srv = daemon_mod.create_server(
            ("127.0.0.1", 0), runtime=runtime,
            db_path=env["db_path"],
            secret_file=env["secret_file"],
            advertise=False, **kw)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        started.append((srv, thread))
        return srv, srv.server_address[1]

    yield start
    for srv, thread in started:
        srv.shutdown_server()
        thread.join(timeout=5)


def request(port, method, path, body=None, token=None, raw=False):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = {"Content-Type": "application/json"}
    if token:
        hdrs["Authorization"] = "Bearer " + token
    payload = json.dumps(body) if body is not None else None
    conn.request(method, path, body=payload, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read().decode("utf-8", "replace")
    conn.close()
    if raw:
        return resp.status, resp.getheader("Content-Type"), data
    try:
        parsed = json.loads(data) if data else {}
    except json.JSONDecodeError:
        parsed = data
    return resp.status, parsed


def pair(port):
    status, code_resp = request(port, "GET", "/pair/code")
    assert status == 200, code_resp
    status, resp = request(port, "POST", "/v1/pair",
                           {"code": code_resp["code"], "device": "i2-pytest"})
    assert status == 200, resp
    return resp["token"]


# -- open routes ----------------------------------------------------------------


def test_open_routes_need_no_token(start_daemon):
    srv, port = start_daemon(FakeRuntime())

    status, health = request(port, "GET", "/health")
    assert status == 200
    assert health["status"] == "ok"
    assert health["backend"] == "fake-runtime"

    status, bundle = request(port, "GET", "/v1/bundle")
    assert status == 200
    assert "bundle" in bundle

    # webapp root (S5 mount)
    status, ctype, body = request(port, "GET", "/", raw=True)
    assert status == 200
    assert ctype.startswith("text/html")
    assert "Steward" in body


# -- gated routes reject a missing token ---------------------------------------


def test_gated_routes_require_auth(start_daemon):
    srv, port = start_daemon(FakeRuntime())

    for path in ("/v1/briefs", "/v1/briefs/today", "/v1/approvals",
                 "/doctor/bundle"):
        status, resp = request(port, "GET", path)
        assert status == 401, (path, status, resp)

    status, resp = request(port, "POST", "/v1/approvals/1", {"approve": True})
    assert status == 401


# -- S8 briefs through the daemon -----------------------------------------------


def test_briefs_routes_via_daemon(start_daemon, env):
    srv, port = start_daemon(FakeRuntime())
    token = pair(port)
    store = srv.ctx.memory_store
    store.add_brief("2026-01-01", "alpha brief", "fake-model")

    status, resp = request(port, "GET", "/v1/briefs", token=token)
    assert status == 200
    assert any(b["text"] == "alpha brief" for b in resp["briefs"])

    # today with no stored brief -> empty but 200
    status, resp = request(port, "GET", "/v1/briefs/today", token=token)
    assert status == 200
    assert resp["today"] == ""


# -- S9 approvals through the daemon -------------------------------------------


def test_approvals_routes_via_daemon(start_daemon):
    srv, port = start_daemon(FakeRuntime())
    token = pair(port)
    manager = srv.ctx.approval_manager
    row = manager.create("fs.write", {"path": "/tmp/x", "content": "hi"}, risk="write")

    status, resp = request(port, "GET", "/v1/approvals", token=token)
    assert status == 200
    assert resp["pending"] == 1
    assert resp["approvals"][0]["id"] == row["id"]

    # deny via the HTTP contract
    status, resp = request(port, "POST", "/v1/approvals/%d" % row["id"],
                           {"decision": "deny"}, token=token)
    assert status == 200
    assert resp["ok"] is True
    assert resp["approval"]["decision"] == "denied"

    status, resp = request(port, "GET", "/v1/approvals", token=token)
    assert resp["pending"] == 0
    assert resp["approvals"][0]["decision"] == "denied"

    # unknown id -> 404
    status, resp = request(port, "POST", "/v1/approvals/99999",
                           {"approve": True}, token=token)
    assert status == 404


# -- S12 diagnostics through the daemon ----------------------------------------


def test_doctor_bundle_via_daemon(start_daemon, env):
    srv, port = start_daemon(FakeRuntime())
    token = pair(port)

    status, resp = request(port, "GET", "/doctor/bundle", token=token)
    assert status == 200, resp
    assert resp["ok"] is True
    assert "bundle.json" in resp["members"]
    assert resp["bytes"] > 0
    bundle_path = Path(resp["bundle"])
    assert bundle_path.is_file()
    with zipfile.ZipFile(bundle_path) as zf:
        names = set(zf.namelist())
    assert {"system.txt", "bundle.json", "versions.txt", "config.redacted.json"} \
        <= names
