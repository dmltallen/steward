#!/usr/bin/env python3
"""Live demo driver: keep an old daemon + a loopback release source running so
the webapp can be exercised in a real browser. Prints the ports/URLs to wait
on, then serves forever until killed. The daemon runs from the frozen OLD
binary at 1.1.0 with the update UI; the source serves a signed 1.2.0 manifest
plus the new frozen binary, so a browser can click Update and watch the
notice -> progress -> reconnect -> daemon-version-change flow.
"""
from __future__ import annotations

import hashlib
import http.server
import json
import os
import shutil
import signal
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
PORT = int(os.environ.get("DEMO_PORT", "4671"))


class _Release(http.server.BaseHTTPRequestHandler):
    routes: dict = {}
    slow_ms = int(os.environ.get("DEMO_SLOW_MS", "0"))

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        blob = self.routes.get((self.path or "").split("?", 1)[0])
        if blob is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        if self.slow_ms and (self.path or "").endswith(".bin"):
            chunk = 512 * 1024
            for i in range(0, len(blob), chunk):
                self.wfile.write(blob[i:i + chunk])
                self.wfile.flush()
                time.sleep(self.slow_ms / 1000.0)
        else:
            self.wfile.write(blob)


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
        "note": "update-ui live demo manifest",
        "app": {
            "version": "1.2.0",
            "released": "2026-10-28",
            "notes": "One-click update UI: notice, progress, reconnect",
            "min_source_version": "1.1.0",
            "payloads": {
                platform: {"url": "steward-1.2.0.bin",
                           "sha256": new_digest, "size": len(new_blob)},
            },
        },
    }
    doc = lic.make_envelope(payload, priv)
    _Release.routes = {
        "/update.json": json.dumps(doc, sort_keys=True).encode("utf-8"),
        "/steward-1.2.0.bin": new_blob,
    }
    src = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Release)
    threading.Thread(target=src.serve_forever, daemon=True).start()
    source = "http://127.0.0.1:%d/update.json" % src.server_address[1]

    home = Path(tempfile.mkdtemp(prefix="steward-demo-"))
    (home / "home").mkdir()
    env = dict(os.environ)
    env["STEWARD_HOME"] = str(home / "home")
    env["STEWARD_OTA_URL"] = source
    env["STEWARD_HOST"] = "127.0.0.1"
    env["STEWARD_PORT"] = str(PORT)
    env["STEWARD_MODEL"] = "qwen3:8b"
    env["STEWARD_OLLAMA_URL"] = "http://127.0.0.1:1"

    dlog = open(home / "daemon.log", "w")
    daemon = subprocess.Popen(
        [str(OLD_BIN), "serve", "--port", str(PORT)],
        stdout=dlog, stderr=subprocess.STDOUT, env=env,
    )
    print("HOME=%s" % home)
    print("SOURCE=%s" % source)
    print("WEBAPP=http://127.0.0.1:%d/" % PORT)
    print("READY")
    sys.stdout.flush()

    def _stop(*_):
        daemon.terminate()
        src.shutdown()
        dlog.close()
        shutil.rmtree(home, ignore_errors=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while True:
        time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
