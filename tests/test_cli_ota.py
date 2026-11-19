"""CLI-level OTA tests: the `steward ota` subcommand.

Two regressions found at the 1.2.0 cut are locked here:

1. A tampered or corrupt manifest must print one plain refusal line and exit
   1, never a launcher crash. This test fails if the uncaught path comes
   back (if someone removes the try/except, cli.main raises and the test
   errors; the subprocess test asserts the frozen-style output has no
   traceback and no launcher crash text).
2. `ota check` must resolve its update source the same way the daemon does
   (STEWARD_OTA_URL override, else config.ota_url, else DEFAULT_OTA_URL), so
   a stock install with no env var reaches the same released default the
   daemon uses. The CLI/daemon parity test pins a config and asserts both
   sides resolve to the identical URL.
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from steward import cli as cli_mod   # noqa: E402
from steward import license as lic    # noqa: E402
from steward import ota as ota_mod    # noqa: E402
from steward.server import daemon as daemon_mod  # noqa: E402


@pytest.fixture
def vendor(monkeypatch):
    from steward import license as lic  # noqa: F811

    pem, pub = lic.generate_keypair()
    monkeypatch.setattr(lic, "VENDOR_PUBLIC_KEY_HEX", pub)
    return {"pub": pub, "priv": lic.private_bytes_from_pem(pem)}


def model_entry(mid="qwen-x:7b") -> dict:
    return {
        "id": mid, "family": "Test", "params_b": 7.0, "active_b": 7.0,
        "q4_gb": 5.0, "tok_range": [20, 100], "license": "Apache-2.0",
        "kind": "dense", "note": "cli test entry",
    }


def signed_manifest(priv, version="v2", **over) -> dict:
    payload = {"version": version, "released": "2026-10-28",
               "app_version": "0.2.0", "models": [model_entry()]}
    payload.update(over)
    return lic.make_envelope(payload, priv)


def write_json(tmp_path, name, obj) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(obj, sort_keys=True), encoding="utf-8")
    return path


def tamper(doc: dict) -> dict:
    """Corrupt the signature deterministically: flip its FIRST hex char so the
    result always differs from the original (a plain '0' + sig[1:] is a no-op
    when the signature already starts with '0' - same as the task repro, which
    only worked because the shipped signature does not start with '0')."""
    doc = json.loads(json.dumps(doc))
    sig = doc["sig"]
    doc["sig"] = ("1" if sig[0] == "0" else "0") + sig[1:]
    assert doc["sig"] != sig
    return doc


# -- defect 1: a tampered manifest refuses with one plain line -------------


def test_ota_check_tampered_manifest_prints_clean_refusal(tmp_path, vendor, capsys):
    doc = signed_manifest(vendor["priv"])
    bad = write_json(tmp_path, "bad.json", tamper(doc))
    rc = cli_mod.main(["ota", "check", "--manifest", str(bad)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "update manifest rejected" in out
    assert "signature" in out
    assert "Traceback" not in out
    # Exit is 1 and only that one refusal line went to stdout.
    assert out.strip().startswith("update manifest rejected")


def test_ota_check_tampered_manifest_subprocess_is_clean(tmp_path, vendor):
    """The frozen-binary failure mode: an unhandled exception must never come
    back. Run the CLI in a clean subprocess (as the launcher would) and assert
    a plain refusal, no traceback and no launcher crash text."""
    doc = signed_manifest(vendor["priv"])
    bad = write_json(tmp_path, "bad.json", tamper(doc))
    worktree_src = str(Path(__file__).resolve().parents[1] / "src")
    env = dict(os.environ)
    env.update({"PYTHONPATH": worktree_src, "STEWARD_HOME": str(tmp_path / "home")})
    env.pop("STEWARD_OTA_URL", None)
    proc = subprocess.run(
        [sys.executable, "-m", "steward.cli", "ota", "check",
         "--manifest", str(bad)],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 1
    combined = proc.stdout + proc.stderr
    assert "update manifest rejected" in combined
    assert "signature" in combined
    assert "Traceback" not in combined
    assert "Failed to execute script" not in combined
    assert "unhandled exception" not in combined


def test_ota_check_corrupt_manifest_prints_clean_refusal(tmp_path, capsys):
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("this is not json", encoding="utf-8")
    rc = cli_mod.main(["ota", "check", "--manifest", str(corrupt)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "update manifest rejected" in out
    assert "Traceback" not in out


# -- defect 2: the CLI resolves the source the same way the daemon does ------


class FakeRuntime:
    """Minimal stand-in so a DaemonContext can resolve its update source."""

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


def _daemon_resolved_source(tmp_path, env: dict) -> str:
    """What the daemon resolves for the given STEWARD_HOME, the exact path
    its update routes use (DaemonContext.update_source -> resolve_source)."""
    ctx = daemon_mod.DaemonContext(
        runtime=FakeRuntime(), model="", source_dir="",
        db_path=tmp_path / "db" / "steward.db",
        secret=b"x" * 32, version="test",
    )
    return ctx.update_source()


def _write_config(home: Path, ota_url: str) -> None:
    (home / "config.json").write_text(
        json.dumps({"ota_url": ota_url}), encoding="utf-8")


def test_cli_and_daemon_resolve_pinned_config_identically(tmp_path, monkeypatch):
    home = tmp_path / "pinned"
    home.mkdir()
    _write_config(home, "https://ops.example.test/releases/update.json")
    monkeypatch.delenv("STEWARD_OTA_URL", raising=False)
    monkeypatch.setenv("STEWARD_HOME", str(home))

    daemon_src = _daemon_resolved_source(tmp_path, env={})
    # The CLI passes exactly what resolve_source() returns into check_update.
    captured = {}

    def fake_check(source):
        captured["source"] = source
        return {
            "available": False, "current_version": "embedded",
            "latest_version": "v2", "model_count": 1, "released": "2026-10-28",
        }

    monkeypatch.setattr(ota_mod, "check_for_update", fake_check)
    rc = cli_mod.main(["ota", "check"])
    assert rc == 0
    assert captured["source"] == daemon_src == "https://ops.example.test/releases/update.json"


def test_cli_and_daemon_resolve_env_override_identically(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_OTA_URL", "https://env.example.test/update.json")
    monkeypatch.setenv("STEWARD_HOME", str(tmp_path))

    daemon_src = _daemon_resolved_source(tmp_path, env={})
    captured = {}

    def fake_check(source):
        captured["source"] = source
        return {
            "available": False, "current_version": "embedded",
            "latest_version": "v2", "model_count": 1, "released": "2026-10-28",
        }

    monkeypatch.setattr(ota_mod, "check_for_update", fake_check)
    rc = cli_mod.main(["ota", "check"])
    assert rc == 0
    assert captured["source"] == daemon_src == "https://env.example.test/update.json"


def test_cli_and_daemon_resolve_released_default_identically(tmp_path, monkeypatch):
    from steward.config import DEFAULT_OTA_URL

    monkeypatch.delenv("STEWARD_OTA_URL", raising=False)
    monkeypatch.setenv("STEWARD_HOME", str(tmp_path))
    # No config.json and no env var: both sides must fall back to the shipped
    # released default, exactly the stock-install path the defect broke.
    daemon_src = _daemon_resolved_source(tmp_path, env={})
    assert daemon_src == DEFAULT_OTA_URL

    captured = {}

    def fake_check(source):
        captured["source"] = source
        return {
            "available": False, "current_version": "embedded",
            "latest_version": "v2", "model_count": 1, "released": "2026-10-28",
        }

    monkeypatch.setattr(ota_mod, "check_for_update", fake_check)
    rc = cli_mod.main(["ota", "check"])
    assert rc == 0
    assert captured["source"] == DEFAULT_OTA_URL


def test_ota_check_reports_source_line_on_success(tmp_path, vendor, capsys,
                                                 monkeypatch):
    """A successful check names the source it used (like `update check`)."""
    doc = signed_manifest(vendor["priv"])
    good = write_json(tmp_path, "good.json", doc)
    rc = cli_mod.main(["ota", "check", "--manifest", str(good)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "source:   %s" % good in out


# -- daemon check-route error path: the same tampered manifest returns a
# -- structured refusal through GET /v1/update/check -------------------------


def test_daemon_check_route_refuses_tampered_manifest_structurally(
        tmp_path, vendor, monkeypatch):
    """The daemon's update check route must answer the same tampered manifest
    with a structured refusal (state=refused), never crash the handler."""
    import http.client

    from steward.server import auth as auth_mod
    from steward.server import daemon as daemon_mod  # noqa: F811

    # Tamper the signature exactly like the CLI repro (flip the first hex char).
    tampered = tamper(signed_manifest(vendor["priv"]))

    class _H(http.server.BaseHTTPRequestHandler):
        doc = tampered

        def log_message(self, format, *args):  # silence
            pass

        def do_GET(self):  # noqa: N802
            body = json.dumps(self.doc, sort_keys=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d/manifest.json" % server.server_address[1]

    srv = daemon_mod.create_server(
        ("127.0.0.1", 0), runtime=FakeRuntime(),
        db_path=tmp_path / "db" / "steward.db",
        secret_file=tmp_path / "db" / "pairing.key", advertise=False,
    )
    srv.ctx.ota_source = base
    sthr = threading.Thread(target=srv.serve_forever, daemon=True)
    sthr.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1],
                                          timeout=15)
        tok = auth_mod.issue_token(srv.ctx.secret, "ota-cli-test")[0]
        conn.request("GET", "/v1/update/check",
                     headers={"Authorization": "Bearer " + tok})
        resp = conn.getresponse()
        body = json.loads(resp.read().decode("utf-8"))
        conn.close()
        assert resp.status == 200
        assert body["ok"] is False
        assert body["state"] == "refused"
        assert "signature" in body["error"].lower()
        assert body["source"] == base
        # The refusal is recorded, so the daemon reports a refused status.
        assert srv.ctx.update_status.get("state") == "refused"
    finally:
        srv.shutdown_server()
        server.shutdown()
