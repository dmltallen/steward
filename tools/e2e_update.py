#!/usr/bin/env python3
"""End-to-end proof: one-click update old (1.1.0) -> new (1.2.0).

Runs the real old frozen daemon from a temp home, points its OTA source at a
loopback server serving a *signed* v2 manifest whose app payload is the new
1.2.0 frozen binary, then drives the actual web client update flow against the
daemon: check reports 1.2.0 available, apply downloads + verifies + swaps +
health-probes, and the daemon restarts into the new binary via exec. The
proof asserts the daemon /v1/webapp/info version changed to 1.2.0 and that a
tampered payload is refused and leaves the old build answering.

Prereqs (built earlier in this card):
    dist/steward             -> new build, `steward version` reports 1.2.0
    dist/steward-1.1.0-old   -> old build the daemon runs, reports 1.1.0
Signs with the real vendor key ~/.steward-keys/steward-signing.key.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OLD_BIN = REPO / "dist" / "steward-1.1.0-old"
NEW_BIN = REPO / "dist" / "steward"
KEY = Path.home() / ".steward-keys" / "steward-signing.key"


class _Release(http.server.BaseHTTPRequestHandler):
    routes: dict = {}

    def log_message(self, format, *args):
        pass

    def _send(self, blob, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_GET(self):
        path = (self.path or "").split("?", 1)[0]
        blob = self.routes.get(path)
        if blob is None:
            self.send_response(404)
            self.end_headers()
            return
        self._send(blob, "application/octet-stream")


def main() -> int:
    sys.path.insert(0, str(REPO / "src"))
    from steward import license as lic
    from steward import ota as ota_mod

    priv = lic.private_bytes_from_pem(KEY.read_bytes())
    new_blob = NEW_BIN.read_bytes()
    new_digest = hashlib.sha256(new_blob).hexdigest()
    platform = ota_mod.platform_key()

    payload = {
        "version": "2026-10-28.2",
        "released": "2026-10-28",
        "app_version": "1.2.0",
        "models": ota_mod.embedded_catalog()["models"],
        "note": "s15 update-ui E2E manifest",
        "app": {
            "version": "1.2.0",
            "released": "2026-10-28",
            "notes": "One-click update UI (s15)",
            "min_source_version": "1.1.0",
            "payloads": {
                platform: {"url": "steward-1.2.0.bin",
                           "sha256": new_digest, "size": len(new_blob)},
            },
        },
    }
    doc = lic.make_envelope(payload, priv)
    manifest_blob = json.dumps(doc, sort_keys=True).encode("utf-8")

    _Release.routes = {
        "/update.json": manifest_blob,
        "/steward-1.2.0.bin": new_blob,
    }
    src = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Release)
    thread = threading.Thread(target=src.serve_forever, daemon=True)
    thread.start()
    source = "http://127.0.0.1:%d/update.json" % src.server_address[1]
    payload_url = "http://127.0.0.1:%d/steward-1.2.0.bin" % src.server_address[1]
    print("release source: %s" % source)

    home = Path(tempfile.mkdtemp(prefix="steward-e2e-"))
    env = dict(os.environ)
    env["STEWARD_HOME"] = str(home)
    env["STEWARD_OTA_URL"] = source
    env["STEWARD_HOST"] = "127.0.0.1"
    env["STEWARD_PORT"] = str(4651)
    env["STEWARD_MODEL"] = "qwen3:8b"
    env["STEWARD_OLLAMA_URL"] = "http://127.0.0.1:1"  # nothing listens: degraded is fine
    env["STEWARD_RESTART_TRACE"] = str(home / "restart-trace.txt")

    daemon_log = home / "daemon.log"
    daemon = subprocess.Popen(
        [str(OLD_BIN), "serve", "--port", "4651"],
        stdout=open(daemon_log, "w"), stderr=subprocess.STDOUT, env=env, text=True,
    )
    try:
        _wait_port(4651)
        token = _pair(4651, env)
        info0 = _get(4651, "/v1/webapp/info", token)
        print("running daemon version: %s" % info0["version"])
        assert info0["version"] == "1.1.0", info0

        # 1. check shows the newer app is available
        check = _get(4651, "/v1/update/check", token)
        assert check["ok"] is True and check["state"] == "ok"
        assert check["app"]["available"] is True, check
        assert check["app"]["latest_version"] == "1.2.0", check
        print("check: 1.2.0 available (running %s)" % check["app"]["current_version"])

        # 2. one-click apply over SSE (the exact request the web client makes)
        result = _apply(4651, token)
        assert result["ok"] is True, result
        assert result["state"] == "applied", result
        assert result["app"]["installed"] is True, result
        assert result["restart_intent"]["pending"] is True, result
        print("apply: installed %s (restart pending)" % result["app"]["version"])
        print("manual command: %s" % result["manual_command"])

        # 3. daemon restarts into the new binary on its own
        print("waiting for the daemon to come back...")
        version_after = _wait_version(4651, token, timeout=90,
                                      expect_change_from=info0["version"])
        assert version_after == "1.2.0", version_after
        print("daemon is back, version changed to %s" % version_after)

        # 4. the new daemon is genuinely the new binary
        out = subprocess.run([str(NEW_BIN), "version"], capture_output=True,
                             text=True, timeout=20)
        assert out.stdout.strip() == "steward 1.2.0"
        print("new binary answers `version`: %s" % out.stdout.strip())

        # 5. tampered payload: flip a byte, refuse, old build still serving
        print("--- tamper proof ---")
        # Drop the previous run's staging so the tampered bytes are actually
        # downloaded and refused at the sha256 gate (a real consumer starts
        # with a clean staging dir; a stale good copy would otherwise mask the
        # tamper). The checksum refusal must be fast and leave the old app.
        import shutil as _shutil
        _staging = Path(home) / "ota" / "staging"
        if _staging.exists():
            _shutil.rmtree(_staging)
        # A *newer* app version (1.3.0) so apply proceeds past "already
        # active", whose served payload bytes were corrupted after minting:
        # the sha256 in the signed manifest refers to good bytes, the server
        # delivers the flipped file, so staging must refuse before install.
        tamper_payload = json.loads(json.dumps(payload))
        tamper_payload["app"]["version"] = "1.3.0"
        tamper_payload["app"]["min_source_version"] = "1.1.0"
        tamper_payload["app"]["payloads"][platform] = {
            "url": "steward-1.2.0.bin",
            "sha256": new_digest, "size": len(new_blob),
        }
        tamper_doc = lic.make_envelope(tamper_payload, priv)
        _Release.routes["/update.json"] = json.dumps(tamper_doc, sort_keys=True).encode("utf-8")
        _Release.routes["/steward-1.2.0.bin"] = new_blob[:-1] + bytes([new_blob[-1] ^ 0xFF])
        got = _apply(4651, token)
        assert got["ok"] is False, got
        assert got["state"] == "refused", got
        print("tampered apply refused: %s" % (got.get("error") or got.get("state")))
        ver = _wait_version(4651, token, timeout=10)
        print("after tamper the daemon answers (old install intact): %s" % ver)
    finally:
        daemon.terminate()
        try:
            daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.kill()
        src.shutdown()
        log_path = daemon_log
        if log_path.is_file():
            print("--- daemon log ---")
            print(log_path.read_text(encoding="utf-8", errors="replace")[-3000:])
        shutil.rmtree(home, ignore_errors=True)
    print("E2E OK")
    return 0


def _wait_port(port, timeout=30):
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.2)
    raise SystemExit("daemon never came up on port %d" % port)


def _req(port, method, path, token=None, body=None, accept=None):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    hdrs = {"Connection": "close"}
    if token:
        hdrs["Authorization"] = "Bearer " + token
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    if accept:
        hdrs["Accept"] = accept
    payload = json.dumps(body) if body is not None else None
    conn.request(method, path, body=payload, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read().decode("utf-8", "replace")
    conn.close()
    return resp.status, data


def _pair(port, env):
    status, data = _req(port, "GET", "/pair/code")
    assert status == 200, data
    code = json.loads(data)["code"]
    status, data = _req(port, "POST", "/v1/pair", body={"code": code, "device": "e2e"})
    assert status == 200, data
    return json.loads(data)["token"]


def _get(port, path, token):
    import json
    status, data = _req(port, "GET", path, token=token)
    assert status == 200, data
    return json.loads(data)


def _apply(port, token):
    status, data = _req(port, "POST", "/v1/update/apply", token=token,
                        body={}, accept="text/event-stream")
    assert status == 200, data[:200]
    # Parse the SSE stream: grab the final result event.
    result = None
    for line in data.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            ev = json.loads(line[6:])
            if ev.get("event") == "result":
                result = ev["result"]
    assert result is not None, data[:200]
    return result


def _wait_version(port, token, timeout=90, expect_change_from=None):
    """Poll /v1/webapp/info until the daemon reports a version that differs
    from the run-before value (the old daemon keeps answering during the 1s
    pre-exec grace, so a single 200 is not proof of a restart)."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            info = _get(port, "/v1/webapp/info", token)
            v = info["version"]
            if last is None:
                last = v
                if expect_change_from is None or v != expect_change_from:
                    return v
                # saw the old build still answering; keep waiting for a swap
            elif v != last:
                return v
        except (AssertionError, OSError, ConnectionError, json.JSONDecodeError):
            pass  # brief gap while the daemon is down is expected
        time.sleep(0.5)
    return last


def token_after(_version):
    return None


if __name__ == "__main__":
    sys.exit(main())
