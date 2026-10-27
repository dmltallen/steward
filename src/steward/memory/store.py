"""Facts and briefs store: SQLite at <data>/db/memory.db, WAL mode.

Two tables:

    facts(id, text, source, confidence, created_at, updated_at, user_edited)
    briefs(id, date, text, created_at, model)

Facts are the assistant's long-term memory with provenance. A fact knows where
it came from (source), how sure we are (confidence 0..1), and whether a human
confirmed or edited it (user_edited). Candidates distilled from conversations
are written with source='conversation' and confidence < 1; when a user edits a
fact it is promoted with user_edited=1, and user-edited facts win in prompts
(see for_prompt).

Each operation opens a short-lived connection with busy_timeout and WAL, so a
threaded caller (the scheduler runner or the daemon) can use the store
concurrently without sharing a connection. created_at and updated_at are
ISO-8601 UTC strings (datetime.now(timezone.utc).isoformat(timespec='seconds')).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from steward.paths import data_root

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    confidence REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    user_edited INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_facts_source ON facts(source);
CREATE TABLE IF NOT EXISTS briefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    model TEXT
);
CREATE INDEX IF NOT EXISTS idx_briefs_date ON briefs(date);
"""

# Conversation distillation always writes candidates below this confidence.
CANDIDATE_CONFIDENCE = 0.7

# Facts above this confidence are treated as trusted for prompt assembly.
TRUSTED_CONFIDENCE = 0.85


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    """Open one short-lived connection with the store pragmas applied."""
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


def _fact_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "text": row["text"],
        "source": row["source"],
        "confidence": round(float(row["confidence"]), 6),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "user_edited": bool(row["user_edited"]),
    }


def _brief_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "date": row["date"],
        "text": row["text"],
        "created_at": row["created_at"],
        "model": row["model"],
    }


class MemoryStore:
    """SQLite facts and briefs store.

    db_path defaults to <data_root>/db/memory.db (see steward.paths). Pass an
    explicit path in tests. Every method opens its own short-lived connection.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is not None:
            path = Path(db_path)
            self.db_path = path / "db" / "memory.db" if path.suffix != ".db" \
                else path
        else:
            self.db_path = data_root() / "db" / "memory.db"
        init_schema(self.db_path)

    # -- facts ----------------------------------------------------------------

    def add_fact(self, text: str, source: str = "manual",
                 confidence: float = 1.0) -> dict:
        """Add a fact. Manual adds are trusted by default (confidence 1.0)."""
        return self.add_candidate(
            text, source=source, confidence=confidence, user_edited=False
        )

    def add_candidate(self, text: str, source: str = "conversation",
                      confidence: float | None = None,
                      user_edited: bool = False) -> dict:
        """Add a fact, clamped so conversation candidates stay below 1.0.

        source != 'manual' (conversation distillation, imports, tools) writes
        a candidate: confidence is clamped to below 1 and user_edited stays 0
        until a human confirms it. Callers may pass user_edited=True to mark
        the fact as human-confirmed at write time.
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("fact text must not be empty")
        confidence = CANDIDATE_CONFIDENCE if confidence is None else float(confidence)
        if source != "manual":
            confidence = min(confidence, 0.999)
        now = utc_now()
        with connect(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO facts(text, source, confidence, created_at, updated_at,"
                " user_edited) VALUES(?, ?, ?, ?, ?, ?)",
                (text, source, confidence, now, now, 1 if user_edited else 0),
            )
            fid = cur.lastrowid
        assert fid is not None
        fact = self.get_fact(fid)
        assert fact is not None
        return fact

    def get_fact(self, fact_id: int) -> dict | None:
        with connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM facts WHERE id = ?",
                               (fact_id,)).fetchone()
        return _fact_row(row) if row else None

    def update_fact(self, fact_id: int, text: str | None = None,
                    confidence: float | None = None,
                    user_edited: bool | None = None) -> dict | None:
        """Edit a fact. Any change bumps updated_at; user_edited=True promotes
        the fact so it wins in prompts. Returns None when not found."""
        current = self.get_fact(fact_id)
        if current is None:
            return None
        text = current["text"] if text is None else (text or "").strip()
        if not text:
            raise ValueError("fact text must not be empty")
        confidence = current["confidence"] if confidence is None else float(confidence)
        edited = current["user_edited"]
        if user_edited is not None:
            edited = bool(user_edited)
        now = utc_now()
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE facts SET text=?, confidence=?, user_edited=?, updated_at=?"
                " WHERE id=?",
                (text, confidence, 1 if edited else 0, now, fact_id),
            )
        return self.get_fact(fact_id)

    def set_user_edited(self, fact_id: int) -> dict | None:
        """Promote a fact to human-confirmed (user_edited=1)."""
        return self.update_fact(fact_id, user_edited=True)

    def delete_fact(self, fact_id: int) -> bool:
        with connect(self.db_path) as conn:
            return conn.execute("DELETE FROM facts WHERE id = ?",
                                (fact_id,)).rowcount > 0

    def list_facts(self, source: str | None = None,
                   only_user_edited: bool = False) -> list[dict]:
        """All facts, oldest first. Optional filters by source and edit state."""
        sql = "SELECT * FROM facts"
        clauses: list[str] = []
        params: list = []
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if only_user_edited:
            clauses.append("user_edited = 1")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with connect(self.db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_fact_row(r) for r in rows]

    def for_prompt(self, limit: int | None = None) -> list[dict]:
        """Facts to put in a prompt: user-edited first, then by confidence.

        A verbatim duplicate of a user-edited fact is dropped, so a later low
        confidence conversation candidate that repeats a confirmed fact never
        shadows it (user edits win). Results are ordered user-edited first,
        then confidence descending, then id.
        """
        facts = self.list_facts()
        edited_texts = {f["text"] for f in facts if f["user_edited"]}
        kept = [
            f for f in facts
            if f["user_edited"] or f["text"] not in edited_texts
        ]
        kept.sort(key=lambda f: (0 if f["user_edited"] else 1,
                                 -f["confidence"], f["id"]))
        if limit is not None and limit > 0:
            kept = kept[:limit]
        return kept

    # -- briefs ---------------------------------------------------------------

    def add_brief(self, date: str, text: str, model: str | None = None) -> dict:
        """Store one generated brief. date is a 'YYYY-MM-DD' string."""
        text = (text or "").strip()
        if not text:
            raise ValueError("brief text must not be empty")
        now = utc_now()
        with connect(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO briefs(date, text, created_at, model) VALUES(?, ?, ?, ?)",
                (date, text, now, model),
            )
            bid = cur.lastrowid
        row = {
            "id": bid, "date": date, "text": text,
            "created_at": now, "model": model,
        }
        return row

    def brief_for_date(self, date: str) -> dict | None:
        """Most recent brief stored for a date, or None."""
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM briefs WHERE date = ? ORDER BY id DESC LIMIT 1",
                (date,),
            ).fetchone()
        return _brief_row(row) if row else None

    def list_briefs(self, date: str | None = None,
                    limit: int | None = None) -> list[dict]:
        """Briefs, newest first. Optional filter by date and a row cap."""
        sql = "SELECT * FROM briefs"
        params: list = []
        if date is not None:
            sql += " WHERE date = ?"
            params.append(date)
        sql += " ORDER BY id DESC"
        if limit is not None and limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        with connect(self.db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_brief_row(r) for r in rows]

    def delete_brief(self, brief_id: int) -> bool:
        with connect(self.db_path) as conn:
            return conn.execute("DELETE FROM briefs WHERE id = ?",
                                (brief_id,)).rowcount > 0


# --------------------------------------------------------------------------
# Conversation summarization
# --------------------------------------------------------------------------


def summarize_conversation(messages: list[dict], runtime, *,
                           store: MemoryStore | None = None,
                           model: str | None = None,
                           max_facts: int = 8) -> list[dict]:
    """Distill a conversation transcript into candidate facts.

    Runs one completion against the runtime seam asking for a JSON list of
    durable facts, then writes each entry as a candidate (source='conversation',
    confidence below 1, user_edited=0). The runtime returns a streaming
    generator like Runtime.chat; this joins all deltas.

    The model reply is parsed defensively: it may be a raw JSON array or text
    containing one. Entries that do not parse or have no usable text are
    skipped, never raised on. Returns the stored candidate facts.
    """
    store = store if store is not None else MemoryStore()
    _json_snippet = "\n".join(
        f"{m.get('role', '?')}: {m.get('content', '')}" for m in messages
    ) if messages else "(no transcript provided)"
    system = (
        "You are Steward's memory writer. Read the transcript and reply with "
        f"a JSON array of at most {max_facts:d} durable facts a user would "
        "still want to know tomorrow. Each entry is an object with one key "
        "'text' holding a single plain-language fact. Facts must be timeless, "
        "not chat chit-chat. Reply with only the JSON array, nothing else. "
        "Never use em dashes."
    )
    user = f"Transcript:\n{_json_snippet}\n\nFacts as JSON:"
    reply = "".join(
        d["delta"] for d in runtime.chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            stream=False, model=model,
        )
    )
    entries = _parse_fact_json(reply)
    stored: list[dict] = []
    seen: set[str] = set()
    for entry in entries:
        text = (entry.get("text") or "").strip() if isinstance(entry, dict) else ""
        if not text or text in seen:
            continue
        seen.add(text)
        stored.append(store.add_candidate(text, source="conversation"))
    return stored


def _parse_fact_json(reply: str) -> list:
    """Pull a JSON array out of a model reply, defensively."""
    import json as _json
    import re as _re

    if not reply:
        return []
    try:
        data = _json.loads(reply)
        if isinstance(data, list):
            return data
    except ValueError:
        pass
    match = _re.search(r"\[[\s\S]*\]", reply)
    if not match:
        return []
    try:
        data = _json.loads(match.group(0))
    except ValueError:
        return []
    return data if isinstance(data, list) else []
