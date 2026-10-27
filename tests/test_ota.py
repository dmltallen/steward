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
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from steward import license as lic  # noqa: E402
from steward import ota as ota_mod    # noqa: E402

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
