#!/usr/bin/env python3
"""Test double for an Ollama backend.

Two roles in one file:

1. Importable: FakeOllamaServer and run_server(port) give SystemOllama tests
   an in-process backend that answers /api/version, /api/tags, /api/chat
   (streamed NDJSON) and /api/generate (keep_alive 0 unload).

2. Runnable as a binary: `python fake_ollama_bin.py serve` behaves like
   `ollama serve` for the SidecarOllama spawn test. It reads OLLAMA_HOST
   (host:port) from the environment, just like the real binary, and serves
   the same fake API from a child process that the test owns and kills.

Self-contained on purpose: the spawned process must not depend on Steward's
src on sys.path.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Fixed, deterministic chat reply. Every token is one streamed delta.
REPLY_TOKENS = ["Hello ", "from ", "the ", "fake ", "backend."]
MODELS = ["fake-model", "qwen3:8b"]
VERSION = "0.0.0-fake"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args) -> None:  # noqa: A002 - stdlib naming
        pass

    def _send_json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/api/version":
            self._send_json(200, {"version": VERSION})
        elif path == "/api/tags":
            self._send_json(200, {"models": [{"name": m} for m in MODELS]})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        try:
            request = json.loads(raw.decode("utf-8"))
        except Exception:
            request = {}
        if path == "/api/generate":
            # keep_alive 0 unload; mirror the real response shape.
            self._send_json(200, {"done": True, "keep_alive": request.get("keep_alive"),
                                  "response": "", "model": request.get("model", "")})
            return
        if path != "/api/chat":
            self._send_json(404, {"error": "not found"})
            return
        if not request.get("stream", False):
            self._send_json(200, {"message": {"role": "assistant",
                                              "content": "".join(REPLY_TOKENS)},
                                  "done": True})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        for token in REPLY_TOKENS:
            line = json.dumps({"message": {"role": "assistant", "content": token},
                               "done": False}) + "\n"
            self.wfile.write(line.encode("utf-8"))
        self.wfile.write(json.dumps({"message": {}, "done": True}).encode("utf-8") + b"\n")


class FakeOllamaServer(ThreadingHTTPServer):
    daemon_threads = True


def run_server(port: int, bind: str = "127.0.0.1") -> FakeOllamaServer:
    """Start an in-process fake backend and return the live server object."""
    server = FakeOllamaServer((bind, port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def parse_host_port() -> tuple[str, int]:
    """Read the serve target from OLLAMA_HOST, like the real binary."""
    raw = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434")
    host, _, port = raw.rpartition(":")
    return (host or "127.0.0.1", int(port))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fake-ollama")
    parser.add_argument("cmd", nargs="?", default="serve")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)
    host, port = parse_host_port()
    if args.host:
        host = args.host
    if args.port:
        port = args.port
    server = FakeOllamaServer((host, port), _Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
