"""Pending approvals for write/exec/net tool calls.

A mutating call (risk in {write, exec, net}) creates a pending row
(id, tool, args, risk, created_at, expires_at, decision) in a SQLite store at
<data>/db/approvals.db, then the caller blocks up to the configured timeout so
a human can decide. The daemon contract over HTTP:

    GET  /v1/approvals          list approvals (newest first)
    POST /v1/approvals/{id}     body {"approve": true|false} or
                                {"decision": "approve"|"deny"} decides a row

The agent loop calls wait(), which returns as soon as a decision lands and
auto-denies when the block expires. Every creation and every decision is
appended to a JSON-lines log carrying the args hash, so the trail is
auditable. Threads never share a connection (short-lived opens, WAL), mirroring
steward.server.store.

Satrtup note for the daemon integrator: mount approvals_router() on the
existing dispatcher (two lines), one ApprovalManager per DaemonContext.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APPROVAL_DECISIONS = ("approved", "denied")

SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    risk TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decision TEXT,
    reason TEXT,
    decided_at TEXT,
    decided_by TEXT
);
"""


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _ts(iso_value: str) -> float:
    try:
        return datetime.fromisoformat(iso_value).timestamp()
    except (TypeError, ValueError):
        return 0.0


def args_hash(args: dict) -> str:
    """Stable, canonical hash of the tool arguments (sort keys, no spaces)."""
    payload = json.dumps(args, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _normalize_decision(decision) -> str | None:
    if decision is True or decision in ("approved", "approve"):
        return "approved"
    if decision is False or decision in ("denied", "deny"):
        return "denied"
    return None


class ApprovalManager:
    """SQLite-backed pending approvals with blocking wait + decision wakeup."""

    def __init__(self, db_path, ttl_s: int = 120, log_path=None) -> None:
        self.db_path = db_path if isinstance(db_path, str) else str(db_path)
        self.ttl_s = int(ttl_s)
        if log_path is None:
            log_head = self.db_path.rsplit("/db/", 1)[0]  # <data>/logs/approvals.log
            self.log_path = str(log_head) + "/logs/approvals.log"
        else:
            self.log_path = str(log_path)
        self._lock = threading.Lock()
        self._events: dict[int, threading.Event] = {}
        import os
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    # -- store ------------------------------------------------------------

    def create(self, tool: str, args: dict, risk: str) -> dict:
        now = _now()
        row = {
            "tool": tool,
            "args": json.dumps(args, sort_keys=True),
            "args_hash": args_hash(args),
            "risk": risk,
            "created_at": _iso(now),
            "expires_at": _iso(now + self.ttl_s),
            "decision": None,
            "reason": None,
            "decided_at": None,
            "decided_by": None,
        }
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO approvals (tool, args, args_hash, risk, created_at, "
                "expires_at, decision, reason, decided_at, decided_by) "
                "VALUES (:tool, :args, :args_hash, :risk, :created_at, :expires_at, "
                ":decision, :reason, :decided_at, :decided_by)",
                row,
            )
            aid = int(cur.lastrowid or 0)
        record = {"id": aid, **row}
        self._log(record, action="created")
        return self._decorate(record)

    def get(self, approval_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE id = ?",
                               (approval_id,)).fetchone()
        if row is None:
            return None
        return self._decorate(self._from_row(row))

    def list_all(self, status: str | None = None) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM approvals ORDER BY id DESC").fetchall()
        out = [self._decorate(self._from_row(row)) for row in rows]
        if status:
            out = [r for r in out if r["status"] == status]
        return out

    def _from_row(self, row: sqlite3.Row) -> dict:
        return {
            "id": int(row["id"]), "tool": row["tool"], "risk": row["risk"],
            "args": row["args"], "args_hash": row["args_hash"],
            "created_at": row["created_at"], "expires_at": row["expires_at"],
            "decision": row["decision"], "reason": row["reason"],
            "decided_at": row["decided_at"], "decided_by": row["decided_by"],
        }

    def _decorate(self, record: dict) -> dict:
        out = dict(record)
        if out["decision"] is not None:
            out["status"] = out["decision"]  # approved | denied
        elif _now() >= _ts(out["expires_at"]):
            out["status"] = "expired"        # pending but past its deadline
        else:
            out["status"] = "pending"
        try:
            out["args"] = json.loads(out["args"]) if out["args"] else {}
        except json.JSONDecodeError:
            out["args"] = {}
        return out

    # -- decide / wait -------------------------------------------------------

    def decide(self, approval_id: int, decision, decided_by: str = "api") -> dict:
        """Approve or deny one pending row. Returns {ok, approval} or an error."""
        record = self.get(approval_id)
        if record is None:
            return {"ok": False, "error": "approval not found"}
        norm = _normalize_decision(decision)
        if norm is None:
            return {"ok": False, "error": "decision must be approve or deny"}
        if record["status"] in ("approved", "denied"):
            return {"ok": False, "error": "approval already %s" % record["status"],
                    "approval": record}
        if _now() >= _ts(record["expires_at"]):
            # A pending row past its deadline can no longer be approved; the
            # block already denied it, so record and mirror the denial.
            self._mark(approval_id, "denied", "expired", decided_by)
            return {"ok": True, "note": "approval had expired; denied",
                    "approval": self.get(approval_id)}
        self._mark(approval_id, norm, "user", decided_by)
        return {"ok": True, "approval": self.get(approval_id)}

    def wait(self, approval_id: int, timeout_s: float | None = None):
        """Block until decided, or auto-deny on timeout/expiry.

        Returns (decision, reason) with decision in {approved, denied}.
        """
        timeout = float(timeout_s if timeout_s is not None else self.ttl_s)
        self._event(approval_id).wait(timeout)
        record = self.get(approval_id)
        if record is None:
            return "denied", "missing"
        status = record["status"]
        if status == "approved":
            return "approved", record["reason"] or "user"
        if status == "denied":
            return "denied", record["reason"] or "user"
        if status == "expired":
            self._mark(approval_id, "denied", "expired", "system")
            return "denied", "expired"
        self._mark(approval_id, "denied", "timeout", "system")
        return "denied", "timeout"

    def _mark(self, approval_id: int, decision: str, reason: str,
              decided_by: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE approvals SET decision = ?, reason = ?, decided_at = ?, "
                "decided_by = ? WHERE id = ?",
                (decision, reason, _iso(_now()), decided_by, approval_id),
            )
        record = self.get(approval_id)
        if record is not None:
            self._log(record, action="decision", decision=decision,
                      reason=reason, decided_by=decided_by)
        with self._lock:
            event = self._events.pop(approval_id, None)
        if event is not None:
            event.set()

    def _event(self, approval_id: int) -> threading.Event:
        with self._lock:
            event = self._events.get(approval_id)
            if event is None:
                event = self._events[approval_id] = threading.Event()
            return event

    # -- audit trail -----------------------------------------------------------

    def _log(self, record: dict, **extra) -> None:
        line = {"args_hash": record["args_hash"],
                "id": record["id"], "tool": record["tool"], "risk": record["risk"],
                "created_at": record["created_at"], "expires_at": record["expires_at"]}
        line.update(extra)
        try:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, sort_keys=True) + "\n")
                fh.flush()
        except OSError:
            pass  # a clogged log must never block a decision


def approvals_router(manager: ApprovalManager):
    """Mount point for the daemon. Returns handle(method, parts, body) ->
    (status, payload) matching the /v1/approvals contract.
    """

    def handle(method: str, parts: list[str], body: dict | None = None):
        if method == "GET" and parts == ["v1", "approvals"]:
            all_rows = manager.list_all()
            pending = [r for r in all_rows if r["status"] == "pending"]
            return 200, {"approvals": all_rows, "pending": len(pending)}
        if method == "POST" and len(parts) == 3 and parts[:2] == ["v1", "approvals"]:
            try:
                approval_id = int(parts[2])
            except ValueError:
                return 404, {"error": "approval not found"}
            payload = body or {}
            decision = payload.get("decision")
            if decision is None and "approve" in payload:
                decision = payload["approve"]
            result = manager.decide(approval_id, decision,
                                    decided_by=str(payload.get("decided_by") or "api"))
            if not result["ok"] and result.get("error") == "approval not found":
                return 404, {"error": "approval not found"}
            if not result["ok"]:
                return 409, result
            return 200, result
        return 404, {"error": "not found"}

    return handle


class _ApprovalsHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, format, *args):  # quiet the access log
        pass

    def _dispatch(self, method: str) -> None:
        path = (self.path or "").split("?", 1)[0]
        parts = [p for p in path.split("/") if p]
        body = self._read_json() if method == "POST" else None
        status, payload = self.server.manager_router(method, parts, body)  # type: ignore[attr-defined]
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    _MAX_BODY = 1_000_000

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, self._MAX_BODY))
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")


class ApprovalsHTTPServer(ThreadingHTTPServer):
    """Convenience server exposing the /v1/approvals contract on its own port.

    Used by tests and the demo. The daemon mount path is approvals_router(),
    which serves the identical contract on the existing dispatcher.
    """

    daemon_threads = True

    def __init__(self, manager: ApprovalManager,
                 addr=("127.0.0.1", 0)) -> None:
        super().__init__(addr, _ApprovalsHandler)
        self.manager_router = approvals_router(manager)
