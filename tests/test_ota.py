"""Tests for signed OTA catalog updates (src/steward/ota.py).

Covers manifest signature rejection, verified staging downloads (over a real
loopback HTTP server and via injected fetchers), atomic swap, the kill-mid-
swap invariant (a subprocess is actually killed between the catalog write and
the pointer commit), rollback on a failed health probe, the probe retry
window, and re-selection on hardware change.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from steward import license as lic  # noqa: E402
from steward import ota as ota_mod    # noqa: E402
from steward import download as dl    # noqa: E402

FIXED_PLAN = {
    "class": "mainstream-16",
    "capability": "qwen-x:7b",
    "assistant": "qwen-x:7b",
    "vision": False,
    "serving_mem_gb": 16.0,
    "selected_at": "2026-10-26T00:00:00+00:00",
}


@pytest.fixture
def keypair():
    pem, pub = lic.generate_keypair()
    return {"pub": pub, "priv": lic.private_bytes_from_pem(pem)}


@pytest.fixture
def vendor(monkeypatch, keypair):
    monkeypatch.setattr(lic, "VENDOR_PUBLIC_KEY_HEX", keypair["pub"])
    return keypair


def model_entry(mid="qwen-x:7b", q4=5.0, tok=(20, 100)) -> dict:
    return {
        "id": mid, "family": "Test", "params_b": 7.0, "active_b": 7.0,
        "q4_gb": q4, "tok_range": list(tok), "license": "Apache-2.0",
        "kind": "dense", "note": "test entry",
    }


def manifest_doc(keypair, version="v2", models=None, **over) -> dict:
    payload = {
        "version": version,
        "released": "2026-10-26",
        "app_version": "0.2.0",
        "models": models or [model_entry()],
        "note": "signed test manifest",
    }
    payload.update(over)
    return lic.make_envelope(payload, keypair["priv"])


def write_json(tmp_path, name, obj) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(obj, sort_keys=True), encoding="utf-8")
    return path


@pytest.fixture
def ota_harness(monkeypatch):
    """Deterministic OTA plumbing: no real models mutation, fixed hw/plan."""
    installed = []

    def fake_install(entries):
        ids = [m["id"] for m in entries]
        installed.append(ids)
        return ids

    monkeypatch.setattr(ota_mod, "_install_models", fake_install)
    monkeypatch.setattr(ota_mod, "_hardware_signature", lambda root=None: "sha256:testhw")
    monkeypatch.setattr(ota_mod, "_plan_dict", lambda: dict(FIXED_PLAN))
    return {"installed": installed}


def current(root: Path) -> dict:
    data = ota_mod._read_json(ota_mod.current_path(root))
    return data if isinstance(data, dict) else {}


# -- signature rejection -----------------------------------------------------


def test_tampered_manifest_never_swaps(tmp_path, vendor, ota_harness):
    doc = manifest_doc(vendor, version="v2")
    tampered = json.loads(json.dumps(doc))
    tampered["payload"]["models"][0]["q4_gb"] = 999.0
    with pytest.raises(lic.SignatureError):
        ota_mod.apply_update(tampered, lambda: {"ok": True}, root=tmp_path)
    assert ota_harness["installed"] == []
    assert not ota_mod.current_path(tmp_path).exists()
    assert not ota_mod.catalog_path("v2", tmp_path).exists()


def test_tampered_manifest_rejected_at_check(tmp_path, vendor):
    doc = manifest_doc(vendor, version="v2")
    path = write_json(tmp_path, "manifest.json", doc)
    info = ota_mod.check_for_update(str(path))          # valid file checks fine
    assert info["available"] is True
    data = json.loads(path.read_text(encoding="utf-8"))
    data["payload"]["models"][0]["q4_gb"] = 999.0       # tamper without re-sign
    path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    with pytest.raises(lic.SignatureError):
        ota_mod.check_for_update(str(path))


def test_wrong_key_rejected(tmp_path, keypair):
    # Manifest signed by a DIFFERENT key than the embedded one is rejected.
    other_pem, _ = lic.generate_keypair()
    other_priv = lic.private_bytes_from_pem(other_pem)
    doc = lic.make_envelope({"version": "v3", "released": "2026-10-26",
                             "models": [model_entry()]}, other_priv)
    with pytest.raises(lic.SignatureError):
        ota_mod.verify_manifest(doc, public_key_hex=keypair["pub"])


def test_malformed_manifest_rejected(tmp_path, vendor):
    doc = lic.make_envelope({"version": "v2", "released": "x", "models": [
        {"id": "m"}  # missing required fields
    ]}, vendor["priv"])
    with pytest.raises(ota_mod.MalformedManifest):
        ota_mod.apply_update(doc, lambda: {"ok": True}, root=tmp_path)
    with pytest.raises(ota_mod.MalformedManifest):
        ota_mod.verify_manifest(doc)


# -- check + apply + load ----------------------------------------------------


def test_check_reports_available(tmp_path, vendor):
    path = write_json(tmp_path, "manifest.json", manifest_doc(vendor, version="v2"))
    info = ota_mod.check_for_update(str(path), root=tmp_path)
    assert info["available"] is True
    assert info["latest_version"] == "v2"
    assert info["current_version"] == "embedded"
    assert info["model_count"] == 1


def test_apply_swaps_and_loads(tmp_path, vendor, ota_harness):
    doc = manifest_doc(vendor, version="v2")
    result = ota_mod.apply_update(doc, lambda: {"ok": True}, root=tmp_path)
    assert result["applied"] is True
    assert result["version"] == "v2"
    assert result["models"] == ["qwen-x:7b"]
    assert result["rolled_back"] is False

    st = current(tmp_path)
    assert st["active_catalog"] == "v2"
    assert st["app_version"] == "0.2.0"
    assert st["hardware"] == "sha256:testhw"
    assert ota_mod.catalog_path("v2", tmp_path).is_file()
    payload = ota_mod.load_active_payload(tmp_path)
    assert payload is not None
    assert payload["models"][0]["id"] == "qwen-x:7b"
    assert ota_harness["installed"] == [["qwen-x:7b"]]

    # Same version again is a no-op.
    again = ota_mod.apply_update(doc, lambda: {"ok": True}, root=tmp_path)
    assert again["applied"] is False
    assert "already active" in again["reason"]


def test_probe_retry_window_allows_slow_ok(tmp_path, vendor, ota_harness):
    state = {"n": 0}

    def probe():
        state["n"] += 1
        if state["n"] < 3:
            return {"ok": False, "error": "still warming"}
        return {"ok": True}

    doc = manifest_doc(vendor, version="v2")
    result = ota_mod.apply_update(doc, probe, root=tmp_path, timeout=1.0)
    assert result["applied"] is True
    assert state["n"] >= 3


def test_rollback_on_failed_health_probe(tmp_path, vendor, ota_harness):
    ok_probe = lambda: {"ok": True}  # noqa: E731
    first = ota_mod.apply_update(manifest_doc(vendor, version="v1"), ok_probe, root=tmp_path)
    assert first["applied"] is True

    bad = {"n": 0}

    def bad_probe():
        bad["n"] += 1
        return {"ok": False, "error": "backend exploded"}

    result = ota_mod.apply_update(manifest_doc(vendor, version="v2"), bad_probe,
                                  root=tmp_path, timeout=0.25)
    assert result["applied"] is False
    assert result["rolled_back"] is True
    assert "exploded" in result["reason"]

    st = current(tmp_path)
    assert st["active_catalog"] == "v1"              # pointer restored
    assert not ota_mod.catalog_path("v2", tmp_path).exists()  # new file dropped
    payload = ota_mod.load_active_payload(tmp_path)
    assert payload is not None
    assert payload["version"] == "v1"                # old state fully usable
    # install v1, install v2, restore v1
    assert ota_harness["installed"] == [["qwen-x:7b"], ["qwen-x:7b"], ["qwen-x:7b"]]


def test_rollback_on_first_apply_removes_pointer(tmp_path, vendor, ota_harness):
    doc = manifest_doc(vendor, version="v2")
    result = ota_mod.apply_update(doc, lambda: {"ok": False, "error": "nope"},
                                  root=tmp_path, timeout=0.2)
    assert result["applied"] is False
    assert result["rolled_back"] is True
    assert not ota_mod.current_path(tmp_path).exists()
    assert not ota_mod.catalog_path("v2", tmp_path).exists()


# -- kill mid-swap leaves the old state usable -------------------------------


def test_kill_mid_swap_leaves_old_state_usable(tmp_path, vendor, ota_harness):
    # Commit a v1 catalog first, so there is an old state to protect.
    assert ota_mod.apply_update(manifest_doc(vendor, version="v1"),
                                lambda: {"ok": True}, root=tmp_path)["applied"]
    v2_path = write_json(tmp_path, "v2.json", manifest_doc(vendor, version="v2"))

    worktree_src = str(Path(__file__).resolve().parents[1] / "src")
    code = (
        "import json, os, sys\n"
        "sys.path.insert(0, os.environ['STEWARD_PYTHONPATH'])\n"
        "import steward.license as lic\n"
        "lic.VENDOR_PUBLIC_KEY_HEX = os.environ['STEWARD_TEST_PUB']\n"
        "from steward import ota as ota_mod\n"
        "doc = json.load(open(sys.argv[1], encoding='utf-8'))\n"
        "ota_mod._kill_hooks = {'catalog-written': lambda: os._exit(9)}\n"
        "ota_mod.apply_update(doc, lambda: {'ok': True}, root=None)\n"
    )
    env = dict(os.environ)
    env.update({
        "STEWARD_HOME": str(tmp_path),
        "STEWARD_PYTHONPATH": worktree_src,
        "STEWARD_TEST_PUB": vendor["pub"],
    })
    proc = subprocess.run(
        [sys.executable, "-c", code, str(v2_path)],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 9   # killed at the catalog-written checkpoint

    # The pointer was never committed, so v1 is still the active, usable state.
    st = current(tmp_path)
    assert st["active_catalog"] == "v1"
    payload = ota_mod.load_active_payload(tmp_path)
    assert payload is not None
    assert payload["version"] == "v1"
    assert ota_mod.catalog_path("v1", tmp_path).is_file()
    # The orphaned v2 catalog file may linger but is never referenced.
    pointer_text = (tmp_path / "ota" / "current.json").read_text(encoding="utf-8")
    assert '"active_catalog": "v2"' not in pointer_text


# -- verified stage download -------------------------------------------------


class _RoutesHandler(http.server.BaseHTTPRequestHandler):
    routes: dict = {}

    def do_GET(self):  # noqa: N802
        body = self.routes.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 - silence server logs
        pass


def serve(routes: dict):
    _RoutesHandler.routes = routes
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RoutesHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, "http://127.0.0.1:%d" % server.server_address[1]


def test_check_and_stage_over_real_http(tmp_path, vendor):
    blob = b"staging-blob-bytes" * 10
    doc = manifest_doc(vendor, version="v2", blobs={"q.bin": hashlib.sha256(blob).hexdigest()})
    server, base = serve({
        "/manifest.json": json.dumps(doc, sort_keys=True).encode("utf-8"),
        "/models/q.bin": blob,
    })
    info = ota_mod.check_for_update(base + "/manifest.json", root=tmp_path)
    assert info["available"] is True

    result = ota_mod.download_update(doc, base_url=base + "/models", root=tmp_path)
    assert result["staged"] == 1
    staged = Path(result["staging_dir"]) / "q.bin"
    assert staged.read_bytes() == blob
    assert result["bytes"] == len(blob)
    server.shutdown()


def test_checksum_mismatch_refused(tmp_path, vendor):
    doc = manifest_doc(vendor, version="v2", blobs={"bad.bin": hashlib.sha256(b"good").hexdigest()})
    server, base = serve({"/models/bad.bin": b"evil-different-bytes"})
    with pytest.raises(ota_mod.ChecksumError):
        ota_mod.download_update(doc, base_url=base + "/models", root=tmp_path)
    assert not list(ota_mod.staging_dir(tmp_path).glob("**/*.bin"))
    server.shutdown()


def test_download_without_blobs_is_noop(tmp_path, vendor):
    doc = manifest_doc(vendor, version="v2")
    result = ota_mod.download_update(doc, root=tmp_path)
    assert result["staged"] == 0
    assert result["bytes"] == 0


def test_apply_installs_catalog_into_selector(tmp_path, vendor, monkeypatch):
    """A real apply swaps the catalog that the canonical selector reads."""
    from steward import models as models_mod
    from steward.detect import HardwareProfile
    from steward.selector import select_bundle

    original = dict(models_mod.MODELS)
    monkeypatch.setattr(ota_mod, "_hardware_signature", lambda root=None: "sha256:hw")
    monkeypatch.setattr(ota_mod, "_plan_dict", lambda: dict(FIXED_PLAN))
    try:
        doc = manifest_doc(vendor, version="v2",
                           models=[model_entry("ota-test-model:7b", 5.0)])
        result = ota_mod.apply_update(doc, lambda: {"ok": True}, root=tmp_path)
        assert result["applied"] is True

        assert "ota-test-model:7b" in models_mod.MODELS
        assert "qwen3:8b" not in models_mod.MODELS     # old generation gone
        assert "ota-test-model:7b" in {m.id for m in models_mod.all_models()}

        # The canonical selector now plans around the OTA generation.
        profile = HardwareProfile(os_name="linux", arch="x86_64", ram_gb=24.0,
                                  gpu_kind="cpu")
        profile.unified_gb = 0.0
        bundle = select_bundle(profile)
        assert bundle.capability.id == "ota-test-model:7b"
    finally:
        models_mod.MODELS.clear()
        models_mod.MODELS.update(original)


# -- hardware-change re-selection --------------------------------------------


def test_reselect_on_hardware_change(tmp_path, vendor, ota_harness, monkeypatch):
    sig = {"value": "sha256:hwA"}
    monkeypatch.setattr(ota_mod, "_hardware_signature", lambda root=None: sig["value"])

    first = ota_mod.reselect_if_hardware_changed(tmp_path)
    assert first["changed"] is True
    assert first["plan"] == FIXED_PLAN
    st = current(tmp_path)
    assert st["hardware"] == "sha256:hwA"
    assert st["plan"]["capability"] == "qwen-x:7b"

    same = ota_mod.reselect_if_hardware_changed(tmp_path)
    assert same["changed"] is False

    sig["value"] = "sha256:hwB"     # GPU swapped or RAM added
    changed = ota_mod.reselect_if_hardware_changed(tmp_path)
    assert changed["changed"] is True
    assert current(tmp_path)["hardware"] == "sha256:hwB"


def test_entry_to_model_round_trip(vendor):
    entry = model_entry("qwen-x:14b", q4=9.0, tok=(12, 90))
    model = ota_mod.entry_to_model(entry)
    assert model.id == "qwen-x:14b"
    assert model.q4_gb == 9.0
    assert model.tok_range == (12, 90)
    assert model.license == "Apache-2.0"
    with pytest.raises(ota_mod.MalformedManifest):
        ota_mod.entry_to_model({"id": "incomplete"})


# ---------------------------------------------------------------------------
# Manifest schema v2: the signed envelope carries the app too
# ---------------------------------------------------------------------------

def app_block(version="1.1.0", key=None, payload=None, **over) -> dict:
    block = {
        "version": version,
        "released": "2026-10-28",
        "notes": "app update for tests",
        "min_source_version": "1.0.0",
        "payloads": {key or "linux-aarch64": payload or {
            "url": "steward-app.bin", "sha256": "0" * 64, "size": 0}},
    }
    block.update(over)
    return block


@pytest.fixture
def fixed_ota(monkeypatch):
    monkeypatch.setattr(ota_mod, "_hardware_signature", lambda root=None: "sha256:testhw")
    monkeypatch.setattr(ota_mod, "_plan_dict", lambda: dict(FIXED_PLAN))
    # Never touch the process-global catalog: these tests are about the app
    # payload and the pointer, so the model install is a recorded no-op that
    # cannot poison later selector/wizard tests in the same process.
    monkeypatch.setattr(ota_mod, "_install_models",
                        lambda entries: [m["id"] for m in entries])
    return None


def make_app_binary(path: Path, version: str, broken: bool = False) -> bytes:
    """An executable fake app: `path version` prints `steward <version>`."""
    body = (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "print('steward %s')\n"
        % ("0.0.0-broken" if broken else version)
    )
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path.read_bytes()


def test_v1_manifest_without_app_still_verifies(tmp_path, vendor):
    doc = manifest_doc(vendor, version="v1")
    payload = ota_mod.verify_manifest(doc)
    assert payload["version"] == "v1"
    assert "app" not in payload
    path = write_json(tmp_path, "manifest.json", doc)
    info = ota_mod.check_update(str(path), root=tmp_path)
    assert info["catalog"]["available"] is True
    assert info["app"]["reason"] == "manifest has no app block"
    assert info["app"]["latest_version"] is None


def test_malformed_app_block_rejected(tmp_path, vendor, fixed_ota):
    for bad in (
        app_block(payloads="nope"),
        app_block(notes=""),
        app_block(min_source_version=7),
        app_block(payloads={"linux-x86_64": {"url": "x.bin", "size": 1}}),
        app_block(payloads={"linux-x86_64": {"url": "x.bin",
                                             "sha256": "z" * 64, "size": 1}}),
        app_block(payloads={"linux-x86_64": {"url": "x.bin",
                                             "sha256": "0" * 64, "size": -3}}),
    ):
        doc = manifest_doc(vendor, version="c1", app=bad)
        with pytest.raises(ota_mod.MalformedManifest):
            ota_mod.verify_manifest(doc)


def test_check_update_reports_app_and_catalog(tmp_path, vendor, monkeypatch):
    key = ota_mod.platform_key(tmp_path)
    blob = b"x" * 100
    doc = manifest_doc(vendor, version="c2", app=app_block(
        version="1.3.0", key=key, payload={"url": "app.bin",
                                           "sha256": hashlib.sha256(blob).hexdigest(),
                                           "size": len(blob)}))
    path = write_json(tmp_path, "manifest.json", doc)
    info = ota_mod.check_update(str(path), root=tmp_path)
    assert info["state"] == "ok"
    assert info["available"] is True
    assert info["catalog"]["latest_version"] == "c2"
    assert info["app"]["latest_version"] == "1.3.0"
    assert info["app"]["payload_for_platform"] is True
    assert info["app"]["available"] is True


def test_check_update_semver_no_downgrade(tmp_path, vendor):
    key = ota_mod.platform_key(tmp_path)
    blob = b"y" * 32
    doc = manifest_doc(vendor, version="c2", app=app_block(
        version="0.9.0", key=key,
        payload={"url": "app.bin", "sha256": hashlib.sha256(blob).hexdigest(),
                 "size": len(blob)}))
    path = write_json(tmp_path, "manifest.json", doc)
    info = ota_mod.check_update(str(path), root=tmp_path)
    assert info["app"]["available"] is False  # a downgrade is not an update
    assert "already on the newest app version" in info["app"]["reason"]


def test_check_update_min_source_gate(tmp_path, vendor, monkeypatch):
    key = ota_mod.platform_key(tmp_path)
    blob = b"z" * 32
    monkeypatch.setattr(ota_mod, "installed_app_version", lambda: "1.1.0")
    # A release requiring a source we are newer than is installable.
    ok_doc = manifest_doc(vendor, version="c2", app=app_block(
        version="9.9.9", min_source_version="1.0.0", key=key,
        payload={"url": "app.bin", "sha256": hashlib.sha256(blob).hexdigest(),
                 "size": len(blob)}))
    info = ota_mod.check_update(str(write_json(tmp_path, "ok.json", ok_doc)),
                                root=tmp_path)
    assert info["app"]["available"] is True
    # A release requiring a source newer than installed is gated off.
    gate_doc = manifest_doc(vendor, version="c3", app=app_block(
        version="9.9.9", min_source_version="2.0.0", key=key,
        payload={"url": "app.bin", "sha256": hashlib.sha256(blob).hexdigest(),
                 "size": len(blob)}))
    info2 = ota_mod.check_update(str(write_json(tmp_path, "gate.json", gate_doc)),
                                 root=tmp_path)
    assert info2["app"]["available"] is False
    assert "requires source" in info2["app"]["reason"]


def test_app_stage_download_verifies_and_refuses(tmp_path, vendor, fixed_ota):
    key = ota_mod.platform_key(tmp_path)
    blob = b"REAL-APP-BYTES" * 10
    doc = manifest_doc(vendor, version="c1", app=app_block(
        key=key, payload={"url": "steward-app.bin",
                          "sha256": hashlib.sha256(blob).hexdigest(),
                          "size": len(blob)}))
    server, base = serve({
        "/manifest.json": json.dumps(doc, sort_keys=True).encode("utf-8"),
        "/steward-app.bin": blob,
    })
    staged = ota_mod.stage_app_payload(doc, source=base + "/manifest.json",
                                       root=tmp_path)
    assert staged["staged"] is True
    staged_path = Path(staged["path"])
    assert staged_path.read_bytes() == blob
    assert staged["version"] == "1.1.0"
    # A payload whose bytes do not match the manifest sha256 is refused. A
    # fresh data root is used so download.fetch cannot short-circuit on the
    # already-verified file from the first stage.
    server.shutdown()
    fresh = tmp_path / "fresh"
    server2, base2 = serve({
        "/manifest.json": json.dumps(doc, sort_keys=True).encode("utf-8"),
        # Same byte length (140) as the real blob so the size check passes and
        # the sha256 check itself is what refuses the tampered payload.
        "/steward-app.bin": b"E" * 140,
    })
    with pytest.raises(dl.ChecksumMismatch):
        ota_mod.stage_app_payload(doc, source=base2 + "/manifest.json", root=fresh)
    assert not list((fresh / "ota" / "staging" / "app").glob("steward-app.bin"))
    server2.shutdown()


class _RangeServer(threading.Thread):
    """A loopback HTTP server that honors Range requests for one body."""

    def __init__(self, body: bytes) -> None:
        super().__init__(daemon=True)
        self.body = body
        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                    _RangeHandler)

    @property
    def port(self) -> int:
        return self._srv.server_address[1]

    def run(self) -> None:
        _RangeHandler.bodies = {"/manifest.json": b"{}",
                                "/big.bin": self.body}
        self._srv.serve_forever()

    def shutdown(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    bodies: dict = {}

    def log_message(self, format, *args):  # quiet the test server
        pass

    def do_GET(self):  # noqa: N802 - stdlib name
        body = self.bodies.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            start = int(rng[6:].split("-")[0])
            body = body[start:]
            self.send_response(206)
            self.send_header("Content-Range",
                             "bytes %d-%d/%d" % (0, len(body) - 1, len(body)))
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


def _range_server(body: bytes) -> _RangeServer:
    server = _RangeServer(body)
    server.start()
    time.sleep(0.02)
    return server


def test_app_stage_cancel_and_resume(tmp_path, vendor, fixed_ota):
    key = ota_mod.platform_key(tmp_path)
    blob = b"B" * (1 << 20) * 8  # 8 MiB so a cancel lands mid-transfer
    doc = manifest_doc(vendor, version="c1", app=app_block(
        key=key, payload={"url": "big.bin",
                          "sha256": hashlib.sha256(blob).hexdigest(),
                          "size": len(blob)}))

    server = _range_server(blob)
    try:
        src = "http://127.0.0.1:%d/manifest.json" % server.port
        scope = dl.CancelScope()
        grab = {"n": 0}

        def canceller(done, expected):
            grab["n"] += 1
            if grab["n"] == 3:
                scope.cancel()

        with pytest.raises(dl.Cancelled):
            ota_mod.stage_app_payload(doc, source=src, root=tmp_path,
                                      progress=canceller, cancel=scope)
        part = tmp_path / "ota" / "staging" / "app" / "big.bin.part"
        assert part.exists()          # cancellation keeps the partial
        assert not Path(staged_name(tmp_path)).exists()
        # A later run resumes from the .part and verifies to completion.
        scope.reset()
        calls = []

        def progress(done, expected):
            calls.append((done, expected))

        staged = ota_mod.stage_app_payload(doc, source=src, root=tmp_path,
                                           progress=progress, cancel=scope)
        assert staged["staged"] is True
        assert Path(staged["path"]).read_bytes() == blob
        assert not part.exists()      # the .part was consumed by the rename
        assert calls                   # progress was reported
    finally:
        server.shutdown()


def staged_name(tmp_path) -> str:
    return str(tmp_path / "ota" / "staging" / "app" / "big.bin")


def test_app_apply_swaps_and_answers(tmp_path, vendor, fixed_ota):
    key = ota_mod.platform_key(tmp_path)
    blob = make_app_binary(tmp_path / "steward-1.1.0", "1.1.0")
    doc = manifest_doc(vendor, version="c1", app=app_block(
        version="1.1.0", key=key, payload={
            "url": "steward-1.1.0",
            "sha256": hashlib.sha256(blob).hexdigest(),
            "size": len(blob)}))
    server, base = serve({
        "/manifest.json": json.dumps(doc, sort_keys=True).encode("utf-8"),
        "/steward-1.1.0": blob,
    })
    staged = ota_mod.stage_app_payload(doc, source=base + "/manifest.json",
                                       root=tmp_path)
    result = ota_mod.apply_update(doc, ota_mod.app_binary_probe(
        staged["path"], staged["version"]), root=tmp_path)
    assert result["applied"] is True
    assert result["app"]["installed"] is True
    st = current(tmp_path)
    assert st["app"]["version"] == "1.1.0"
    assert st["app"]["binary"] == "steward-1.1.0"
    # The active binary answers with its version.
    import subprocess
    out = subprocess.run([ota_mod.app_binary_path(tmp_path), "version"],
                         capture_output=True, text=True).stdout
    assert "1.1.0" in out
    server.shutdown()


def test_app_rollback_failed_probe_old_binary_answers(
        tmp_path, vendor, fixed_ota):
    import subprocess
    key = ota_mod.platform_key(tmp_path)
    old_blob = make_app_binary(tmp_path / "steward-1.0.0", "1.0.0")
    old_doc = manifest_doc(vendor, version="c1", app=app_block(
        version="1.0.0", key=key, payload={
            "url": "steward-1.0.0",
            "sha256": hashlib.sha256(old_blob).hexdigest(),
            "size": len(old_blob)}))
    # The NEW payload is broken: it always answers 0.0.0-broken.
    new_blob = make_app_binary(tmp_path / "steward-1.1.0", "1.1.0", broken=True)
    new_doc = manifest_doc(vendor, version="c2", app=app_block(
        version="1.1.0", key=key, payload={
            "url": "steward-1.1.0",
            "sha256": hashlib.sha256(new_blob).hexdigest(),
            "size": len(new_blob)}))
    server, base = serve({
        "/manifest.json": json.dumps(old_doc, sort_keys=True).encode("utf-8"),
        "/steward-1.0.0": old_blob,
        "/steward-1.1.0": new_blob,
        "/manifest2.json": json.dumps(new_doc, sort_keys=True).encode("utf-8"),
    })
    ota_mod.stage_app_payload(old_doc, source=base + "/manifest.json", root=tmp_path)
    assert ota_mod.apply_update(old_doc, ota_mod.app_binary_probe(
        staged_for(tmp_path, "steward-1.0.0"), "1.0.0"),
        root=tmp_path)["applied"]
    out = subprocess.run([ota_mod.app_binary_path(tmp_path), "version"],
                         capture_output=True, text=True).stdout
    assert "1.0.0" in out

    # Stage and apply the broken 1.1.0: the health probe must fail it.
    ota_mod.stage_app_payload(new_doc, source=base + "/manifest2.json", root=tmp_path)
    result = ota_mod.apply_update(new_doc, ota_mod.app_binary_probe(
        staged_for(tmp_path, "steward-1.1.0"), "1.1.0"),
        root=tmp_path, timeout=0.5)
    assert result["applied"] is False
    assert result["rolled_back"] is True
    st = current(tmp_path)
    assert st["app"]["version"] == "1.0.0"      # pointer restored to the old app
    assert st["active_catalog"] == "c1"
    # The OLD binary still answers after the rollback.
    assert not (tmp_path / "ota" / "app" / "steward-1.1.0").exists()
    out = subprocess.run([ota_mod.app_binary_path(tmp_path), "version"],
                         capture_output=True, text=True).stdout
    assert "1.0.0" in out
    server.shutdown()


def staged_for(tmp_path, name) -> str:
    return str(tmp_path / "ota" / "staging" / "app" / name)


def test_app_apply_refuses_corrupted_staged_payload(tmp_path, vendor, fixed_ota):
    key = ota_mod.platform_key(tmp_path)
    blob = make_app_binary(tmp_path / "steward-x", "9.9.9")
    doc = manifest_doc(vendor, version="c1", app=app_block(
        version="9.9.9", key=key, payload={
            "url": "steward-x",
            "sha256": hashlib.sha256(blob).hexdigest(),
            "size": len(blob)}))
    server, base = serve({
        "/manifest.json": json.dumps(doc, sort_keys=True).encode("utf-8"),
        "/steward-x": blob,
    })
    ota_mod.stage_app_payload(doc, source=base + "/manifest.json", root=tmp_path)
    # Corrupt the verified staged bytes on disk; apply must refuse.
    staged = Path(staged_for(tmp_path, "steward-x"))
    staged.write_bytes(b"tampered-after-staging")
    with pytest.raises(ota_mod.ChecksumError):
        ota_mod.apply_update(doc, lambda: {"ok": True}, root=tmp_path)
    st = current(tmp_path)
    assert not st or "app" not in st              # nothing committed
    server.shutdown()


def test_kill_during_app_swap_leaves_old_app_usable(tmp_path, vendor, fixed_ota):
    import subprocess
    key = ota_mod.platform_key(tmp_path)
    old_blob = make_app_binary(tmp_path / "steward-1.0.0", "1.0.0")
    old_doc = manifest_doc(vendor, version="c1", app=app_block(
        version="1.0.0", key=key, payload={
            "url": "steward-1.0.0",
            "sha256": hashlib.sha256(old_blob).hexdigest(),
            "size": len(old_blob)}))
    new_blob = make_app_binary(tmp_path / "steward-2.0.0", "2.0.0")
    new_doc = manifest_doc(vendor, version="c2", app=app_block(
        version="2.0.0", key=key, payload={
            "url": "steward-2.0.0",
            "sha256": hashlib.sha256(new_blob).hexdigest(),
            "size": len(new_blob)}))
    server, base = serve({
        "/manifest.json": json.dumps(old_doc, sort_keys=True).encode("utf-8"),
        "/steward-1.0.0": old_blob,
        "/manifest2.json": json.dumps(new_doc, sort_keys=True).encode("utf-8"),
        "/steward-2.0.0": new_blob,
    })
    # Commit 1.0.0 first so there is an old app to protect.
    ota_mod.stage_app_payload(old_doc, source=base + "/manifest.json", root=tmp_path)
    assert ota_mod.apply_update(old_doc, lambda: {"ok": True},
                                root=tmp_path)["applied"]

    # Stage 2.0.0 in-process (needs the HTTP server), then kill the swap in a
    # subprocess at the app-installed checkpoint, BEFORE the pointer commits.
    ota_mod.stage_app_payload(new_doc, source=base + "/manifest2.json", root=tmp_path)
    new_doc_path = write_json(tmp_path, "new.json", new_doc)
    worktree_src = str(Path(__file__).resolve().parents[1] / "src")
    code = (
        "import json, os, sys\n"
        "sys.path.insert(0, os.environ['STEWARD_PYTHONPATH'])\n"
        "import steward.license as lic\n"
        "lic.VENDOR_PUBLIC_KEY_HEX = os.environ['STEWARD_TEST_PUB']\n"
        "from steward import ota as ota_mod\n"
        "doc = json.load(open(sys.argv[1], encoding='utf-8'))\n"
        "ota_mod._kill_hooks = {'app-installed': lambda: os._exit(9)}\n"
        "ota_mod.apply_update(doc, lambda: {'ok': True}, root=None)\n"
    )
    env = dict(os.environ)
    env.update({
        "STEWARD_HOME": str(tmp_path),
        "STEWARD_PYTHONPATH": worktree_src,
        "STEWARD_TEST_PUB": vendor["pub"],
    })
    proc = subprocess.run([sys.executable, "-c", code, str(new_doc_path)],
                          capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 9   # killed right after the new app file was placed

    st = current(tmp_path)
    assert st["app"]["version"] == "1.0.0"        # pointer never committed 2.0.0
    assert st["app"]["binary"] == "steward-1.0.0"
    pointer_text = (tmp_path / "ota" / "current.json").read_text(encoding="utf-8")
    assert '"binary": "steward-2.0.0"' not in pointer_text
    # The old binary still answers; the orphaned new file is never referenced.
    out = subprocess.run([ota_mod.app_binary_path(tmp_path), "version"],
                         capture_output=True, text=True).stdout
    assert "1.0.0" in out
    server.shutdown()


def test_app_pointer_survives_catalog_only_apply(tmp_path, vendor, fixed_ota):
    key = ota_mod.platform_key(tmp_path)
    blob = make_app_binary(tmp_path / "steward-1.1.0", "1.1.0")
    payload_entry = {"url": "steward-1.1.0",
                     "sha256": hashlib.sha256(blob).hexdigest(),
                     "size": len(blob)}
    doc = manifest_doc(vendor, version="c1", app=app_block(
        version="1.1.0", key=key, payload=payload_entry))
    server, base = serve({
        "/manifest.json": json.dumps(doc, sort_keys=True).encode("utf-8"),
        "/steward-1.1.0": blob,
    })
    ota_mod.stage_app_payload(doc, source=base + "/manifest.json", root=tmp_path)
    assert ota_mod.apply_update(doc, lambda: {"ok": True},
                                root=tmp_path)["applied"]
    assert current(tmp_path)["app"]["version"] == "1.1.0"

    # A catalog-only apply whose manifest carries the SAME already-active app
    # block must not clobber the app pointer.
    doc2 = manifest_doc(vendor, version="c2", app=app_block(
        version="1.1.0", key=key, payload=payload_entry))
    result = ota_mod.apply_update(doc2, lambda: {"ok": True}, root=tmp_path)
    assert result["applied"] is True
    st = current(tmp_path)
    assert st["active_catalog"] == "c2"
    assert st["app"]["version"] == "1.1.0"          # preserved
    assert st["app"]["binary"] == "steward-1.1.0"
    server.shutdown()


def test_resolve_source_default_and_override(monkeypatch):
    monkeypatch.delenv("STEWARD_OTA_URL", raising=False)
    from steward.config import DEFAULT_OTA_URL
    assert ota_mod.resolve_source() == DEFAULT_OTA_URL
    monkeypatch.setenv("STEWARD_OTA_URL", "https://example.test/update.json")
    assert ota_mod.resolve_source() == "https://example.test/update.json"
