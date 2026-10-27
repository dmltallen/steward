"""Steward daemon: a hardened local HTTP API that serves the model bundle.

Built for LAN access from a phone: pairing first, everything gated, model
access only through the steward.runtime seam (never urllib to Ollama directly).

Routes:

Open (no token):
    GET  /health            daemon + backend status, served model, pair count
    GET  /v1/bundle         live selector output for this host (canonical)
    GET  /bundle            shim for /v1/bundle (one-release compatibility)
    GET  /pair/code         current 6-digit pairing code, loopback only
    POST /v1/pair           {code, device} -> signed session token (rate-limited)

Bearer-required (Authorization: Bearer <token>):
    POST /v1/chat           SSE stream of model deltas + final data: [DONE]
    POST /chat              JSON shim for /v1/chat (old app contract)
    GET  /v1/source         list data-source files (canonical)
    GET  /demo/source       shim for /v1/source
    POST /v1/summarize      read data source, summarize through the model
    POST /demo/summarize    shim for /v1/summarize
    GET  /v1/conversations  list conversations
    POST /v1/conversations  create a conversation {title?}
    GET  /v1/conversations/{id}       conversation + messages
    DELETE /v1/conversations/{id}     delete conversation and its messages
    POST /v1/conversations/{id}/messages  append {role?, content}
    GET  /v1/briefs           stored briefs, newest first (S8 memory)
    GET  /v1/briefs/today     most recent brief for today, if any
    GET  /v1/approvals        pending + decided tool approvals (S9)
    POST /v1/approvals/{id}   {decision|approve} decides a pending approval
    GET  /doctor/bundle       build a redacted support bundle (S12)
    GET  / , /index.html , /app.js , /style.css + /v1/webapp/*  (S5 webapp)

Conversations and messages persist in SQLite at <data>/db/steward.db (WAL).
Pass conversation_id to /v1/chat or /chat to record both turns in the store.

Security model: a 6-digit pairing code is printed at startup and served only
to loopback (GET /pair/code). Pairing trades the code for a stateless HMAC
session token (30 day TTL) signed with a key persisted under
<data>/db/pairing.key, so tokens survive restarts. Failed pair attempts are
rate-limited to 5 per 5 minutes per IP. Details in auth.py and README.md.

Discovery: advertises steward-<port>._steward._tcp.local via zeroconf with a
TXT record of version, port, model, paired_count (see mdns.py). The pairing
URL / QR payload is printed on startup.

Run (from the repo root):
    STEWARD_MODEL=qwen3:8b PYTHONPATH=src python3 -m steward.server.daemon
The default bind is 0.0.0.0 so a phone on the LAN can reach it; set
STEWARD_HOST=127.0.0.1 to keep it loopback-only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from steward.server import store
from steward.server.auth import (
    PairRateLimiter,
    bearer_token,
    check_token,
    code_matches,
    device_label,
    generate_pair_code,
    issue_token,
    load_or_create_secret,
)
from steward.server.mdns import Advertiser, instance_name, lan_ip
from steward.memory.store import MemoryStore
from steward.tools.approvals import ApprovalManager, approvals_router
from steward.webapp import serve as webapp_serve

DEFAULT_HOST = "0.0.0.0"  # LAN-visible by default; auth gates every route
DEFAULT_PORT = 3877
DEFAULT_MODEL = "qwen3:8b"  # resident 8B assistant, the v0.1 pick, benchmarked T1
DEFAULT_SOURCE = str(Path(__file__).resolve().parents[3] / "demo" / "source")
MAX_BODY = 1_000_000

VERSION = "0.2.0"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_loopback_ip(ip: str) -> bool:
    """True when a peer address is the local loopback (pair code access)."""
    return ip in ("127.0.0.1", "::1", "localhost")


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


def summarize_source(source_dir: str, runtime, model: str) -> dict:
    """Read the data source and summarize it through the runtime seam."""
    files = read_source(source_dir)
    chunks = [f"## {f['name']}\n{f['text']}" for f in files]
    joined = "\n\n".join(chunks)
    prompt = (
        "These are my local Steward project notes (calendar, launch notes, and "
        "config snapshot). Briefly summarize what is coming up and list the top "
        "three next actions.\n\n"
        + joined
    )
    reply = "".join(d["delta"] for d in runtime.chat(
        [{"role": "user", "content": prompt}], stream=False, model=model))
    return {"source": source_dir, "files": [f["name"] for f in files], "reply": reply}


def build_messages(data: dict) -> list[dict]:
    """Assemble the chat message list from the app payload.

    Accepts the T2 app shape {messages: [{role, text}], text: current turn}
    and the v1 shape {messages: [{role, content}]}. The fresh turn always ends
    the list, mirroring the old daemon behavior.
    """
    text = str(data.get("text", "")).strip()
    history = data.get("messages") or []
    messages = []
    for m in history:
        if not isinstance(m, dict):
            continue
        content = str(m.get("content") or m.get("text") or "").strip()
        if not content:
            continue
        messages.append({"role": str(m.get("role", "user")), "content": content})
    if text and (not messages or messages[-1]["content"] != text):
        messages.append({"role": "user", "content": text})
    return messages


class DaemonContext:
    """Everything a request handler needs, shared across handler threads."""

    def __init__(self, *, runtime, model: str, source_dir: str,
                 db_path: Path, secret: bytes, version: str) -> None:
        self.runtime = runtime
        self.model = model
        self.source_dir = source_dir
        self.db_path = db_path
        self.secret = secret
        self.version = version
        self.pair_code = generate_pair_code()
        self.pairs: set[str] = set()  # distinct device labels that ever paired
        self.limiter = PairRateLimiter()
        self.port = 0  # filled in by StewardServer once the socket is bound
        self.host_ip = "127.0.0.1"  # filled in by StewardServer
        # S8 memory + S9 approvals: one store per daemon, at <data>/db/, so the
        # daemon serves /v1/briefs and /v1/approvals from the same data root
        # as conversations and pairing.
        data_root = Path(db_path).resolve().parent.parent
        self.memory_store = MemoryStore(data_root / "db" / "memory.db")
        self.approval_manager = ApprovalManager(data_root / "db" / "approvals.db")
        self.approvals_handle = approvals_router(self.approval_manager)


class Handler(BaseHTTPRequestHandler):
    server_version = "StewardDaemon/" + VERSION
    # HTTP/1.0 closes the connection after each response; the client of an SSE
    # stream sees the end at EOF, right after the final data: [DONE] event.
    protocol_version = "HTTP/1.0"

    @property
    def ctx(self) -> DaemonContext:
        return self.server.ctx  # type: ignore[attr-defined]

    # -- plumbing ---------------------------------------------------------------

    def log_message(self, format, *args):  # quieter access logs
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), format % args))

    def _client_ip(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return self.client_address[0] if self.client_address else "unknown"

    def _is_loopback(self) -> bool:
        ip = self.client_address[0] if self.client_address else ""
        return is_loopback_ip(ip)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        length = min(length, MAX_BODY)
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _unauthorized(self) -> None:
        body = json.dumps({"error": "unauthorized"}).encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("WWW-Authenticate", 'Bearer realm="steward"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _require_auth(self) -> str | None:
        """Send 401 and return None when the bearer token is missing or bad."""
        token = bearer_token(self.headers.get("Authorization"))
        if not check_token(token, self.ctx.secret):
            self._unauthorized()
            return None
        return device_label(token) if token else "unknown"

    # -- dispatch ---------------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        path = (self.path or "").split("?", 1)[0]
        parts = [p for p in path.split("/") if p]
        # S5 mount: the webapp serves its static client and its own API here.
        if webapp_serve.dispatch(self, method, parts, path):
            return
        try:
            if method == "GET":
                self._route_get(parts, path)
            elif method == "DELETE":
                self._route_delete(parts)
            else:
                self._route_post(parts, path)
        except Exception as exc:  # noqa: BLE001 - a broken route is a 500
            self._json(500, {"error": str(exc)})

    def _route_get(self, parts: list[str], path: str) -> None:
        if path == "/health":
            self._handle_health()
        elif path in ("/v1/bundle", "/bundle"):
            self._handle_bundle()
        elif path == "/pair/code":
            self._handle_pair_code()
        elif path in ("/v1/source", "/demo/source"):
            if self._require_auth() is None:
                return
            self._handle_source()
        elif parts[:2] == ["v1", "conversations"]:
            if self._require_auth() is None:
                return
            if len(parts) == 2:
                self._handle_conversation_list()
            elif len(parts) == 3:
                self._handle_conversation_get(_as_int(parts[2]))
            else:
                self._json(404, {"error": "not found"})
        elif parts[:2] == ["v1", "approvals"]:
            # S9 approvals contract: GET /v1/approvals lists pending + history.
            if self._require_auth() is None:
                return
            status, payload = self.ctx.approvals_handle("GET", parts)
            self._json(status, payload)
        elif path == "/v1/briefs":
            if self._require_auth() is None:
                return
            self._handle_briefs_list()
        elif path == "/v1/briefs/today":
            if self._require_auth() is None:
                return
            self._handle_briefs_today()
        elif path == "/doctor/bundle":
            if self._require_auth() is None:
                return
            self._handle_doctor_bundle()
        else:
            self._json(404, {"error": "not found"})

    def _route_post(self, parts: list[str], path: str) -> None:
        if path == "/v1/pair":
            self._handle_pair()
        elif path == "/v1/chat":
            if self._require_auth() is None:
                return
            self._handle_v1_chat()
        elif path == "/chat":
            if self._require_auth() is None:
                return
            self._handle_chat_shim()
        elif path in ("/v1/summarize", "/demo/summarize"):
            if self._require_auth() is None:
                return
            self._handle_summarize()
        elif parts[:2] == ["v1", "conversations"]:
            if self._require_auth() is None:
                return
            if len(parts) == 2:
                self._handle_conversation_create()
            elif len(parts) == 4 and parts[3] == "messages":
                self._handle_conversation_message(_as_int(parts[2]))
            else:
                self._json(404, {"error": "not found"})
        elif len(parts) == 3 and parts[:2] == ["v1", "approvals"]:
            # S9 approvals: POST /v1/approvals/{id} {decision|approve} decides.
            if self._require_auth() is None:
                return
            status, payload = self.ctx.approvals_handle(
                "POST", parts, body=self._read_json())
            self._json(status, payload)
        else:
            self._json(404, {"error": "not found"})

    def _route_delete(self, parts: list[str]) -> None:
        if parts[:2] == ["v1", "conversations"] and len(parts) == 3:
            if self._require_auth() is None:
                return
            self._handle_conversation_delete(_as_int(parts[2]))
        else:
            self._json(404, {"error": "not found"})

    def do_GET(self) -> None:  # noqa: N802 (stdlib names)
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    # -- open routes -------------------------------------------------------------

    def _handle_health(self) -> None:
        try:
            h = self.ctx.runtime.health()
        except Exception as exc:  # noqa: BLE001 - healthy failure mode
            h = {"ok": False, "error": str(exc)}
        ok = bool(h.get("ok"))
        self._json(200, {
            "status": "ok" if ok else "degraded",
            "backend": getattr(self.ctx.runtime, "kind", "unknown"),
            "ollama_url": getattr(self.ctx.runtime, "url", None),
            "model": self.ctx.model,
            "model_resident": self.ctx.model in (h.get("models") or []),
            "version": self.ctx.version,
            "paired": len(self.ctx.pairs),
        })

    def _handle_bundle(self) -> None:
        try:
            bundle = detect_bundle()
        except Exception as exc:  # noqa: BLE001 - bundle is a report, not a gate
            bundle = {"error": str(exc)}
        self._json(200, {
            "bundle": bundle,
            "served_model": self.ctx.model,
            "note": "capability tier is resident only when serving holds; "
                    "the resident assistant model serves chat",
        })

    def _handle_pair_code(self) -> None:
        if not self._is_loopback():
            self._json(403, {"error": "pair code is only served to loopback clients"})
            return
        self._json(200, {
            "code": self.ctx.pair_code,
            "note": "rotates on the next daemon restart; valid for the life of this run",
        })

    def _handle_pair(self) -> None:
        ip = self._client_ip()
        blocked = self.ctx.limiter.blocked(ip)
        if blocked > 0:
            self._json(429, {"error": "too many failed pairing attempts",
                             "retry_after_seconds": blocked})
            return
        data = self._read_json()
        code = str(data.get("code", ""))
        device = str(data.get("device", "")).strip()[:64] or "device"
        if not code_matches(code, self.ctx.pair_code):
            self.ctx.limiter.note_failure(ip)
            self._json(401, {"error": "invalid pairing code"})
            return
        self.ctx.limiter.clear(ip)
        token, exp = issue_token(self.ctx.secret, device)
        self.ctx.pairs.add(device)
        self._json(200, {
            "token": token,
            "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(exp)),
            "device": device,
            "paired": True,
        })
        self.server.on_paired()  # type: ignore[attr-defined]

    # -- source / summarize ------------------------------------------------------

    def _handle_source(self) -> None:
        files = read_source(self.ctx.source_dir)
        self._json(200, {"source": self.ctx.source_dir,
                         "files": [f["name"] for f in files]})

    def _handle_summarize(self) -> None:
        try:
            result = summarize_source(self.ctx.source_dir, self.ctx.runtime,
                                      self.ctx.model)
            self._json(200, result)
        except Exception as exc:  # noqa: BLE001 - backend failure is a 502
            self._json(502, {"error": str(exc)})

    # -- chat --------------------------------------------------------------------

    def _sse(self, obj: dict) -> None:
        self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode("utf-8"))
        self.wfile.flush()

    def _sse_done(self) -> None:
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _handle_v1_chat(self) -> None:
        data = self._read_json()
        messages = build_messages(data)
        if not messages or messages[-1]["role"] != "user":
            self._json(400, {"error": "empty text"})
            return
        conversation_id = _as_int(data.get("conversation_id"))
        if conversation_id is not None:
            if store.get_conversation(self.ctx.db_path, conversation_id) is None:
                self._json(404, {"error": "conversation not found"})
                return
            store.add_message(self.ctx.db_path, conversation_id, "user",
                              messages[-1]["content"], self.ctx.model)
        # SSE headers first, so the client starts receiving deltas immediately.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        full: list[str] = []
        try:
            for item in self.ctx.runtime.chat(messages, stream=True, model=self.ctx.model):
                delta = item.get("delta", "")
                if delta:
                    full.append(delta)
                    self._sse({"delta": delta})
        except Exception as exc:  # noqa: BLE001 - surface mid-stream failure
            self._sse({"error": str(exc)})
        self._sse_done()
        if conversation_id is not None:
            store.add_message(self.ctx.db_path, conversation_id, "assistant",
                              "".join(full), self.ctx.model)

    def _handle_chat_shim(self) -> None:
        """Old /chat JSON contract, same body and reply shape as v0.1, now
        Bearer-gated. Streams the model, returns one JSON object."""
        data = self._read_json()
        messages = build_messages(data)
        if not messages or messages[-1]["role"] != "user":
            self._json(400, {"error": "empty text"})
            return
        conversation_id = _as_int(data.get("conversation_id"))
        if conversation_id is not None:
            if store.get_conversation(self.ctx.db_path, conversation_id) is None:
                self._json(404, {"error": "conversation not found"})
                return
            store.add_message(self.ctx.db_path, conversation_id, "user",
                              messages[-1]["content"], self.ctx.model)
        try:
            reply = "".join(d["delta"] for d in self.ctx.runtime.chat(
                messages, stream=False, model=self.ctx.model))
        except Exception as exc:  # noqa: BLE001 - backend failure is a 502
            self._json(502, {"error": str(exc)})
            return
        if conversation_id is not None:
            store.add_message(self.ctx.db_path, conversation_id, "assistant",
                              reply, self.ctx.model)
        self._json(200, {"id": f"msg-{int(time.time() * 1000)}",
                         "role": "assistant", "text": reply, "model": self.ctx.model})

    # -- conversations -------------------------------------------------------------

    def _handle_conversation_list(self) -> None:
        self._json(200, {"conversations": store.list_conversations(self.ctx.db_path)})

    def _handle_conversation_create(self) -> None:
        data = self._read_json()
        conv = store.create_conversation(self.ctx.db_path, str(data.get("title", "")))
        self._json(201, conv)

    def _handle_conversation_get(self, cid: int | None) -> None:
        if cid is None:
            self._json(404, {"error": "conversation not found"})
            return
        conv = store.get_conversation(self.ctx.db_path, cid)
        if conv is None:
            self._json(404, {"error": "conversation not found"})
            return
        self._json(200, conv)

    def _handle_conversation_delete(self, cid: int | None) -> None:
        if cid is None or not store.delete_conversation(self.ctx.db_path, cid):
            self._json(404, {"error": "conversation not found"})
            return
        self._json(200, {"ok": True, "deleted": cid})

    def _handle_conversation_message(self, cid: int | None) -> None:
        if cid is None:
            self._json(404, {"error": "conversation not found"})
            return
        data = self._read_json()
        content = str(data.get("content", "")).strip()
        if not content:
            self._json(400, {"error": "empty content"})
            return
        role = str(data.get("role", "user")).strip() or "user"
        msg = store.add_message(self.ctx.db_path, cid, role, content, self.ctx.model)
        if msg is None:
            self._json(404, {"error": "conversation not found"})
            return
        self._json(201, msg)

    # -- memory briefs (S8) ------------------------------------------------------

    def _handle_briefs_list(self) -> None:
        """GET /v1/briefs: stored briefs, newest first (webapp history pane)."""
        briefs = self.ctx.memory_store.list_briefs(limit=50)
        self._json(200, {"briefs": briefs})

    def _handle_briefs_today(self) -> None:
        """GET /v1/briefs/today: the most recent brief for today, if any."""
        from datetime import date
        row = self.ctx.memory_store.brief_for_date(date.today().isoformat())
        self._json(200, {
            "today": row["text"] if row else "",
            "brief": row if row else None,
        })

    # -- diagnostics bundle (S12) --------------------------------------------------

    def _handle_doctor_bundle(self) -> None:
        """GET /doctor/bundle: build a redacted support bundle and report it.

        The bundle is written into <data>/support/steward-support-<ts>.zip and
        the JSON reply carries the member list and byte count (no secrets in
        the reply). Requires a bearer token like every other data route.
        """
        from steward.diagnostics import build_bundle

        data_root = self.ctx.db_path.resolve().parent.parent
        support_dir = data_root / "support"
        support_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out_path = support_dir / f"steward-support-{stamp}.zip"
        try:
            path = build_bundle(out_path, root=data_root, runtime=self.ctx.runtime)
        except Exception as exc:  # noqa: BLE001 - a failed bundle is a 500
            self._json(500, {"error": str(exc)})
            return
        import zipfile
        with zipfile.ZipFile(path) as zf:
            members = sorted(zf.namelist())
        self._json(200, {"ok": True, "bundle": str(path), "members": members,
                         "bytes": path.stat().st_size})


class StewardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, ctx: DaemonContext, *, advertise: bool = True,
                 instance_name_override: str | None = None,
                 host_ip: str | None = None) -> None:
        super().__init__(addr, Handler)
        self.ctx = ctx
        ctx.port = int(self.server_address[1])
        ctx.host_ip = host_ip or lan_ip()
        self.advertiser: Advertiser | None = None
        if advertise:
            self.advertiser = Advertiser(
                port=ctx.port, model=ctx.model, version=ctx.version,
                paired_count=lambda: len(ctx.pairs), host_ip=ctx.host_ip,
                service_name=instance_name_override or instance_name(ctx.port),
            )
            self.advertiser.start()

    def on_paired(self) -> None:
        """Refresh the mDNS TXT record when a device pairs."""
        if self.advertiser is not None:
            self.advertiser.update()

    def shutdown_server(self) -> None:
        """Stop the serve loop, release mDNS and the listen socket. Idempotent."""
        if self.advertiser is not None:
            self.advertiser.stop()
        try:
            self.shutdown()  # blocks until serve_forever exits
        except Exception:  # noqa: BLE001 - already stopped is fine
            pass
        self.server_close()


def create_server(addr, *, runtime, model: str = DEFAULT_MODEL,
                  source_dir: str | None = None, db_path: Path | None = None,
                  secret_file: Path | None = None, secret: bytes | None = None,
                  advertise: bool = True,
                  instance_name_override: str | None = None,
                  host_ip: str | None = None,
                  version: str = VERSION) -> StewardServer:
    """Build a fully-wired StewardServer.

    The knobs a caller (tests, CLI, mobile dev) commonly needs are runtime,
    db_path and secret_file; everything else has a sane default. db_path
    defaults to <data>/db/steward.db under STEWARD_HOME (or ~/.steward).
    """
    from steward.paths import db_dir

    db_path = Path(db_path) if db_path else db_dir() / "steward.db"
    store.init_schema(db_path)
    key = secret if secret else load_or_create_secret(secret_file or (db_path.parent / "pairing.key"))
    ctx = DaemonContext(runtime=runtime, model=model,
                        source_dir=source_dir or DEFAULT_SOURCE,
                        db_path=db_path, secret=key, version=version)
    return StewardServer(addr, ctx, advertise=advertise,
                         instance_name_override=instance_name_override,
                         host_ip=host_ip)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="steward serve", description=__doc__)
    parser.add_argument("--host", default=_env("STEWARD_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(_env("STEWARD_PORT", str(DEFAULT_PORT))))
    parser.add_argument("--model", default=_env("STEWARD_MODEL", DEFAULT_MODEL))
    parser.add_argument("--ollama-url", default=_env("STEWARD_OLLAMA_URL", ""))
    parser.add_argument("--source", default=_env("STEWARD_DEMO_SOURCE", DEFAULT_SOURCE))
    parser.add_argument("--no-mdns", action="store_true", help="skip mDNS advertisement")
    args = parser.parse_args(argv)

    from steward.config import Config
    from steward.runtime import make_runtime

    cfg = Config.load()
    if args.ollama_url:
        cfg.ollama_url = args.ollama_url
    if args.model:
        cfg.model = args.model
    runtime = make_runtime(cfg)

    try:
        serving = runtime.ensure_serving(args.model)
        print(f"  backend={runtime.kind} model={args.model} "
              f"resident={str(serving.get('resident', False)).lower()}")
    except Exception as exc:  # noqa: BLE001 - daemon still boots, reports degraded
        print(f"  backend={runtime.kind} model={args.model} "
              f"WARNING ensure_serving failed: {exc}")

    server = create_server((args.host, args.port), runtime=runtime,
                           model=args.model, source_dir=args.source,
                           advertise=not args.no_mdns)

    # S8 scheduler: start the job thread (morning brief, user jobs). It reads
    # <data>/jobs.json (seeded with the default when absent) and never blocks
    # daemon boot; the daemon owns its lifecycle and history.
    scheduler = None
    try:
        from steward.scheduler import Scheduler
        scheduler = Scheduler(cfg=cfg)
        scheduler.start()
        count = len(scheduler.store.list_jobs())
        print(f"  scheduler: {count} job(s) loaded (morning brief on schedule)")
    except Exception as exc:  # noqa: BLE001 - a broken scheduler never blocks boot
        print(f"  WARNING scheduler failed to start: {exc}")

    ctx = server.ctx
    ip = ctx.host_ip
    port = ctx.port
    print(f"Steward daemon {VERSION} on {args.host}:{port}")
    print(f"  pairing code: {ctx.pair_code}")
    print("  pair from a phone on the LAN: POST /v1/pair with this code and a device label")
    print(f"  LAN address: http://{ip}:{port}")
    print(f"  pairing URL / QR payload: steward://pair?host={ip}&port={port}")
    print("  pair code also served at GET /pair/code (loopback only: 127.0.0.1)")
    print(f"  data: {ctx.db_path}  mDNS: {'off' if args.no_mdns else 'on'}")
    if server.advertiser is not None and server.advertiser.available:
        print(f"  advertising {server.advertiser.service_name} with TXT "
              f"version={ctx.version} port={port} model={ctx.model} "
              f"paired_count={len(ctx.pairs)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("  shutdown requested")
    finally:
        if scheduler is not None:
            try:
                scheduler.stop()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass
        server.shutdown_server()
        if hasattr(runtime, "shutdown"):
            runtime.shutdown()  # type: ignore[attr-defined]
        print(f"  daemon stopped, port {ctx.port} released")
    return 0


if __name__ == "__main__":
    sys.exit(main())
