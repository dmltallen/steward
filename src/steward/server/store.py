"""Conversation store: SQLite at <data>/db/steward.db, WAL mode.

Two tables, one-to-many:

    conversations(id INTEGER PK, title TEXT, created_at TEXT)
    messages(id INTEGER PK, conversation_id FK -> conversations, role TEXT,
             content TEXT, created_at TEXT, model TEXT)

Each operation opens a short-lived connection with busy_timeout and foreign
keys on, so a threaded HTTP server can call in concurrently without sharing a
connection. WAL mode is set at the database level (persistent) on every open.

Routes in the daemon call these methods; the daemon never touches SQL.

created_at is ISO-8601 UTC (datetime.now(timezone.utc).isoformat()).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL DEFAULT 'New conversation',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    model TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(conversation_id, id);
"""


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    """Open one short-lived connection with the daemon's pragmas applied."""
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(db_path: Path) -> None:
    """Create tables if missing. Idempotent; safe to call every boot."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Cursor:
    return conn.execute(sql, params)


def _conversation_row(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "title": row["title"], "created_at": row["created_at"]}


def create_conversation(db_path: Path, title: str | None = None) -> dict:
    now = utc_now()
    title = (title or "").strip() or "New conversation"
    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO conversations (title, created_at) VALUES (?, ?)",
            (title, now),
        )
        cid = cur.lastrowid
    return {"id": cid, "title": title, "created_at": now}


def list_conversations(db_path: Path) -> list[dict]:
    """All conversations, newest first, each with its message count."""
    out: list[dict] = []
    with connect(db_path) as conn:
        rows = _rows(
            conn,
            "SELECT c.id, c.title, c.created_at, "
            "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count "
            "FROM conversations c ORDER BY c.id DESC",
        )
        for row in rows:
            item = _conversation_row(row)
            item["message_count"] = row["message_count"]
            out.append(item)
    return out


def get_conversation(db_path: Path, conversation_id: int) -> dict | None:
    """A conversation plus its messages in order, or None when not found."""
    with connect(db_path) as conn:
        row = _rows(conn, "SELECT id, title, created_at FROM conversations WHERE id = ?",
                    (conversation_id,)).fetchone()
        if row is None:
            return None
        item = _conversation_row(row)
        item["messages"] = [
            {"id": m["id"], "role": m["role"], "content": m["content"],
             "created_at": m["created_at"], "model": m["model"]}
            for m in _rows(conn, "SELECT id, role, content, created_at, model "
                                 "FROM messages WHERE conversation_id = ? "
                                 "ORDER BY id", (conversation_id,))
        ]
    return item


def delete_conversation(db_path: Path, conversation_id: int) -> bool:
    """Delete a conversation and its messages (cascade)."""
    with connect(db_path) as conn:
        cur = conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        return cur.rowcount > 0


def add_message(db_path: Path, conversation_id: int, role: str,
                content: str, model: str | None = None) -> dict | None:
    """Append a message to a conversation; None when the conversation is gone."""
    now = utc_now()
    with connect(db_path) as conn:
        exists = _rows(conn, "SELECT id FROM conversations WHERE id = ?",
                       (conversation_id,)).fetchone() is not None
        if not exists:
            return None
        cur = conn.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at, model) "
            "VALUES (?, ?, ?, ?, ?)",
            (conversation_id, role, content, now, model),
        )
        mid = cur.lastrowid
    return {"id": mid, "conversation_id": conversation_id, "role": role,
            "content": content, "created_at": now, "model": model}
