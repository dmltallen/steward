"""Tests for the runtime seam: SystemOllama, SidecarOllama, and the factory.

The backend is a fake Ollama (tests/fake_ollama_bin.py): an in-process HTTP
server for SystemOllama tests, and the same server run as a standalone child
process (the spawnable fake binary) for SidecarOllama lifecycle tests. Every
spawned process and server is shut down in a finally block so no daemon or
port survives a test run.
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_ollama_bin as fake  # noqa: E402

from steward.runtime import (  # noqa: E402
    DEFAULT_OLLAMA_URL,
    BackendError,
    SidecarOllama,
    SystemOllama,
    make_runtime,
)

FAKE_BIN = str(Path(__file__).resolve().parent / "fake_ollama_bin.py")
os.chmod(FAKE_BIN, 0o755)  # spawnable in every checkout, whatever the git mode


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def fake_server():
    port = free_port()
    server = fake.run_server(port)
    try:
        yield port, server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def sidecar_env(monkeypatch):
    """Isolate sidecar binary discovery: no real binary, no env pin."""
    monkeypatch.delenv("STEWARD_OLLAMA_BIN", raising=False)
    monkeypatch.setattr("steward.runtime.SIDECAR_SEARCH", [])
    return monkeypatch


# -- SystemOllama --------------------------------------------------------------


def test_system_health_when_server_missing():
    rt = SystemOllama(url=f"http://127.0.0.1:{free_port()}")
    health = rt.health()
    assert health["ok"] is False
    assert health["backend"] == "system-ollama"
    assert health["version"] is None
    assert health["models"] == []
    assert health["error"]


def test_system_health_and_models(fake_server):
    port, _ = fake_server
    rt = SystemOllama(url=f"http://127.0.0.1:{port}")
    health = rt.health()
    assert health["ok"] is True
    assert health["version"] == "0.0.0-fake"
    assert rt.list_models() == ["fake-model", "qwen3:8b"]


def test_system_chat_streamed_delta_assembly(fake_server):
    port, _ = fake_server
    rt = SystemOllama(url=f"http://127.0.0.1:{port}")
    deltas = [d["delta"] for d in
              rt.chat([{"role": "user", "content": "hi"}], stream=True, model="qwen3:8b")]
    assert deltas == fake.REPLY_TOKENS
    assert "".join(deltas) == "".join(fake.REPLY_TOKENS)


def test_system_chat_unstreamed_single_delta(fake_server):
    port, _ = fake_server
    rt = SystemOllama(url=f"http://127.0.0.1:{port}", model="qwen3:8b")
    got = list(rt.chat([{"role": "user", "content": "hi"}], stream=False))
    assert got == [{"delta": "".join(fake.REPLY_TOKENS)}]


def test_system_chat_requires_model_no_network():
    rt = SystemOllama(url="http://127.0.0.1:1")
    with pytest.raises(BackendError):
        list(rt.chat([{"role": "user", "content": "hi"}], stream=True))


def test_system_stop_unloads(fake_server):
    port, _ = fake_server
    rt = SystemOllama(url=f"http://127.0.0.1:{port}")
    result = rt.stop("qwen3:8b")
    assert result["ok"] is True
    assert result["unloaded"] is True


def test_ensure_serving_resident_and_missing(fake_server):
    port, _ = fake_server
    rt = SystemOllama(url=f"http://127.0.0.1:{port}")
    resident = rt.ensure_serving("qwen3:8b")
    assert resident["ok"] is True and resident["resident"] is True
    missing = rt.ensure_serving("gemma4:26b")
    assert missing["ok"] is True and missing["resident"] is False
    assert missing["note"]


def test_ensure_serving_degraded_when_down():
    rt = SystemOllama(url=f"http://127.0.0.1:{free_port()}")
    result = rt.ensure_serving("qwen3:8b")
    assert result["ok"] is False and result["serving"] is False
    assert result["note"]


# -- SidecarOllama -------------------------------------------------------------


def test_sidecar_starts_owns_serves_and_stops(sidecar_env):
    rt = SidecarOllama(binary=FAKE_BIN)
    try:
        url = rt.start()
        sidecar_port = rt.port
        assert sidecar_port is not None
        assert url.startswith("http://127.0.0.1:")
        heart = rt.health()
        assert heart["ok"] is True
        assert heart["version"] == "0.0.0-fake"
        assert heart["sidecar_available"] is True
        assert heart["models"] == ["fake-model", "qwen3:8b"]
        reply = "".join(d["delta"] for d in
                        rt.chat([{"role": "user", "content": "hi"}], model="qwen3:8b"))
        assert reply == "".join(fake.REPLY_TOKENS)
    finally:
        rt.shutdown()
    assert rt._port_listening(rt.host, sidecar_port) is False


def test_sidecar_refuses_double_start(sidecar_env):
    rt = SidecarOllama(binary=FAKE_BIN)
    rt.start()
    try:
        with pytest.raises(RuntimeError, match="double-start"):
            rt.start()
        # the first instance is untouched by the refused second start
        assert rt.health()["ok"] is True
    finally:
        rt.shutdown()
    # after a clean shutdown a fresh instance starts again
    again = SidecarOllama(binary=FAKE_BIN)
    try:
        again.start()
        assert again.health()["ok"] is True
    finally:
        again.shutdown()


def test_sidecar_refuses_busy_port(sidecar_env):
    port = free_port()
    server = fake.run_server(port)
    try:
        rt = SidecarOllama(binary=FAKE_BIN, port=port)
        with pytest.raises(RuntimeError, match="already listens"):
            rt.start()
        assert rt._proc is None  # refused before spawning anything
    finally:
        server.shutdown()
        server.server_close()


def test_sidecar_binary_absent_reports_searched_paths(sidecar_env, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda *a, **k: None)
    rt = SidecarOllama(binary="/nonexistent/ollama")
    heart = rt.health()
    assert heart["ok"] is False
    assert heart["sidecar_available"] is False
    assert heart["binary_path"] is None
    assert "/nonexistent/ollama" in heart["searched_paths"]
    result = rt.ensure_serving("qwen3:8b")  # degrades, never raises
    assert result["ok"] is False
    assert "no ollama binary" in result["note"]


# -- factory -------------------------------------------------------------------


def test_default_ollama_url():
    assert DEFAULT_OLLAMA_URL == "http://localhost:11434"


def test_make_runtime_explicit_system(fake_server):
    port, _ = fake_server
    cfg = SimpleNamespace(ollama_url=f"http://127.0.0.1:{port}", ollama_bin="", model="")
    assert make_runtime(cfg, backend="system").kind == "system-ollama"


def test_make_runtime_explicit_sidecar(fake_server):
    cfg = SimpleNamespace(ollama_url="http://127.0.0.1:1", ollama_bin=FAKE_BIN, model="")
    assert make_runtime(cfg, backend="sidecar").kind == "sidecar-ollama"


def test_make_runtime_auto_prefers_system(fake_server):
    port, _ = fake_server
    cfg = SimpleNamespace(ollama_url=f"http://127.0.0.1:{port}", ollama_bin=FAKE_BIN, model="")
    assert make_runtime(cfg, backend="auto").kind == "system-ollama"


def test_make_runtime_auto_falls_to_sidecar_when_system_down(sidecar_env):
    dead = free_port()
    cfg = SimpleNamespace(ollama_url=f"http://127.0.0.1:{dead}", ollama_bin=FAKE_BIN, model="")
    assert make_runtime(cfg, backend="auto").kind == "sidecar-ollama"
