"""Install channel detection: one update owner per install channel.

Locks the STEWARD-UPDATE-CHANNEL contract:

* The channel detector is honest: a dpkg-owned binary reports apt, a
  Homebrew-prefix binary reports brew, a user-path binary reports manual, a
  source run reports source, and anything unprovable reports unknown. No
  channel is guessed.
* When a package manager owns the install, the app must not replace its own
  binary. apply_update() refuses before any write, names the manager, and
  gives the exact command a person runs. The daemon apply route refuses
  before staging. A manual or unknown channel still updates, and an unknown
  channel explicitly says so.
* /health and the update check response carry the channel; a managed check
  reports the newer version but never offers a dead Update button
  (app.available is False).

These tests FAIL if the managed-by guard is removed, on purpose: the guard is
the whole point of the card.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import threading
import http.client
import http.server
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from steward import license as lic  # noqa: E402
from steward import ota as ota_mod  # noqa: E402
from steward.server import auth as auth_mod  # noqa: E402
from steward.server import daemon as daemon_mod  # noqa: E402


@pytest.fixture
def vendor(monkeypatch):
    pem, pub = lic.generate_keypair()
    monkeypatch.setattr(lic, "VENDOR_PUBLIC_KEY_HEX", pub)
    return {"pub": pub, "priv": lic.private_bytes_from_pem(pem)}


def model_entry(mid="qwen-x:7b") -> dict:
    return {
        "id": mid, "family": "Test", "params_b": 7.0, "active_b": 7.0,
        "q4_gb": 5.0, "tok_range": [20, 100], "license": "Apache-2.0",
        "kind": "dense", "note": "channel test entry",
    }


def manifest_doc(vendor, version="c1", **extra) -> dict:
    payload = {
        "version": version,
        "released": "2026-10-28",
        "app_version": "1.1.0",
        "models": [model_entry()],
        "note": "signed channel-test manifest",
    }
    payload.update(extra)
    return lic.make_envelope(payload, vendor["priv"])


def app_block(version="1.1.1", key=None, payload=None) -> dict:
    return {
        "version": version,
        "released": "2026-10-28",
        "notes": "channel-test app update",
        "min_source_version": "0.0.0",
        "payloads": {key or "linux-aarch64": payload or {
            "url": "steward-app.bin", "sha256": "0" * 64, "size": 0}},
    }


def make_app_binary(path: Path, version: str) -> bytes:
    body = ("#!/usr/bin/env python3\nimport sys\n"
            "print('steward %s')\n" % version)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path.read_bytes()


def ok_probe():
    return {"ok": True}


# ---------------------------------------------------------------------------
# Channel detection (honest probes)
# ---------------------------------------------------------------------------


def test_detect_channel_source_checkout_on_this_host():
    """A source run (this test) has no frozen binary, so it reports source."""
    # This process is python, not a frozen steward binary.
    assert getattr(sys, "frozen", False) is False
    assert ota_mod.detect_channel() == ota_mod.CHANNEL_SOURCE


def test_detect_channel_dpkg_owned_path_names_apt(tmp_path):
    """A binary owned by dpkg-query reports apt."""
    fake_bin = tmp_path / "scratch" / "fake-dpkg"
    fake_bin.mkdir(parents=True, exist_ok=True)
    shim = fake_bin / "dpkg-query"
    shim.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-S\" ]; then\n"
        "  echo 'steward: '$(echo \"$2\")\n"
        "  exit 0\n"
        "fi\nexit 1\n",
        encoding="utf-8")
    shim.chmod(0o755)
    binary = "/usr/bin/steward"
    env = {"PATH": str(fake_bin), "HOME": "/nonexistent",
           "LOCALAPPDATA": ""}
    assert ota_mod.detect_channel(binary=binary, env=env) == ota_mod.CHANNEL_APT


def test_detect_channel_brew_cellar_path_names_brew(tmp_path):
    """A binary under a Homebrew Cellar/Caskroom reports brew (layout probe)."""
    env = {"PATH": "/u/nowhere", "HOME": "/nonexistent"}
    for rel in ("/opt/homebrew/Cellar/steward/1.2.0/bin/steward",
                "/usr/local/Caskroom/steward/1.2.0/steward"):
        assert ota_mod.detect_channel(binary=rel, env=env) == ota_mod.CHANNEL_BREW


def test_detect_channel_user_path_reports_manual(tmp_path):
    """A binary under the user home reports manual (no package manager)."""
    home = str(tmp_path / "home")
    binary = str(Path(home) / ".local" / "bin" / "steward")
    env = {"PATH": "/u/nowhere", "HOME": home, "LOCALAPPDATA": ""}
    assert ota_mod.detect_channel(binary=binary, env=env) == ota_mod.CHANNEL_MANUAL


def test_detect_channel_unowned_system_path_reports_unknown(tmp_path):
    """A path no package manager owns and that is not a user path: unknown."""
    env = {"PATH": "/u/nowhere", "HOME": "/nonexistent", "LOCALAPPDATA": ""}
    assert ota_mod.detect_channel(binary="/opt/steward/steward",
                                  env=env) == ota_mod.CHANNEL_UNKNOWN


def test_managed_update_command_names_manager():
    assert "apt" in ota_mod.channel_update_command("apt")
    assert "brew" in ota_mod.channel_update_command("brew")
    assert "winget" in ota_mod.channel_update_command("winget")


# ---------------------------------------------------------------------------
# The guard: apply_update refuses on a package-managed install
# ---------------------------------------------------------------------------


def test_apply_dpkg_owned_refuses_and_names_apt(tmp_path, vendor):
    """A dpkg-owned install refuses to swap its binary and names apt.

    Uses a manifest with an app payload for this host so the apply would
    otherwise swap. The refusal must (a) happen, (b) name apt, (c) give the
    exact command, and (d) never write anything.
    """
    key = ota_mod.platform_key(tmp_path)
    blob = make_app_binary(tmp_path / "steward-app.bin", "9.9.9")
    doc = manifest_doc(vendor, app=app_block(
        key=key, payload={"url": "steward-app.bin",
                          "sha256": hashlib.sha256(blob).hexdigest(),
                          "size": len(blob)}))

    result = ota_mod.apply_update(doc, ok_probe, root=tmp_path,
                                  channel=ota_mod.CHANNEL_APT)
    assert result["state"] == "managed"
    assert result["applied"] is False
    assert "apt" in result["error"]
    assert "sudo apt install --only-upgrade steward" in result["error"]
    assert result["update_command"] == "sudo apt install --only-upgrade steward"
    # Nothing was written: no catalog file, no pointer, no app directory.
    assert not ota_mod.catalog_path("c1", tmp_path).exists()
    assert not ota_mod.current_path(tmp_path).exists()
    assert not (ota_mod._ota_dir(tmp_path) / "app").exists()


def test_apply_manual_channel_still_applies(tmp_path, vendor):
    """A manual install (binary in a user path) is not managed: it updates."""
    key = ota_mod.platform_key(tmp_path)
    blob = make_app_binary(tmp_path / "steward-app.bin", "2.0.0")
    doc = manifest_doc(vendor, version="m1", app=app_block(
        version="2.0.0", key=key, payload={"url": "steward-app.bin",
                                           "sha256": hashlib.sha256(blob).hexdigest(),
                                           "size": len(blob)}))
    # Stage the payload exactly like the daemon route and CLI do before apply.
    target = ota_mod.staging_dir(tmp_path) / "app" / "steward-app.bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(blob)
    target.chmod(0o700)
    result = ota_mod.apply_update(
        doc, ota_mod.app_binary_probe(str(target), "2.0.0"),
        root=tmp_path, channel=ota_mod.CHANNEL_MANUAL)
    assert result["applied"] is True
    assert result["channel"] == ota_mod.CHANNEL_MANUAL
    st = ota_mod.current_state(tmp_path)
    assert st["app"]["version"] == "2.0.0"


def test_apply_unknown_channel_updates_but_says_so(tmp_path, vendor):
    """An unknown channel updates, and the result explicitly says so."""
    key = ota_mod.platform_key(tmp_path)
    blob = make_app_binary(tmp_path / "steward-app.bin", "2.0.0")
    doc = manifest_doc(vendor, version="u2", app=app_block(
        version="2.0.0", key=key, payload={"url": "steward-app.bin",
                                           "sha256": hashlib.sha256(blob).hexdigest(),
                                           "size": len(blob)}))
    target = ota_mod.staging_dir(tmp_path) / "app" / "steward-app.bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(blob)
    target.chmod(0o700)
    result = ota_mod.apply_update(
        doc, ota_mod.app_binary_probe(str(target), "2.0.0"),
        root=tmp_path, channel=ota_mod.CHANNEL_UNKNOWN)
    assert result["applied"] is True
    assert result["channel"] == ota_mod.CHANNEL_UNKNOWN


def test_apply_managed_catalog_only_still_allowed(tmp_path, vendor):
    """A catalog-only apply swaps no binary, so it stays allowed on managed."""
    doc = manifest_doc(vendor, version="cat")
    result = ota_mod.apply_update(doc, ok_probe, root=tmp_path,
                                  channel=ota_mod.CHANNEL_APT)
    assert result["applied"] is True
    assert result["channel"] == ota_mod.CHANNEL_APT


# ---------------------------------------------------------------------------
# Daemon routes: /health and check carry the channel; apply refuses
# ---------------------------------------------------------------------------


class FakeRuntime:
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
def daemon(tmp_path, monkeypatch):
    started = []

    def start(channel=None):
        srv = daemon_mod.create_server(
            ("127.0.0.1", 0), runtime=FakeRuntime(),
            db_path=tmp_path / "db" / "steward.db",
            secret_file=tmp_path / "db" / "pairing.key", advertise=False)
        if channel is not None:
            srv.ctx._installed_channel = channel
        thr = threading.Thread(target=srv.serve_forever, daemon=True)
        thr.start()
        started.append((srv, thr))
        return srv, srv.server_address[1]

    yield start
    for srv, thr in started:
        srv.shutdown_server()
        thr.join(timeout=5)


@pytest.fixture(autouse=True)
def no_global_models(monkeypatch):
    """Route tests manipulate pointers, not the process-global catalog."""
    monkeypatch.setattr(ota_mod, "_install_models",
                        lambda entries: [m["id"] for m in entries])


def _request(port, method, path, token=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    hdrs = {"Content-Type": "application/json"}
    if token:
        hdrs["Authorization"] = "Bearer " + token
    if headers:
        hdrs.update(headers)
    conn.request(method, path, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read().decode("utf-8", "replace")
    conn.close()
    try:
        body = json.loads(data) if data else {}
    except json.JSONDecodeError:
        body = data
    return resp.status, body


def _token(srv) -> str:
    return auth_mod.issue_token(srv.ctx.secret, "channel-test")[0]


def test_health_carries_channel(daemon, tmp_path):
    srv, port = daemon(channel=ota_mod.CHANNEL_APT)
    status, body = _request(port, "GET", "/health")
    assert status == 200
    assert body["channel"] == "apt"


def test_check_route_carries_channel_and_managed_info(daemon, tmp_path, vendor):
    """A managed check still reports the newer version, but app.available is
    False (no dead Update button) and the exact command rides along."""
    key = ota_mod.platform_key(tmp_path)
    blob = make_app_binary(tmp_path / "steward-app.bin", "2.0.0")
    doc = manifest_doc(vendor, version="r1", app=app_block(
        version="2.0.0", key=key, payload={"url": "steward-app.bin",
                                           "sha256": hashlib.sha256(blob).hexdigest(),
                                           "size": len(blob)}))

    class _H(http.server.BaseHTTPRequestHandler):
        routes = {"/manifest.json": json.dumps(doc, sort_keys=True).encode()}
        log_message = lambda *a, **k: None

        def do_GET(self):  # noqa: N802
            body = self.routes.get(self.path, b"not found")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    sthr = threading.Thread(target=server.serve_forever, daemon=True)
    sthr.start()
    try:
        srv, port = daemon(channel=ota_mod.CHANNEL_APT)
        srv.ctx.ota_source = "http://127.0.0.1:%d/manifest.json" % server.server_address[1]
        status, body = _request(port, "GET", "/v1/update/check",
                                token=_token(srv))
        assert status == 200
        assert body["channel"] == "apt"
        assert body["managed_by"] == "apt"
        assert body["managed_update_command"] == "sudo apt install --only-upgrade steward"
        # A newer version exists, but the in-app updater must not offer it.
        assert body["app"]["latest_version"] == "2.0.0"
        assert body["app"]["available"] is False
        assert "apt" in body["app"]["reason"]
    finally:
        server.shutdown()


def test_apply_route_refuses_managed_no_stage_or_swap(daemon, tmp_path, vendor):
    """POST /v1/update/apply on a managed install returns the managed-by
    refusal and never stages, swaps, or writes anything."""
    key = ota_mod.platform_key(tmp_path)
    blob = make_app_binary(tmp_path / "steward-app.bin", "1.2.0")
    doc = manifest_doc(vendor, version="r2", app=app_block(
        version="1.2.0", key=key, payload={"url": "steward-app.bin",
                                           "sha256": hashlib.sha256(blob).hexdigest(),
                                           "size": len(blob)}))

    class _H(http.server.BaseHTTPRequestHandler):
        routes = {"/manifest.json": json.dumps(doc, sort_keys=True).encode(),
                  "/steward-app.bin": blob}
        log_message = lambda *a, **k: None

        def do_GET(self):  # noqa: N802
            body = self.routes.get(self.path, b"not found")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    sthr = threading.Thread(target=server.serve_forever, daemon=True)
    sthr.start()
    try:
        srv, port = daemon(channel=ota_mod.CHANNEL_APT)
        srv.ctx.ota_source = "http://127.0.0.1:%d/manifest.json" % server.server_address[1]
        status, body = _request(port, "POST", "/v1/update/apply",
                                token=_token(srv))
        assert status == 200
        assert body["state"] == "managed"
        assert body["applied"] is False
        assert "apt" in body["error"]
        assert body["update_command"] == "sudo apt install --only-upgrade steward"
        # The manual escape hatch is the package-manager command, not the
        # in-app updater (which would just refuse again).
        assert body["manual_command"] == "sudo apt install --only-upgrade steward"
        # Nothing staged or swapped: no pointer, no app file, no download.
        assert ota_mod.current_state(tmp_path) is None
        assert not (tmp_path / "ota" / "app").exists()
    finally:
        server.shutdown()
