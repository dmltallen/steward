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
    GET  /v1/update/check     fetch, verify and report catalog + app updates
    POST /v1/update/apply     download, verify, apply an update with rollback
    GET  / , /index.html , /app.js , /style.css + /v1/webapp/*  (S5 webapp)

Update routing: /v1/update/check and /v1/update/apply are bearer-required
(same gate as every other data/mutating route). The check also runs once at
daemon start in a background thread and the result is recorded on the
daemon; an unreachable update source is a normal start with a recorded
status, never a failed unit. There is no timer, no systemd unit and no
separate updater process: the check runs at boot and when a client asks.
The update source is the released config default (or STEWARD_OTA_URL), so a
consumer never has to set an env var.

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
import threading
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
from steward.tools.loop import run_loop, tool_system_prompt
from steward.webapp import serve as webapp_serve

DEFAULT_HOST = "0.0.0.0"  # LAN-visible by default; auth gates every route
DEFAULT_PORT = 3877
DEFAULT_MODEL = "qwen3:8b"  # resident 8B assistant, the v0.1 pick, benchmarked T1
DEFAULT_SOURCE = str(Path(__file__).resolve().parents[3] / "demo" / "source")
MAX_BODY = 1_000_000
# Chat auto-citation (F4): the daemon grounds a turn in the retrieval index at
# the data root when it has documents. Fail-closed: no db, no documents, a
# backend error, or nothing above the floor all leave the chat turn unchanged.
# The floors are caller-side decisions the citation library deliberately left
# to the daemon (see docs/RETRIEVAL.md); 0.5 is the minimum cosine a hit needs
# to be offered to the model, 7000 chars caps the grounded source block.
CITE_SCORE_FLOOR = 0.5
CITE_BUDGET_CHARS = 7000

try:  # report the package version, not a hand-kept constant
    from steward import __version__ as VERSION
except Exception:  # noqa: BLE001 - the daemon must still start
    VERSION = "0.0.0+unknown"


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
                 db_path: Path, secret: bytes, version: str,
                 tool_overrides: dict | None = None,
                 retrieval_index=None) -> None:
        self.runtime = runtime
        self.model = model
        self.source_dir = source_dir
        self.db_path = db_path
        self.secret = secret
        self.version = version
        # Chat auto-citation index. None means "derive at first use from the
        # data root" (the same <data>/db/index.db the webapp Files tab uses).
        # A test or harness may inject a fixed index to ground a turn without
        # talking to Ollama; setting one never changes what a normal daemon
        # does.
        self.retrieval_index = retrieval_index
        self._retrieval_index_lazy = None
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
        # F8 tools: one registry and one ToolSettings per daemon, rooted at the
        # data root so a chat turn can list/read/write inside the sandbox.
        # No new config keys: allow_shell rides the existing Config (passed by
        # the serve CLI through create_server), everything else is a
        # ToolSettings default. The empty http_allowlist below means http.fetch
        # refuses every host until an operator configures an allowlist.
        from steward.tools import build_registry, build_settings
        tool_overlays = {
            "data_dir": str(data_root),
            "fs_roots": [data_root],
            "approval_db": str(data_root / "db" / "approvals.db"),
            "approval_log": str(data_root / "logs" / "approvals.log"),
            "http_allowlist": [],
        }
        tool_overlays.update(dict(tool_overrides or {}))
        self.tool_settings = build_settings(None, overrides=tool_overlays)
        self.tool_registry = build_registry(self.tool_settings)
        # OTA update state: the source (config default or STEWARD_OTA_URL) and
        # the last check result, both read/written under one lock. The startup
        # check fills update_status at boot; every check/apply records fresh.
        self.ota_source = ""
        self.update_status = {
            "state": "pending", "ok": None, "available": False,
            "checked_at": None, "source": None, "error": None,
        }
        self._update_lock = threading.Lock()
        # Install channel, computed once on first use (never guessed: every
        # value is platform-probed by steward.ota.detect_channel). Tests that
        # stage a fake install set this directly to force the channel.
        self._installed_channel: str | None = None
        # How a client-triggered app restart happens. Empty in tests and when
        # the daemon runs embedded (create_server never installs one); the CLI
        # `serve` path sets it to an in-place exec of the freshly applied app
        # binary, which is what makes "one-click update, daemon restarts into
        # the new build, client reconnects" real. The route only reports
        # restart_intent; whether anything flies depends on this callback.
        self.restart_app_fn = None
        # The serve flags the daemon was started with (the CLI `serve` path
        # sets this), so the restart step can re-exec the applied binary with
        # the exact same port/model/source flags.
        self.serve_argv: list[str] = []

    def record_update_status(self, result: dict) -> None:
        with self._update_lock:
            self.update_status = dict(result)

    def manual_update_command(self) -> str:
        """The command a person can run by hand when auto-restart is off or
        fails. This is the never-a-dead-screen escape hatch the client shows
        next to every refusal or error."""
        return "steward update apply --source %s" % self.update_source()

    def update_source(self) -> str:
        """The effective update source, resolved once from config/env."""
        if not self.ota_source:
            from steward import ota as ota_mod
            self.ota_source = ota_mod.resolve_source()
        return self.ota_source

    def resolved_retrieval_index(self):
        """The chat-citation index: injected one if present, else a lazy
        RetrievalIndex rooted at this daemon's data root (same index the
        webapp Files tab builds). Returns None if it cannot be resolved so
        chat grounding fails closed."""
        if self.retrieval_index is not None:
            return self.retrieval_index
        if self._retrieval_index_lazy is None:
            try:
                from steward.retrieval.index import RetrievalIndex
                data_root = Path(self.db_path).resolve().parent.parent
                self._retrieval_index_lazy = RetrievalIndex(data_root_path=data_root)
            except Exception:  # noqa: BLE001 - grounding is never fatal
                return None
        return self._retrieval_index_lazy

    def installed_channel(self) -> str:
        """The install channel that owns this daemon's binary (cached).

        Computed once via steward.ota.detect_channel(); a forced value set by
        a test (or a scratch-install harness) takes precedence so the managed
        refusal can be exercised without touching the host.
        """
        if self._installed_channel is None:
            from steward import ota as ota_mod
            self._installed_channel = ota_mod.detect_channel()
        return self._installed_channel


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
        elif path == "/v1/update/check":
            if self._require_auth() is None:
                return
            self._handle_update_check()
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
        elif parts[:2] == ["v1", "setup"] and len(parts) == 3:
            # Web wizard routes (S5): loopback-only, delegate to wizard.py.
            self._handle_setup(parts[2])
        elif path == "/v1/chat":
            if self._require_auth() is None:
                return
            self._handle_v1_chat()
        elif path == "/chat":
            if self._require_auth() is None:
                return
            self._handle_chat_shim()
        elif path == "/v1/briefs/generate":
            if self._require_auth() is None:
                return
            try:
                from steward.memory.brief import build_brief
                out = build_brief(None, "today")
                self._json(200, {"ok": True, "brief": out})
            except Exception as exc:  # noqa: BLE001 - surfaced to the client
                self._json(502, {"ok": False, "error": str(exc)})
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
        elif path == "/v1/update/apply":
            # Mutating, so gated like every other mutating route: a valid
            # bearer token is required (see S5/S9 route gating). The strongest
            # gate is cryptographic: a tampered manifest or payload is refused
            # regardless of who calls, before anything is written.
            if self._require_auth() is None:
                return
            self._handle_update_apply()
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
            "channel": self.ctx.installed_channel(),
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

    # -- web wizard (POST /v1/setup/*) -----------------------------------------
    #
    # Delegates to steward.wizard, which owns the resumable state machine. The
    # routes are loopback-only because they mutate machine state (downloads,
    # service starts). Contract, one envelope on every route:
    #   {"ok": bool, "state": str, "done": bool, "message": str,
    #    "transcript": [str], "plan": {...} | null,
    #    "download": null | {model, expected_bytes, done_bytes, percent, state},
    #    "error": null | str}
    # Actions: plan (read), start (resume or reset), step (run one state),
    # status (no change), cancel (stop an in-flight download).

    def _handle_setup(self, action: str) -> None:
        if not self._is_loopback():
            self._json(403, {"error": "setup routes are loopback-only"})
            return
        from steward import wizard as _wizard_mod

        try:
            if action == "plan":
                self._json(200, _wizard_mod.setup_plan())
            elif action == "start":
                data = self._read_json()
                resume = bool(data.get("resume", True))
                self._json(200, _wizard_mod.setup_start(resume=resume))
            elif action == "step":
                self._json(200, _wizard_mod.setup_step())
            elif action == "status":
                self._json(200, _wizard_mod.setup_status())
            elif action == "cancel":
                self._json(200, _wizard_mod.setup_cancel())
            else:
                self._json(404, {"error": "unknown setup action",
                                 "action": action})
        except Exception as exc:  # noqa: BLE001 - setup failures are a 500 body
            self._json(500, {"error": str(exc)})

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

    def _run_tool_loop(self, messages: list, emit) -> str:
        """Drive one chat turn through the agent tool loop (F8).

        every /v1/chat and /chat turn runs run_loop with the tool system prompt
        and the daemon's approval manager, so a tool-eligible reply executes
        instead of returning raw JSON blob text. emit receives the SSE events
        (run_loop streams model deltas and per-step tool events); the shim
        passes emit=None and gets only final text back.

        Before the loop the turn is grounded in the retrieval index (F4 chat
        auto-citation) when the index has documents and scores above the
        floor. The sources are appended to the system prompt as numbered
        blocks and every claim is expected to cite [n]. After the loop the
        citations are resolved and a Sources block is appended to the final
        text when the reply cited anything. Every failure path leaves the
        turn ungrounded (exact pre-F4 behavior), never a chat error.
        """
        grounded = self._ground_chat(messages)
        system_prompt = tool_system_prompt(self.ctx.tool_registry)
        if grounded is not None:
            instruction, selected = grounded
            system_prompt = system_prompt + "\n\n" + instruction
        final = run_loop(
            self.ctx.runtime, self.ctx.tool_registry, self.ctx.tool_settings,
            self.ctx.approval_manager, messages,
            system_prompt=system_prompt,
            model=self.ctx.model, emit=emit)
        if grounded is not None:
            final = self._append_chat_sources(final, grounded[1])
        return final

    def _ground_chat(self, messages: list):
        """Return (prompt_instruction, selected) to ground a turn, or None.

        Fail-closed: None when there is no index (or it has no documents), the
        backend errors, nothing scores above CITE_SCORE_FLOOR, or the chat has
        no fresh user turn. Callers append the returned context to the system
        prompt and cite it as [n]; a None return means the turn is unchanged
        from a plain assistant chat."""
        user_turn = next((m for m in reversed(messages)
                          if m.get("role") == "user"), None)
        question = (user_turn or {}).get("content", "")
        question = (question or "").strip()
        if not question:
            return None
        index = self.ctx.resolved_retrieval_index()
        if index is None:
            return None
        try:
            stats = index.stats()
            if not stats.get("documents"):
                return None
            hits = [h for h in index.search(question, k=8)
                    if float(h.get("score", 0.0)) >= CITE_SCORE_FLOOR]
        except Exception:  # noqa: BLE001 - grounding is never fatal
            return None
        from steward.citations import build_prompt, select_context
        selected = select_context(hits, budget_chars=CITE_BUDGET_CHARS)
        if not selected:
            return None
        return build_prompt(question, selected), selected

    def _append_chat_sources(self, final: str, selected) -> str:
        """Resolve the [n] markers the final reply used and append the Sources
        block, or return the reply untouched when it cited nothing."""
        if not selected:
            return final
        try:
            from steward.citations import extract_citations, render_sources
            result = extract_citations(final, selected)
            sources = render_sources(result["used"])
        except Exception:  # noqa: BLE001 - a broken reply never breaks chat
            return final
        if not sources:
            return final
        return (final or "").rstrip() + "\n\n" + sources

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
        # SSE headers first, so the client starts receiving events immediately.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        final = ""
        try:
            final = self._run_tool_loop(messages, emit=self._sse)
        except Exception as exc:  # noqa: BLE001 - surface mid-stream failure
            self._sse({"error": str(exc)})
        self._sse_done()
        if conversation_id is not None and final:
            store.add_message(self.ctx.db_path, conversation_id, "assistant",
                              final, self.ctx.model)

    def _handle_chat_shim(self) -> None:
        """Old /chat JSON contract, same body and reply shape as v0.1, now
        Bearer-gated. Runs the same tool loop as /v1/chat but without streaming
        events: the mutating reply is the final model text as one JSON object,
        so the phone app never has to parse the SSE wire format.
        """
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
            reply = self._run_tool_loop(messages, emit=None)
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

    # -- update routes (OTA, schema v2) -----------------------------------------

    def _data_root_path(self) -> Path:
        return Path(self.ctx.db_path).resolve().parent.parent

    def _ota_failure_state(self, exc: Exception) -> str:
        """Classify an OTA failure: a signature/shape/checksum refusal vs an
        operational error (unreachable source, download glitch). Both come
        back as a structured 200 so the client shows the real reason instead
        of a dead screen; only the label differs."""
        names = {"SignatureError", "ManifestSignatureError", "MalformedLicense",
                 "MalformedManifest", "ChecksumError", "ChecksumMismatch"}
        return "refused" if type(exc).__name__ in names else "error"

    def _handle_update_check(self) -> None:
        """GET /v1/update/check: fetch, verify and report catalog + app updates.

        Bearer-required like every data route. A tampered manifest comes back
        as a refused state, an unreachable source as an error state; neither
        is a server failure. The result is recorded so the daemon can report
        what the last check found.
        """
        from steward import ota as ota_mod
        root = self._data_root_path()
        source = self.ctx.update_source()
        channel = self.ctx.installed_channel()

        # A package manager owns this install: the check may still report that
        # a newer app version exists, but the in-app updater must not offer a
        # button for it. check_update already reports available=False with the
        # exact managed command; carry the channel on failure paths too so the
        # client never shows an Update button it cannot act on.
        try:
            result = ota_mod.check_update(source=source, root=root,
                                          channel=channel)
            result["state"] = "ok"
        except Exception as exc:  # noqa: BLE001 - every source outcome is a status
            result = {"ok": False, "state": self._ota_failure_state(exc),
                      "error": str(exc), "source": source,
                      "channel": channel}
            managed = channel in ota_mod.MANAGED_CHANNELS
            if managed:
                result["managed_by"] = channel
                result["managed_update_command"] = ota_mod.channel_update_command(channel)
        result["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.ctx.record_update_status(result)
        self._json(200, result)

    def _handle_update_apply(self) -> None:
        """POST /v1/update/apply: download, verify and apply with rollback.

        Bearer-required (mutating, like the other mutating routes). Flow:
        fresh fetch + signature check (a tampered manifest is refused before
        anything is written), then app payload staging through steward.download
        (sha256 enforced, resumable), then apply_update whose atomic pointer
        rename is the commit point and whose health probe restores the previous
        app binary on failure.

        Two response modes, chosen by the request Accept header:

          * default (application/json or no Accept): one JSON result at the
            end, exactly as the CLI and the base OTA tests expect;
          * Accept: text/event-stream: SSE progress. Events are
            {"event":"progress","done":N,"total":M} during the download,
            {"event":"phase","name":"verify|apply|restart"}, and a final
            {"event":"result","result":{...}} plus [DONE]. The web client uses
            this mode so a big payload shows real progress instead of a blank
            button.

        On a successful app install the response carries `restart_intent`
        (the daemon is being replaced by the new build) and `manual_command`
        (what a person can run by hand if the daemon does not come back). The
        actual re-exec happens in ctx.restart_app_fn, which the `serve` CLI
        installs and tests leave empty, so a unit test can assert the intent
        without killing the test process.
        """
        from steward import ota as ota_mod
        root = self._data_root_path()
        source = self.ctx.update_source()
        channel = self.ctx.installed_channel()
        stream = (self.headers.get("Accept") or "").find("text/event-stream") != -1

        def emit(obj: dict) -> None:
            if stream:
                self._sse(obj)

        def progress(done, total) -> None:
            emit({"event": "progress", "done": done, "total": total})

        # A package manager owns this install: apply must refuse, name the
        # manager and the exact command, and never stage or swap the binary.
        # Refuse before fetching a manifest so a managed install performs no
        # network work for a self-update it is not allowed to do. The check
        # route still reports that a newer version exists for the user to see.
        early_refusal = ota_mod.managed_by_update_refusal(channel=channel)
        if early_refusal is not None:
            result = dict(early_refusal)
            result["source"] = source
            # The honest "what a person runs" is the package-manager command;
            # pointing at the in-app updater would loop back into this refusal.
            result["manual_command"] = result.get("update_command") \
                or self.ctx.manual_update_command()
            self.ctx.record_update_status(result)
            if stream:
                emit({"event": "result", "result": result})
                emit({"event": "phase", "name": "done"})
                self._sse_done()
            else:
                self._json(200, result)
            return

        try:
            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
            doc = ota_mod.fetch_document(source)
            payload = ota_mod.verify_manifest(doc)   # tampered == refused here
            emit({"event": "phase", "name": "verify"})
            staged = None
            if payload.get("app") is not None:
                staged = ota_mod.stage_app_payload(
                    doc, source=source, root=root, progress=progress)
                if not staged["staged"]:
                    result = {"ok": False, "state": "refused",
                              "error": staged["reason"], "source": source,
                              "manual_command": self.ctx.manual_update_command()}
                    emit({"event": "result", "result": result})
                    emit({"event": "phase", "name": "done"})
                    self._sse_done()
                    self.ctx.record_update_status(result)
                    return
            emit({"event": "phase", "name": "apply"})
            probe = (ota_mod.app_binary_probe(staged["path"], staged["version"])
                     if staged is not None and staged["staged"]
                     else ota_mod.backend_health_probe(self.ctx.model))
            result = ota_mod.apply_update(doc, health_probe=probe, root=root,
                                          timeout=ota_mod.APPLY_PROBE_TIMEOUT_S)
            result["ok"] = bool(result.get("applied"))
            result["rolled_back"] = bool(result.get("rolled_back"))
            result["source"] = source
            result["state"] = ("applied" if result["ok"]
                               else "rolled_back" if result["rolled_back"]
                               else "noop")
            result["manual_command"] = self.ctx.manual_update_command()
            # Tell the client when the daemon is about to be replaced so it can
            # show a reconnecting state instead of a dead screen. The re-exec
            # itself is ctx.restart_app_fn, installed only by the `serve` path.
            if result["state"] == "applied" and (result.get("app") or {}).get(
                    "installed"):
                result["restart_intent"] = {
                    "pending": True,
                    "version": (result.get("app") or {}).get("version"),
                    "manual": self.ctx.manual_update_command(),
                }
                # Fire-and-forget the real re-exec (None in tests / embedded).
                if self.ctx.restart_app_fn is not None:
                    emit({"event": "phase", "name": "restart"})
                    self._flush_stream()
                    self._schedule_restart_apply(result)
            self.ctx.record_update_status(result)
            if stream:
                emit({"event": "result", "result": result})
                emit({"event": "phase", "name": "done"})
                self._sse_done()
            else:
                self._json(200, result)
        except Exception as exc:  # noqa: BLE001 - OTA refusal/error is a body
            result = {"ok": False,
                      "state": self._ota_failure_state(exc),
                      "error": str(exc), "source": source,
                      "manual_command": self.ctx.manual_update_command()}
            self.ctx.record_update_status(result)
            if stream:
                emit({"event": "result", "result": result})
                emit({"event": "phase", "name": "done"})
                self._sse_done()
            else:
                self._json(200, result)

    def _flush_stream(self) -> None:
        try:
            self.wfile.flush()
        except Exception:  # noqa: BLE001 - flush is best effort mid-restart
            pass

    def _schedule_restart_apply(self, result: dict) -> None:
        """Hand the applied app to the daemon's restart hook on another thread.

        ctx.restart_app_fn replaces the running image with the freshly applied
        app binary (`os.execv` the installed payload with `serve` args). It is
        only installed by the CLI `serve` path; tests and embedded daemons keep
        it None and observe restart_intent instead. Running the re-exec on a
        thread means the SSE response is fully flushed before the image is
        replaced, and the client reconnects on its own.
        """
        fn = self.ctx.restart_app_fn
        if fn is None:
            return

        def _go() -> None:
            time.sleep(1.0)  # let the result event + [DONE] reach the client
            try:
                import os as _os
                _os.environ.setdefault("STEWARD_RESTART_TRACE", "")
                if _os.environ.get("STEWARD_RESTART_TRACE"):
                    with open(_os.environ["STEWARD_RESTART_TRACE"], "a") as _fh:
                        _fh.write("restart hook fired at %s\n" % time.time())
                fn(result)
            except Exception:  # noqa: BLE001 - a failed restart keeps the daemon alive
                import sys
                print("  update: restart step failed (%s); "
                      "the current build is still serving"
                      % sys.exc_info()[1], file=sys.stderr)

        threading.Thread(target=_go, daemon=True, name="steward-update-restart").start()


def build_restart_argv(app_path: str, serve_argv: list[str]) -> list[str]:
    """argv for re-exec'ing the daemon into a freshly applied app binary.

    The installed payload is a full `steward` binary; re-running it with the
    same subcommand and flags as the current process (public API, not
    internals) keeps the port, model, source and other serve flags identical
    across the swap, so the client reconnects to the same address.
    """
    return [str(app_path), "serve"] + list(serve_argv or [])


def restart_into_app(result: dict, serve_argv: list[str]) -> dict:
    """In-place re-exec of the running daemon into the applied app binary.

    Called from the `serve` CLI path only. The health probe during apply
    already proved the installed payload answers `steward version`, so the
    exec here is the controlled second step: same process, new image, port
    rebound by the fresh daemon. Returns a dict (never raises) so the caller
    has an honest reason on a machine where exec fails.
    """
    from steward import ota as ota_mod
    app = (result or {}).get("app") or {}
    installed = bool(app.get("installed"))
    path = app.get("path")
    if not installed or not path or not Path(path).is_file():
        return {"restarted": False,
                "reason": "no installed app binary to re-exec"}
    argv = build_restart_argv(path, serve_argv)
    try:
        os.execv(path, argv)  # replaces this process image
    except Exception as exc:  # noqa: BLE001 - report, keep the old build serving
        return {"restarted": False, "reason": str(exc)}
    return {"restarted": True}  # only reached on a broken execv in tests


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
                  tool_overrides: dict | None = None,
                  retrieval_index=None,
                  version: str = VERSION) -> StewardServer:
    """Build a fully-wired StewardServer.

    The knobs a caller (tests, CLI, mobile dev) commonly needs are runtime,
    db_path and secret_file; everything else has a sane default. db_path
    defaults to <data>/db/steward.db under STEWARD_HOME (or ~/.steward).
    tool_overrides lets the CLI and tests pin ToolSettings knobs (allow_shell,
    fs_roots, http_allowlist); defaults root the sandbox at the data dir and
    keep allow_shell false and the http allowlist empty. retrieval_index lets
    a test inject a fixed index so a chat turn grounds without Ollama; a
    normal daemon leaves it None and derives one from the data root on first
    use (same index the webapp Files tab builds).
    """
    from steward.paths import db_dir

    db_path = Path(db_path) if db_path else db_dir() / "steward.db"
    store.init_schema(db_path)
    key = secret if secret else load_or_create_secret(secret_file or (db_path.parent / "pairing.key"))
    ctx = DaemonContext(runtime=runtime, model=model,
                        source_dir=source_dir or DEFAULT_SOURCE,
                        db_path=db_path, secret=key, version=version,
                        tool_overrides=tool_overrides,
                        retrieval_index=retrieval_index)
    return StewardServer(addr, ctx, advertise=advertise,
                         instance_name_override=instance_name_override,
                         host_ip=host_ip)


def run_startup_update_check(ctx: DaemonContext) -> dict:
    """One-shot update check at daemon boot; records status and never raises.

    Spawned as a daemon thread so a slow or unreachable source never blocks
    serve_forever: an unreachable source is a normal start with a recorded
    status (`ctx.update_status`), never a failed unit. No timer, no systemd
    unit, no separate updater process; clients can still ask for a fresh
    check on demand via GET /v1/update/check.
    """
    from steward import ota as ota_mod
    root = Path(ctx.db_path).resolve().parent.parent
    source = ctx.update_source()
    result = ota_mod.startup_check_status(source=source, root=root)
    ctx.record_update_status(result)
    ok = result.get("ok") is True and result.get("state") == "ok"
    if ok:
        latest = ((result.get("app") or {}).get("latest_version")
                  or (result.get("catalog") or {}).get("latest_version") or "?")
        print("  update: ok, newest version %s" % latest)
    else:
        print("  update: recorded %s (%s)"
              % (result.get("state"), result.get("error") or "no source"))
    return result


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
                           advertise=not args.no_mdns,
                           tool_overrides={"allow_shell": bool(cfg.allow_shell)})

    # One-click update: after a successful apply the freshly installed app
    # binary replaces this process in place (`os.execv` with the same serve
    # flags). Only the real `serve` path installs the hook; tests and embedded
    # daemons keep it None and observe restart_intent on the route result.
    ctx = server.ctx
    ctx.serve_argv = list(argv or [])
    ctx.restart_app_fn = lambda result: restart_into_app(result, ctx.serve_argv)

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
    # OTA source from the released config (or STEWARD_OTA_URL), then a
    # one-shot background check at boot. The boot check records its status on
    # the daemon and never blocks or fails startup, even when the source is
    # unreachable or tampered.
    ctx.ota_source = cfg.ota_url or ""
    _upd_thread = threading.Thread(
        target=run_startup_update_check, args=(ctx,),
        daemon=True, name="steward-update-check",
    )
    _upd_thread.start()
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
