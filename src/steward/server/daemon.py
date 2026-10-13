"""Steward daemon: a small local HTTP API that serves the selected model bundle.

Bound to 127.0.0.1 on a fixed port (default 3877). Talks to the local Ollama
backend (the T1 benchmark winner) at http://localhost:11434 and fronts it with
three client-friendly routes plus a first-look demo capability that reads a
real local data source (a notes/calendar folder) and summarizes it.

Routes:
    GET  /health            daemon + backend + served model status
    GET  /bundle            live selector output for this host + served model
    POST /chat              real model-generated reply (Ollama backend)
    GET  /demo/source       list the data-source files the demo reads
    POST /demo/summarize    read the data source, summarize through the model

The data source is a plain folder of markdown files (calendar, notes, config)
pointed at by STEWARD_DEMO_SOURCE (default: demo/source in the repo). Set it
to any local folder, for example the Obsidian vault, for real use.

Run (from the repo root):
    STEWARD_MODEL=qwen3:8b PYTHONPATH=src python3 -m steward.server.daemon
See README.md in this directory and docs/DEMO.md for the full steps.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3877
DEFAULT_MODEL = "qwen3:8b"  # resident 8B assistant, the v0.1 pick, benchmarked T1
DEFAULT_OLLAMA = "http://localhost:11434"
DEFAULT_SOURCE = str(Path(__file__).resolve().parents[3] / "demo" / "source")

VERSION = "0.1.0"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def detect_bundle() -> dict:
    """Live selector output for this host. Imported lazily so the daemon also
    runs on machines without NVML tooling; detect() falls back safely."""
    from steward.detect import detect
    from steward.selector import select_bundle

    p = detect()
    b = select_bundle(p)
    return {
        "class": b.class_name,
        "serving_mem_gb": b.serving_mem_gb,
        "capability": b.capability.id,
        "assistant": b.assistant.id,
        "vision": b.vision,
        "capability_est_tok_s": list(b.capability.tok_range),
        "gpu": p.gpu_name,
    }


def ollama_tags(base: str) -> list:
    req = urllib.request.Request(base + "/api/tags", method="GET")
    with urllib.request.urlopen(req, timeout=5) as r:
        data = json.loads(r.read().decode("utf-8"))
    return sorted(m["name"] for m in data.get("models", []))


def ollama_chat(base: str, model: str, messages: list, timeout: int = 240) -> str:
    """Blocking single-turn completion against the local backend."""
    body = json.dumps({
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.7},
    }).encode("utf-8")
    req = urllib.request.Request(base + "/api/chat", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data["message"]["content"].strip()


def read_source(source_dir: str) -> list:
    """Return [{"name": ..., "text": ...}] for every .md/.txt file in the
    data-source folder, newest first. Skips dotfiles and nested dirs."""
    out = []
    path = Path(source_dir)
    if not path.exists():
        return out
    for f in sorted(path.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if f.is_file() and f.suffix.lower() in (".md", ".txt") and not f.name.startswith("."):
            out.append({"name": f.name, "text": f.read_text(encoding="utf-8", errors="replace")})
    return out


def summarize_source(source_dir: str, model: str, base: str, timeout: int = 240) -> dict:
    files = read_source(source_dir)
    chunks = [f"## {f['name']}\n{f['text']}" for f in files]
    joined = "\n\n".join(chunks)
    prompt = (
        "These are my local Steward project notes (calendar, launch notes, and "
        "config snapshot). Briefly summarize what is coming up and list the top "
        "three next actions.\n\n"
        + joined
    )
    reply = ollama_chat(base, model, [{"role": "user", "content": prompt}], timeout=timeout)
    return {"source": source_dir, "files": [f["name"] for f in files], "reply": reply}


class Handler(BaseHTTPRequestHandler):
    server_version = "StewardDaemon/" + VERSION

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def log_message(self, format, *args):  # quieter access logs
        sys.stderr.write(
            "[%s] %s\n" % (time.strftime("%H:%M:%S"), format % args)
        )

    def do_GET(self) -> None:  # noqa: N802 (stdlib names)
        path = self.path.split("?")[0]
        if path == "/health":
            served = self.server.served_model  # type: ignore[attr-defined]
            try:
                tags = ollama_tags(self.server.ollama_url)  # type: ignore[attr-defined]
                backend = {"ok": True, "models": tags}
            except Exception as exc:
                backend = {"ok": False, "error": str(exc)}
            self._json(200, {
                "status": "ok" if backend["ok"] else "degraded",
                "backend": "ollama",
                "ollama_url": self.server.ollama_url,  # type: ignore[attr-defined]
                "model": served,
                "model_resident": served in backend.get("models", []),
                "version": VERSION,
            })
        elif path == "/bundle":
            self._json(200, {
                "bundle": detect_bundle(),
                "served_model": self.server.served_model,  # type: ignore[attr-defined]
                "note": "capability tier is resident only when serving holds; the resident assistant model serves chat",
            })
        elif path == "/demo/source":
            files = read_source(self.server.source_dir)  # type: ignore[attr-defined]
            self._json(200, {"source": self.server.source_dir,  # type: ignore[attr-defined]
                             "files": [f["name"] for f in files]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/chat":
            data = self._read_json()
            text = str(data.get("text", "")).strip()
            history = data.get("messages") or []
            if not text:
                self._json(400, {"error": "empty text"})
                return
            messages = [{"role": m.get("role", "user"),
                         "content": str(m.get("text", ""))}
                        for m in history if m.get("text")]
            # Ensure the fresh user turn is present even if the app sent history only.
            if not messages or messages[-1]["content"] != text:
                messages.append({"role": "user", "content": text})
            try:
                reply = ollama_chat(self.server.ollama_url,  # type: ignore[attr-defined]
                                    self.server.served_model,  # type: ignore[attr-defined]
                                    messages)
                self._json(200, {"id": f"msg-{int(time.time() * 1000)}",
                                 "role": "assistant", "text": reply,
                                 "model": self.server.served_model})  # type: ignore[attr-defined]
            except urllib.error.HTTPError as exc:
                self._json(502, {"error": f"backend error {exc.code}"})
            except Exception as exc:
                self._json(502, {"error": str(exc)})
        elif path == "/demo/summarize":
            try:
                result = summarize_source(
                    self.server.source_dir,  # type: ignore[attr-defined]
                    self.server.served_model,  # type: ignore[attr-defined]
                    self.server.ollama_url,    # type: ignore[attr-defined]
                )
                self._json(200, result)
            except Exception as exc:
                self._json(502, {"error": str(exc)})
        else:
            self._json(404, {"error": "not found"})


class StewardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, handler, *, model, ollama_url, source_dir):
        super().__init__(addr, handler)
        self.served_model = model
        self.ollama_url = ollama_url
        self.source_dir = source_dir
        self.bundle = {}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="steward serve", description=__doc__)
    parser.add_argument("--host", default=_env("STEWARD_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(_env("STEWARD_PORT", str(DEFAULT_PORT))))
    parser.add_argument("--model", default=_env("STEWARD_MODEL", DEFAULT_MODEL))
    parser.add_argument("--ollama-url", default=_env("STEWARD_OLLAMA_URL", DEFAULT_OLLAMA))
    parser.add_argument("--source", default=_env("STEWARD_DEMO_SOURCE", DEFAULT_SOURCE))
    args = parser.parse_args(argv)

    server = StewardServer((args.host, args.port), Handler,
                           model=args.model, ollama_url=args.ollama_url,
                           source_dir=args.source)

    try:
        tags = ollama_tags(args.ollama_url)
        resident = args.model in tags
        print(f"Steward daemon {VERSION} on {args.host}:{args.port} "
              f"backend=ollama model={args.model} resident={resident}")
    except Exception as exc:
        print(f"Steward daemon {VERSION} on {args.host}:{args.port} "
              f"backend=ollama model={args.model} WARNING backend unreachable: {exc}")
    try:
        info = detect_bundle()
        print(f"  selector bundle on this host: class={info['class']} "
              f"capability={info['capability']} assistant={info['assistant']} "
              f"vision={info['vision']}")
    except Exception as exc:
        print(f"  selector bundle unavailable: {exc}")
    print(f"  demo data source: {args.source}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
