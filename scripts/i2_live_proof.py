"""I2 live proof: daemon from the merged tree serves every route family.

Starts a real StewardServer on 127.0.0.1:18991 (a port that is free before and
must be free after), with a fake runtime, then hits each required route and
quotes the status code. Spins the daemon down and verifies the port is free.
"""

import http.client
import json
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, "/home/user/steward/.worktrees/t_801c6738/src")

from steward.server import daemon as daemon_mod  # noqa: E402

PORT = 18991
DATA_ROOT = Path("/tmp/steward-i2-proof")

import shutil
shutil.rmtree(DATA_ROOT, ignore_errors=True)


class FakeRuntime:
    kind = "fake-runtime"
    url = "http://127.0.0.1:1"

    def health(self):
        return {"ok": True, "backend": "fake", "version": "0.0.0-fake",
                "models": ["fake-model", "qwen3:8b"]}

    def list_models(self):
        return ["fake-model", "qwen3:8b"]

    def chat(self, messages, stream=True, model=None):
        if not stream:
            yield {"delta": "I2 live proof reply from the fake backend."}
            return
        for token in ["I2 ", "live ", "proof"]:
            yield {"delta": token}


def port_free(port):
    # Mirrors how the daemon binds: ThreadingHTTPServer sets SO_REUSEADDR, so
    # a bind succeeds across TIME-WAIT residues from short probe connections.
    s = socket.socket()
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def request(port, method, path, body=None, token=None, raw=False):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
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


def main():
    print("== I2 live daemon proof (merged tree) ==")
    assert port_free(PORT), "port %d must be free before start" % PORT

    db_path = DATA_ROOT / "db" / "steward.db"
    srv = daemon_mod.create_server(
        ("127.0.0.1", PORT), runtime=FakeRuntime(),
        db_path=db_path,
        secret_file=DATA_ROOT / "db" / "pairing.key",
        advertise=False)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.5)
    port = srv.server_address[1]
    print("daemon bound on 127.0.0.1:%d (data root %s)" % (port, DATA_ROOT))

    # open routes
    s, h = request(port, "GET", "/health")
    print("GET /health          -> %d (status=%s backend=%s)" % (s, h["status"], h["backend"]))
    s, b = request(port, "GET", "/v1/bundle")
    print("GET /v1/bundle       -> %d (class=%s)" % (s, (b.get("bundle") or {}).get("class")))

    # pair
    s, code_resp = request(port, "GET", "/pair/code")
    token = None
    if s == 200:
        s, pr = request(port, "POST", "/v1/pair",
                        {"code": code_resp["code"], "device": "i2-proof"})
        token = pr.get("token")
    print("POST /v1/pair        -> %d (token acquired: %s)" % (s, bool(token)))

    # webapp root (S5)
    s, ctype, body = request(port, "GET", "/", raw=True)
    print("GET /                -> %d (%s, %d bytes)" % (s, ctype, len(body)))

    # briefs (S8)
    s, br = request(port, "GET", "/v1/briefs", token=token)
    print("GET /v1/briefs       -> %d (briefs=%d)" % (s, len(br.get("briefs", []))))
    s, br2 = request(port, "GET", "/v1/briefs/today", token=token)
    print("GET /v1/briefs/today -> %d (today=%r)" % (s, br2.get("today", "")))

    # approvals (S9): create one via the manager, then list and deny over HTTP
    mgr = srv.ctx.approval_manager
    row = mgr.create("fs.write", {"path": "/tmp/i2proof", "content": "x"}, risk="write")
    s, ap = request(port, "GET", "/v1/approvals", token=token)
    print("GET /v1/approvals    -> %d (pending=%d, total=%d)" % (s, ap["pending"], len(ap["approvals"])))
    s, dec = request(port, "POST", "/v1/approvals/%d" % row["id"],
                     {"decision": "deny"}, token=token)
    print("POST /v1/approvals/%d -> %d (ok=%s) decision=%s"
          % (row["id"], s, dec.get("ok"), (dec.get("approval") or {}).get("decision")))

    # diagnostics (S12)
    s, doc = request(port, "GET", "/doctor/bundle", token=token)
    print("GET /doctor/bundle   -> %d (members=%d, bytes=%d)"
          % (s, len(doc.get("members", [])), doc.get("bytes", 0)))
    print("  members: %s" % ", ".join(doc.get("members", [])))

    # stream check: /v1/chat (S2) still works on the merged handler
    s, cresp = request(port, "POST", "/v1/chat", {"text": "hello"}, token=token)
    print("POST /v1/chat        -> %d (SSE, %d bytes)"
          % (s, len(cresp) if isinstance(cresp, str) else 0))

    # spin down
    srv.shutdown_server()
    thread.join(timeout=5)
    # A just-closed listener can take a moment to release while in-flight
    # handler threads (e.g. the SSE /v1/chat connection above) wind down, so
    # poll up to 5s before declaring the port free.
    free, owned = False, False
    for _ in range(50):
        if port_free(port) and port_free(PORT):
            free = owned = True
            break
        time.sleep(0.1)
    print("daemon stopped.")
    print("port %d (bound) free: %s | original %d free: %s" % (port, free, PORT, owned))
    assert free, "bound port must be free after shutdown"
    assert owned, "original port must be free after shutdown"
    shutil.rmtree(DATA_ROOT, ignore_errors=True)
    print("== proof done, no daemon or port left running ==")


if __name__ == "__main__":
    sys.exit(main())
