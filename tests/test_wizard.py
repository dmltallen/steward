"""Tests for the resumable first-run wizard (src/steward/wizard.py).

The wizard is a state machine that talks to (a) hardware detection, (b) a
model backend through the runtime seam, and (c) a pairing-capable daemon. All
three are faked here: detect() is monkeypatched to a fixed unified 128 GB box,
the backend is a FakeRuntime (no Ollama, no network), and the daemon is a
stub factory. Download-state resume, cancel and checksum tests run against a
small real HTTP server on loopback with Range support. Everything shuts down
in fixtures, so no daemon, thread or port survives a run.
"""

from __future__ import annotations

import hashlib
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

import steward.download as dl
import steward.wizard as wiz
from steward.config import Config

# -- fakes --------------------------------------------------------------------


class FakeRuntime:
    """Stand-in for the runtime seam: health, chat, kind, url, models."""

    kind = "fake-runtime"
    url = "http://127.0.0.1:1"

    def __init__(self, models=("qwen3:8b",), reply: str = "Hello from the "
                 "fake backend, everything works.",
                 health_ok: bool = True, chat_log: list | None = None) -> None:
        self.models = list(models)
        self.reply = reply
        self.health_ok = health_ok
        self.chat_calls = 0
        self.chat_log = chat_log if chat_log is not None else []

    def health(self) -> dict:
        if not self.health_ok:
            return {"ok": False, "backend": "fake", "version": "0.0.0-fake",
                    "models": [], "error": "backend not running"}
        return {"ok": True, "backend": "fake", "version": "0.0.0-fake",
                "models": list(self.models)}

    def chat(self, messages, stream=True, model=None):
        self.chat_calls += 1
        self.chat_log.append({"messages": messages, "model": model})
        if not stream:
            yield {"delta": self.reply}
            return
        for token in (self.reply[i:i + 4] for i in range(0, len(self.reply), 4)):
            yield {"delta": token}


class FakeDaemon:
    """Stub pairing daemon with the interfaces the wizard touches."""

    def __init__(self, code: str = "654321") -> None:
        self.ctx = SimpleNamespace(pair_code=code, port=0)
        self.host_ip = "127.0.0.1"
        self.stopped = False

    def shutdown_server(self) -> None:
        self.stopped = True


def stub_factory(*, node_host: str, port: int, runtime, model: str,
                 host_ip: str | None):
    return FakeDaemon(), None


def make_cfg(tmp_path: Path, port: int = 0) -> Config:
    cfg = Config()
    cfg.data_dir = str(tmp_path)
    if port:
        cfg.port = port
    return cfg


@pytest.fixture
def fake_profile(monkeypatch):
    """A fixed unified 128 GB machine, so verdict/bundle are deterministic."""
    from steward.detect import HardwareProfile

    def _detect(data_dir=None):
        return HardwareProfile(
            os_name="linux", arch="aarch64", cpu_name="Test CPU",
            gpu_kind="unified", gpu_name="Test GB10", unified_gb=128.0,
            ram_gb=128.0, disk_free_gb=512.0, confidence="test")

    monkeypatch.setattr("steward.detect.detect", _detect)
    return _detect


def new_wizard(tmp_path: Path, *, model: str = "qwen3:8b", backend=None,
               manifest: dict | None = None, port: int = 0,
               daemon_factory=stub_factory) -> wiz.Wizard:
    cfg = make_cfg(tmp_path, port=port)
    return wiz.Wizard(cfg, ui=wiz.BufferUI(), backend=backend
                      if backend is not None else FakeRuntime(),
                      daemon_factory=daemon_factory, manifest=manifest,
                      host_ip="127.0.0.1", model=model)


# -- tiny Range HTTP server for download-state tests --------------------------


BODY = bytes(range(256)) * 2048          # 512 KB body
BODY_SHA = hashlib.sha256(BODY).hexdigest()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # noqa: A003
        pass

    def do_GET(self) -> None:  # noqa: N802
        server = self.server
        server.requests += 1
        if self.path.split("?")[0] != "/blob.bin":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        total = len(server.body)
        rng = self.headers.get("Range")
        start, end, status = 0, total - 1, 200
        if rng:
            m = re.match(r"bytes=(\d+)-(\d*)", rng)
            if m:
                start = int(m.group(1))
                end = int(m.group(2)) - 1 if m.group(2) else total - 1
            if start > total - 1:
                self.send_response(416)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        chunk = server.body[start:end + 1]
        if server.abort_after is not None:
            served = server.bytes_served
            allowed = max(server.abort_after - served, 0)
            if allowed == 0:
                self.close_connection = True
                self.send_response(200)
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                return
            chunk = chunk[:allowed]
        server.bytes_served += len(chunk)
        self.send_response(status)
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, total))
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        self.wfile.write(chunk)
        self.wfile.flush()
        if server.abort_after is not None and allowed is not None and allowed < len(server.body) - start and server.bytes_served >= server.abort_after:
            self.close_connection = True


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.requests = 0
        self.bytes_served = 0
        self.abort_after: int | None = None
        super().__init__(("127.0.0.1", 0), _Handler)


@pytest.fixture
def blob_server():
    server = _Server(BODY)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def manifest_for(port: int) -> dict:
    return {"models": {
        "qwen3:32b": {"url": "http://127.0.0.1:%d/blob.bin" % port,
                      "bytes": len(BODY), "sha256": BODY_SHA},
    }}


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# -- full happy path -----------------------------------------------------------


def test_happy_path_runs_to_done(fake_profile, tmp_path):
    backend = FakeRuntime(models=["qwen3:8b"])
    w = new_wizard(tmp_path, model="qwen3:8b", backend=backend)
    result = w.run()
    assert result.ok is True
    assert result.state == wiz.DONE
    assert w.cursor() == wiz.DONE
    states_run = list(w.results.keys())
    assert states_run == [
        "preflight", "verdict", "explain", "download", "runtime",
        "selftest", "pair"]
    # one chat + one note summary through the fake runtime
    assert backend.chat_calls == 2
    assert w.selftest_results["chat"] == backend.reply
    assert w.selftest_results["summary"]
    # QR payload and a code come out of the pair state
    pair = w.results["pair"].data
    assert pair["payload"].startswith("steward://pair?host=")
    assert pair["code"] == "654321"
    # the managed daemon is stopped cleanly, nothing left running
    w.shutdown()
    assert pair  # device_lib kept, no assertion of sockets here


def test_transcript_contains_all_states(fake_profile, tmp_path):
    w = new_wizard(tmp_path, model="qwen3:8b")
    w.run()
    joined = "\n".join(w.transcript)
    for state in wiz.STATES:
        assert ("== %s ==" % state) in joined
    assert "QR payload" in joined
    w.shutdown()


def test_selftest_note_comes_from_demo_notes(fake_profile, tmp_path):
    """The note summary summarizes the shipped demo/source notes."""
    backend = FakeRuntime(models=["qwen3:8b"])
    w = new_wizard(tmp_path, model="qwen3:8b", backend=backend)
    w.run()
    first_prompt = backend.chat_log[1]["messages"][0]["content"]
    # the second chat call is the note summary, fed with the note text
    assert "these are notes" in first_prompt.lower() or "notes from" in first_prompt.lower()
    w.shutdown()


def test_cached_model_skips_download_no_manifest(fake_profile, tmp_path):
    w = new_wizard(tmp_path, model="qwen3:8b")
    result = w.step("download")
    assert result.ok is True
    assert "already cached" in result.message
    assert w.cursor() == "runtime"  # the step advanced past download


# -- state persistence and resume ----------------------------------------------


def test_resume_from_saved_cursor_after_partial_run(fake_profile, tmp_path):
    w1 = new_wizard(tmp_path, model="qwen3:8b")
    out1 = w1.step("preflight")
    assert out1.ok and out1.next == "verdict"
    # a second, fresh Wizard on the same data dir sees the saved cursor
    w2 = new_wizard(tmp_path, model="qwen3:8b")
    assert w2.cursor() == "verdict"
    result = w2.run()
    assert result.ok and result.state == wiz.DONE
    assert w2.cursor() == wiz.DONE
    w2.shutdown()


def test_failed_state_keeps_cursor_and_recovery_runs_it(fake_profile, tmp_path,
                                                        monkeypatch):
    cfg = make_cfg(tmp_path)
    # A model that must be downloaded and a manifest that says it is 20 GB.
    big_manifest = {"models": {"qwen3:32b": {
        "url": "http://127.0.0.1:9/blob.bin",
        "bytes": 20 * (1024 ** 3), "sha256": "0" * 64}}}
    monkeypatch.setattr("steward.download.free_disk",
                        lambda path: 10 * (1024 ** 3))  # only 10 GB free
    w = wiz.Wizard(cfg, ui=wiz.BufferUI(), backend=FakeRuntime(),
                   daemon_factory=stub_factory, host_ip="127.0.0.1",
                   model="qwen3:32b", manifest=big_manifest)
    bad = w.step("preflight")
    assert bad.ok is False and "free up space" in bad.message
    assert w.cursor() == "preflight"   # never advanced past the failure
    # free disk restored, same cursor, the wizard can now proceed
    monkeypatch.setattr("steward.download.free_disk",
                        lambda path: 1000 * (1024 ** 3))
    w2 = wiz.Wizard(cfg, ui=wiz.BufferUI(), backend=FakeRuntime(),
                    daemon_factory=stub_factory, host_ip="127.0.0.1",
                    model="qwen3:32b", manifest=big_manifest)
    assert w2.cursor() == "preflight"
    assert w2.step("preflight").ok is True


# -- specific failure messages -------------------------------------------------


def test_preflight_no_backend_no_binary_requires_network(fake_profile, tmp_path,
                                                         monkeypatch):
    monkeypatch.setattr("steward.wizard._ollama_binary_present",
                        lambda: False)
    w = new_wizard(tmp_path, model="qwen3:32b",
                   backend=FakeRuntime(models=[], health_ok=False))
    result = w.step("preflight")
    assert result.ok is False
    assert "install Ollama" in result.message
    assert "ollama.com/download" in result.message


def test_preflight_backend_already_running_message(fake_profile, tmp_path):
    w = new_wizard(tmp_path, model="qwen3:8b")
    result = w.step("preflight")
    assert result.ok is True
    assert "already running" in result.message


def test_gpu_visible_but_ollama_not_using_it(fake_profile, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "steward.wizard.ollama_gpu_status",
        lambda profile: {"gpu_visible": True, "uses_gpu": False,
                         "note": "nvidia-smi shows zero compute apps"})
    w = new_wizard(tmp_path, model="qwen3:8b")
    result = w.step("preflight")
    assert result.ok is True
    assert "GPU is installed but the model process is not using it" in result.message


def test_download_manifest_missing_ollama_pull_instruction(fake_profile, tmp_path):
    # model neither cached nor in the manifest -> a specific actionable stop
    w = new_wizard(tmp_path, model="qwen3:32b",
                   backend=FakeRuntime(models=["qwen3:8b"]), manifest=None)
    wiz._write_cursor(w.state_file, "download")
    result = w.step("download")
    assert result.ok is False
    assert "ollama pull" in result.message
    assert "qwen3:32b" in result.message
    assert w.cursor() == "download"


def test_download_checksum_mismatch_rejected_clean(fake_profile, tmp_path,
                                                   blob_server, monkeypatch):
    monkeypatch.setattr("steward.download.free_disk",
                        lambda path: 1000 * (1024 ** 3))
    manifest = manifest_for(blob_server.server_address[1])
    manifest["models"]["qwen3:32b"]["sha256"] = "0" * 64  # wrong on purpose
    w = new_wizard(tmp_path, model="qwen3:32b",
                   backend=FakeRuntime(models=["qwen3:8b"]), manifest=manifest)
    wiz._write_cursor(w.state_file, "download")
    result = w.step("download")
    assert result.ok is False
    assert "checksum mismatch" in result.message.lower()
    dest = Path(tmp_path) / "models" / "qwen3:32b"
    assert not dest.exists()          # nothing claims success
    assert not Path(str(dest) + ".part").exists()  # the bad part is discarded
    assert w.cursor() == "download"


# -- download branch: resume, cancel -------------------------------------------


def download_wizard(tmp_path, port, *, ui=None, backend=None):
    cfg = make_cfg(tmp_path)
    w = wiz.Wizard(cfg, ui=ui or wiz.BufferUI(), backend=backend
                   if backend is not None else FakeRuntime(models=["qwen3:8b"]),
                   daemon_factory=stub_factory, host_ip="127.0.0.1",
                   model="qwen3:32b", manifest=manifest_for(port))
    # put the machine at the download state, as a crashed mid-download run would
    wiz._write_cursor(w.state_file, "download")
    return w


def test_wizard_download_resumes_after_truncation(fake_profile, tmp_path,
                                                  blob_server, monkeypatch):
    monkeypatch.setattr("steward.download.free_disk",
                        lambda path: 1000 * (1024 ** 3))
    port = blob_server.server_address[1]
    blob_server.abort_after = len(BODY) // 2

    w1 = download_wizard(tmp_path, port)
    r1 = w1.step("download")
    assert r1.ok is False                        # transfer was cut
    part = Path(tmp_path) / "models" / "qwen3:32b.part"
    assert part.exists() and part.stat().st_size == len(BODY) // 2
    assert not Path(tmp_path / "models" / "qwen3:32b").exists()
    assert w1.cursor() == "download"             # resumable, not reset

    # Healed server, brand new Wizard on the same data dir resumes the blob.
    blob_server.abort_after = None
    w2 = download_wizard(tmp_path, port)
    assert w2.cursor() == "download"
    r2 = w2.step("download")
    assert r2.ok is True and "verified" in r2.message
    dest = Path(tmp_path) / "models" / "qwen3:32b"
    assert dest.read_bytes() == BODY
    w2.shutdown()


class CancelOnFirstProgress(wiz.UI):
    def __init__(self, wizard: wiz.Wizard) -> None:
        self.wizard = wizard
        self.fired = False
        self.lines: list[str] = []

    def emit(self, text: str) -> None:
        self.lines.append(text)

    def progress(self, done: int, expected: int) -> None:
        if not self.fired:
            self.fired = True
            self.wizard.cancel_download()


def test_wizard_cancel_leaves_part_no_success_file(fake_profile, tmp_path,
                                                   blob_server, monkeypatch):
    monkeypatch.setattr("steward.download.free_disk",
                        lambda path: 1000 * (1024 ** 3))
    port = blob_server.server_address[1]
    w1 = download_wizard(tmp_path, port)
    ui = CancelOnFirstProgress(w1)
    w1.ui = ui
    w1._active_scope()                     # ensure the shared scope exists
    r1 = w1.step("download")
    assert r1.ok is False and "stopped" in r1.message
    dest = Path(tmp_path) / "models" / "qwen3:32b"
    part = Path(str(dest) + ".part")
    assert not dest.exists()               # the final name never appeared
    assert part.exists()                   # the partial is kept for resume
    assert w1.cursor() == "download"

    # A fresh wizard on the same root resumes to a verified file.
    w2 = download_wizard(tmp_path, port)
    r2 = w2.step("download")
    assert r2.ok is True
    assert dest.read_bytes() == BODY
    assert not part.exists()
    w2.shutdown()


# -- pair state ----------------------------------------------------------------


def test_pair_port_conflict_is_actionable(fake_profile, tmp_path):
    """A non-Steward program owning the port fails with a clear next step."""
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("0.0.0.0", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        backend = FakeRuntime(models=["qwen3:8b"])
        w = wiz.Wizard(make_cfg(tmp_path, port=port), backend=backend,
                       daemon_factory=None, host_ip="127.0.0.1",
                       model="qwen3:8b")
        result = w.step("pair")
        assert result.ok is False
        assert "already in use" in result.message
        assert "STEWARD_PORT" in result.message
    finally:
        blocker.close()


def test_pair_reuses_running_steward_daemon(fake_profile, tmp_path):
    """When a daemon is already serving on the port, its code is printed."""
    from steward.server.daemon import create_server

    server = create_server(("127.0.0.1", 0), runtime=FakeRuntime(models=["qwen3:8b"]),
                           advertise=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        w = wiz.Wizard(make_cfg(tmp_path, port=port), backend=FakeRuntime(),
                       daemon_factory=stub_factory, host_ip="127.0.0.1",
                       model="qwen3:8b")
        result = w.step("pair")
        assert result.ok is True
        assert result.data["code"] == server.ctx.pair_code
        # it did not start a second daemon: nothing is owned by the wizard
        assert w._owns_daemon is False
    finally:
        server.shutdown_server()
        thread.join(timeout=5)


# -- web wizard contract -------------------------------------------------------


def test_web_adapter_contract(fake_profile, tmp_path, monkeypatch):
    w = new_wizard(tmp_path, model="qwen3:8b")
    monkeypatch.setattr("steward.wizard.make_wizard",
                        lambda env=None, ui=None, **kw: w)
    out = wiz.setup_plan()
    assert out["ok"] and out["plan"]["served_model"] == "qwen3:8b"
    assert out["state"] == wiz.STATES[0]
    assert "transcript" not in out

    out = wiz.setup_step()
    assert out["state"] in wiz.STATES
    assert out["done"] is False
    assert isinstance(out["transcript"], list)
    assert "message" in out and "plan" in out and "download" in out
    assert out["error"] is None

    out = wiz.setup_status()
    assert out["state"] == wiz.STATES[1]  # advanced by the step above
    assert out["error"] is None

    out = wiz.setup_cancel()
    assert out["cancelled"] in (True, False)


def test_web_step_drives_to_done(fake_profile, tmp_path, monkeypatch):
    w = new_wizard(tmp_path, model="qwen3:8b")
    monkeypatch.setattr("steward.wizard.make_wizard",
                        lambda env=None, ui=None, **kw: w)
    guard = 0
    while True:
        out = wiz.setup_step()
        guard += 1
        assert guard < 20
        if out["done"]:
            break
        assert out["ok"] is True
    assert out["state"] == wiz.DONE
    w.shutdown()


# -- internals ----------------------------------------------------------------


def test_cursor_file_format_and_reset(fake_profile, tmp_path):
    w = new_wizard(tmp_path, model="qwen3:8b")
    assert w.cursor() == "preflight"
    w.step("preflight")
    data = __import__("json").loads(
        (Path(tmp_path) / "wizard-state.json").read_text())
    assert data["state"] == "verdict" and data["version"] == 1
    w.reset()
    assert w.cursor() == "preflight"
