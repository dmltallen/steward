"""Runtime seam: the one interface for talking to a model backend.

The daemon and CLI never talk to Ollama directly; they go through these
classes so the backend can be swapped later (ARCHITECTURE.md keeps Ollama as
the v1 backend, vLLM behind the same seam for a future dedicated pool).

Two implementations behind one interface:

SystemOllama
    Talks to an already-running server (default http://localhost:11434).
    Owns nothing; health() fails cleanly when the server is down.

SidecarOllama
    Spawns and owns a bundled `ollama` binary: picks a free localhost port,
    waits for /api/version to answer, kills the process on shutdown, and
    refuses to start a second copy or to start where another server is
    already listening on the chosen port. The binary comes from
    STEWARD_OLLAMA_BIN or config.ollama_bin, then from well-known paths. When
    no binary is present, health() reports sidecar_available=false with the
    searched paths and degrades instead of crashing.

Interface (shared by both implementations):

    health() -> dict
    ensure_serving(model) -> dict
    list_models() -> list[str]
    chat(messages, stream=True) -> generator of {"delta": str}
    stop(model) -> dict

The streamed chat is NDJSON like the real Ollama API: each response line is
a JSON object whose message.content is one token delta, with a final
done:true line that carries no content.

Transport is stdlib urllib only; there are no third-party dependencies.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator

DEFAULT_OLLAMA_URL = "http://localhost:11434"
SIDECAR_HOST = "127.0.0.1"
SIDECAR_STARTUP_TIMEOUT_S = 60.0

# Well-known places a bundled ollama binary may live. STEWARD_OLLAMA_BIN and
# config.ollama_bin (checked first) extend or pin this list.
SIDECAR_SEARCH = [
    "/usr/local/bin/ollama",
    "/usr/bin/ollama",
    "/home/user/ollama-local/bin/ollama",
]


class BackendError(RuntimeError):
    """The model backend is unreachable, misconfigured, or refused work."""


def _json_loads_value(raw: bytes) -> dict | list:
    data = json.loads(raw.decode("utf-8"))
    return data if isinstance(data, (dict, list)) else {}


class Runtime:
    """Base interface shared by every backend implementation.

    Subclasses set `kind` and implement health(). The HTTP plumbing, chat,
    list_models, ensure_serving and stop live here on top of the shared
    urllib transport.
    """

    kind = "runtime"

    def __init__(self, url: str = DEFAULT_OLLAMA_URL, timeout: float = 8.0,
                 model: str | None = None) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.model = model

    # -- HTTP plumbing --------------------------------------------------------

    def _request(self, method: str, path: str, body: bytes | None = None,
                 timeout: float | None = None, headers: dict | None = None):
        req = urllib.request.Request(self.url + path, data=body, method=method)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        return urllib.request.urlopen(req, timeout=timeout or self.timeout)

    def _get_json(self, path: str, timeout: float | None = None) -> dict:
        with self._request("GET", path, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data if isinstance(data, dict) else {}

    def _post_json(self, path: str, payload: dict, timeout: float | None = None) -> dict:
        body = json.dumps(payload).encode("utf-8")
        with self._request("POST", path, body=body, timeout=timeout,
                           headers={"Content-Type": "application/json"}) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data if isinstance(data, dict) else {}

    def _post_stream(self, path: str, payload: dict) -> Iterator[dict]:
        body = json.dumps(payload).encode("utf-8")
        with self._request("POST", path, body=body,
                           headers={"Content-Type": "application/json"}) as r:
            for raw in r:
                line = raw.decode("utf-8").strip()
                if not line:
                    continue
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    yield parsed

    # -- shared behavior --------------------------------------------------------

    def health(self) -> dict:
        """Subclasses report backend health; the base has nothing to probe."""
        raise NotImplementedError

    def list_models(self) -> list[str]:
        data = self._get_json("/api/tags")
        return sorted(m.get("name", "") for m in data.get("models", []))

    def ensure_serving(self, model: str | None = None) -> dict:
        """Report whether a model can be served right now, without starting
        downloads. Never pulls; a missing model is reported, not fixed."""
        model = model or self.model
        info = self.health()
        if not info.get("ok"):
            return {
                "ok": False, "model": model or "", "resident": False, "serving": False,
                "backend": info.get("backend") or self.kind,
                "note": info.get("error") or "backend unreachable",
            }
        models = info.get("models") or []
        resident = bool(model) and model in models
        note = None
        if not resident and model:
            note = "model not pulled yet; it loads on first request or pull it explicitly"
        return {
            "ok": True, "model": model or "", "resident": resident, "serving": True,
            "backend": info.get("backend") or self.kind, "note": note,
        }

    def stop(self, model: str | None = None) -> dict:
        """Unload a model from backend memory (/api/generate keep_alive 0)."""
        model = model or self.model
        try:
            self._post_json("/api/generate", {"model": model or "", "keep_alive": 0})
            return {"ok": True, "model": model or "", "unloaded": True, "error": None}
        except Exception as exc:
            return {"ok": False, "model": model or "", "unloaded": False, "error": str(exc)}

    def chat(self, messages: list, stream: bool = True,
             model: str | None = None) -> Iterator[dict]:
        """Chat completion. Yields {"delta": str} dicts; with stream=False a
        single dict carries the whole reply."""
        model = model or self.model
        if not model:
            raise BackendError("no model configured for chat; pass model= or set it")
        payload = {
            "model": model,
            "messages": messages,
            "stream": bool(stream),
            "options": {"temperature": 0.7},
        }
        if not stream:
            data = self._post_json("/api/chat", payload)
            message = data.get("message") or {}
            yield {"delta": message.get("content", "")}
            return
        for line in self._post_stream("/api/chat", payload):
            if line.get("done") is True:
                continue
            content = (line.get("message") or {}).get("content", "")
            if content:
                yield {"delta": content}


class SystemOllama(Runtime):
    """A backend that already runs (e.g. the machine's Ollama service)."""

    kind = "system-ollama"

    def __init__(self, url: str = DEFAULT_OLLAMA_URL, timeout: float = 8.0,
                 model: str | None = None) -> None:
        super().__init__(url=url, timeout=timeout, model=model)

    def health(self) -> dict:
        base = {
            "ok": False, "backend": self.kind, "url": self.url,
            "version": None, "models": [], "error": None,
        }
        try:
            version = self._get_json("/api/version", timeout=2.0)
            base["version"] = version.get("version")
            base["models"] = self.list_models()
            base["ok"] = True
        except Exception as exc:
            base["error"] = str(exc)
        return base


class SidecarOllama(Runtime):
    """A bundled ollama binary Steward spawns, probes and owns.

    start() picks a free localhost port, verifies nothing else listens there,
    spawns `ollama serve` with OLLAMA_HOST pinned to that port, and waits for
    /api/version. shutdown() terminates the child. Starting while a copy is
    already running, or on a port that is already occupied, raises.
    """

    kind = "sidecar-ollama"

    def __init__(self, binary: str = "", *, host: str = SIDECAR_HOST,
                 port: int | None = None,
                 startup_timeout: float = SIDECAR_STARTUP_TIMEOUT_S,
                 timeout: float = 8.0, model: str | None = None) -> None:
        Runtime.__init__(self, url="", timeout=timeout, model=model)
        self._requested_binary = binary
        self.host = host
        self._forced_port = port
        self.port: int | None = port
        self.startup_timeout = startup_timeout
        self._proc: subprocess.Popen | None = None

    # -- binary discovery ------------------------------------------------------

    def _candidates(self) -> list[str]:
        out: list[str] = []
        env = os.environ.get("STEWARD_OLLAMA_BIN") or ""
        for candidate in (env, self._requested_binary, shutil.which("ollama") or ""):
            if candidate and candidate not in out:
                out.append(candidate)
        for candidate in SIDECAR_SEARCH:
            if candidate not in out:
                out.append(candidate)
        return out

    @property
    def binary_path(self) -> str | None:
        for candidate in self._candidates():
            path = Path(candidate)
            if path.is_file() and os.access(candidate, os.X_OK):
                return candidate
        return None

    # -- lifecycle --------------------------------------------------------------

    def start(self) -> str:
        """Spawn the sidecar and wait for it to answer /api/version."""
        if self._proc is not None and self._proc.poll() is None:
            raise RuntimeError(
                "refusing to double-start: sidecar ollama already running (pid %d)"
                % self._proc.pid
            )
        binary = self.binary_path
        if not binary:
            raise RuntimeError(
                "no ollama binary found; searched: %s" % ", ".join(self._candidates())
            )
        port = self._forced_port if self._forced_port else self._pick_free_port()
        if self._port_listening(self.host, port):
            raise RuntimeError(
                "refusing to start sidecar: something already listens on %s:%d"
                % (self.host, port)
            )
        self.port = port
        self.url = "http://%s:%d" % (self.host, port)
        env = dict(os.environ)
        env["OLLAMA_HOST"] = "%s:%d" % (self.host, port)
        self._proc = subprocess.Popen(
            [binary, "serve"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            self._wait_for_version()
        except Exception:
            self.shutdown()
            raise
        return self.url

    def shutdown(self) -> None:
        """Terminate the owned process if it is running. Idempotent."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass

    # context-manager convenience: `with SidecarOllama(...) as rt:` starts and
    # always shuts the process down, even on exception.
    def __enter__(self) -> "SidecarOllama":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.shutdown()

    def _pick_free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((self.host, 0))
            return int(sock.getsockname()[1])

    @staticmethod
    def _port_listening(host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            return False

    def _wait_for_version(self) -> dict:
        deadline = time.monotonic() + self.startup_timeout
        last_error = None
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise BackendError(
                    "sidecar process exited early (code %s)"
                    % self._proc.returncode
                )
            try:
                return self._get_json("/api/version", timeout=0.7)
            except Exception as exc:  # noqa: BLE001 - any probe failure is retried
                last_error = exc
                time.sleep(0.1)
        raise BackendError(
            "sidecar did not answer /api/version within %.0fs (%s)"
            % (self.startup_timeout, last_error)
        )

    # -- interface ---------------------------------------------------------------

    def health(self) -> dict:
        base = {
            "ok": False, "backend": self.kind, "url": self.url or None,
            "version": None, "models": [],
            "sidecar_available": False, "binary_path": None,
            "searched_paths": self._candidates(), "error": None,
        }
        binary = self.binary_path
        base["binary_path"] = binary
        base["sidecar_available"] = binary is not None
        if not binary:
            base["error"] = "no ollama binary found; searched: %s" % ", ".join(
                self._candidates()
            )
            return base
        if not self.url:
            base["error"] = "sidecar binary found but not started"
            return base
        try:
            version = self._get_json("/api/version", timeout=2.0)
            base["version"] = version.get("version")
            base["models"] = self.list_models()
            base["ok"] = True
            base["error"] = None
        except Exception as exc:
            base["error"] = str(exc)
        return base

    def ensure_serving(self, model: str | None = None) -> dict:
        if self._proc is None or self._proc.poll() is not None:
            try:
                self.start()
            except Exception as exc:
                return {
                    "ok": False, "model": model or "", "resident": False,
                    "serving": False, "backend": self.kind, "note": str(exc),
                }
        return super().ensure_serving(model)


def make_runtime(cfg, backend: str = "auto") -> Runtime:
    """Pick a Runtime for the daemon and CLI.

    backend:
      "system"  -> SystemOllama(cfg.ollama_url)
      "sidecar" -> SidecarOllama(cfg.ollama_bin)
      "auto"    -> SystemOllama when the system server answers, else
                   SidecarOllama when a binary is available, else
                   SystemOllama, which then reports itself degraded.
    """
    if backend == "system":
        return SystemOllama(url=cfg.ollama_url or DEFAULT_OLLAMA_URL, model=cfg.model)
    if backend == "sidecar":
        return SidecarOllama(binary=cfg.ollama_bin, model=cfg.model)
    system = SystemOllama(url=cfg.ollama_url or DEFAULT_OLLAMA_URL, model=cfg.model)
    try:
        system._get_json("/api/version", timeout=1.0)
        return system
    except (urllib.error.URLError, OSError, TimeoutError, ValueError, json.JSONDecodeError):
        pass
    sidecar = SidecarOllama(binary=cfg.ollama_bin, model=cfg.model)
    if sidecar.binary_path:
        return sidecar
    return system
