"""Tests for src/steward/memory: facts store, briefs, calendar, brief build.

The corpus for brief building is stubbed: a FakeIndex for retrieval and either
a stub runtime (fixed reply) or the in-process fake Ollama server for the real
runtime seam. No test touches the network or a real model.
"""

from __future__ import annotations

import json
import socket
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_ollama_bin as fake  # noqa: E402

import steward.memory.store as store_mod  # noqa: E402
from steward.config import Config  # noqa: E402
from steward.memory import (  # noqa: E402
    CalendarEvent,
    MemoryStore,
    assemble_brief_prompt,
    build_brief,
    events_on,
    parse_ics,
    summarize_conversation,
)
from steward.memory.ics import load_calendar_events  # noqa: E402
from steward.runtime import SystemOllama  # noqa: E402


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def make_cfg(home: Path, **env) -> Config:
    return Config.load(env={"STEWARD_HOME": str(home), **env})


class FakeIndex:
    """Retrieval stub: returns canned hits, records the queries."""

    def __init__(self, hits: list | None = None) -> None:
        self.hits = list(hits or [])
        self.queries: list[str] = []

    def search(self, query, k: int = 8) -> list[dict]:
        self.queries.append(query)
        return [dict(h) for h in self.hits]


class StubRuntime:
    """Chat stub: returns a fixed reply and records the prompt + model."""

    def __init__(self, reply: str = "Stub brief reply.") -> None:
        self.reply = reply
        self.calls: list = []

    def chat(self, messages, stream: bool = True, model: str | None = None):
        self.calls.append(([dict(m) for m in messages], model))
        return [{"delta": self.reply}]


# --------------------------------------------------------------------------
# Facts CRUD with provenance
# --------------------------------------------------------------------------


def test_fact_add_get_update_delete_with_provenance(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_now():
        calls["n"] += 1
        return "2026-10-26T%02d:00:00+00:00" % calls["n"]

    monkeypatch.setattr(store_mod, "utc_now", fake_now)
    store = MemoryStore(tmp_path / "home" / "db" / "memory.db")

    fact = store.add_fact("Don lives in Oregon.", source="manual")
    assert fact["source"] == "manual"
    assert fact["confidence"] == 1.0
    assert fact["user_edited"] is False
    assert fact["created_at"] == "2026-10-26T01:00:00+00:00"

    # read back
    got = store.get_fact(fact["id"])
    assert got is not None
    assert got["text"] == "Don lives in Oregon."

    # update bumps updated_at and can promote to user-edited
    updated = store.update_fact(fact["id"], text="Don lives in Portland, Oregon.",
                                user_edited=True)
    assert updated is not None
    assert updated["user_edited"] is True
    assert updated["updated_at"] == "2026-10-26T02:00:00+00:00"
    assert updated["updated_at"] > updated["created_at"]
    reread = store.get_fact(fact["id"])
    assert reread is not None
    assert reread["text"].startswith("Don lives in Portland")

    # delete
    assert store.delete_fact(fact["id"]) is True
    assert store.get_fact(fact["id"]) is None
    assert store.delete_fact(fact["id"]) is False


def test_update_missing_fact_returns_none(tmp_path):
    store = MemoryStore(tmp_path / "db" / "memory.db")
    assert store.update_fact(999, text="x") is None
    assert store.set_user_edited(999) is None


def test_candidate_writes_below_one_and_conversation_source(tmp_path):
    store = MemoryStore(tmp_path / "db" / "memory.db")
    candidate = store.add_candidate("client prefers weekly calls.", source="conversation")
    assert candidate["source"] == "conversation"
    assert candidate["confidence"] < 1.0
    assert candidate["user_edited"] is False
    # a manual call with an explicit high confidence is not clamped
    manual = store.add_fact("renewal is due November 30.", confidence=0.95)
    assert manual["confidence"] == 0.95


def test_empty_fact_text_rejected(tmp_path):
    store = MemoryStore(tmp_path / "db" / "memory.db")
    with pytest.raises(ValueError):
        store.add_fact("   ")
    fact = store.add_fact("ok")
    with pytest.raises(ValueError):
        store.update_fact(fact["id"], text="")


def test_list_filters_by_source_and_edit_state(tmp_path):
    store = MemoryStore(tmp_path / "db" / "memory.db")
    store.add_fact("a", source="manual")
    store.add_fact("b", source="manual")
    store.add_candidate("c", source="conversation")
    store.add_candidate("d", source="conversation")
    edited = store.add_fact("e", source="manual", confidence=0.9)
    store.set_user_edited(edited["id"])

    assert len(store.list_facts()) == 5
    assert len(store.list_facts(source="conversation")) == 2
    assert len(store.list_facts(only_user_edited=True)) == 1
    assert store.list_facts(only_user_edited=True)[0]["text"] == "e"


def test_for_prompt_user_edited_wins_over_duplicate_candidate(tmp_path):
    store = MemoryStore(tmp_path / "db" / "memory.db")
    store.add_candidate("backup runs nightly.", source="conversation")
    promoted = store.add_candidate("backup runs nightly.", source="conversation")
    store.set_user_edited(promoted["id"])
    store.add_candidate("backup is broken.", source="conversation", confidence=0.99)

    ordered = store.for_prompt()
    texts = [f["text"] for f in ordered]
    # the user-edited duplicate wins; the other candidate with the same text drops
    assert texts.count("backup runs nightly.") == 1
    assert ordered[0]["user_edited"] is True
    assert ordered[0]["text"] == "backup runs nightly."
    # remaining candidates order by confidence desc
    assert ordered[1]["text"] == "backup is broken."


def test_for_prompt_respects_limit(tmp_path):
    store = MemoryStore(tmp_path / "db" / "memory.db")
    for i in range(5):
        store.add_fact("fact %d" % i)
    assert len(store.for_prompt(limit=3)) == 3


def test_brief_crud(tmp_path):
    store = MemoryStore(tmp_path / "db" / "memory.db")
    stored = store.add_brief("2026-10-26", "Morning.", model="qwen3:8b")
    assert stored["date"] == "2026-10-26"
    assert stored["model"] == "qwen3:8b"
    saved = store.brief_for_date("2026-10-26")
    assert saved is not None
    assert saved["text"] == "Morning."
    store.add_brief("2026-10-26", "Second same-day brief.", model="qwen3:8b")
    assert store.list_briefs(date="2026-10-26")[0]["text"] == "Second same-day brief."
    assert len(store.list_briefs()) == 2
    # deleting the first leaves the second for the same date
    assert store.delete_brief(stored["id"]) is True
    remaining = store.brief_for_date("2026-10-26")
    assert remaining is not None and remaining["text"] == "Second same-day brief."
    assert store.delete_brief(remaining["id"]) is True
    assert store.brief_for_date("2026-10-26") is None


# --------------------------------------------------------------------------
# Conversation summarization
# --------------------------------------------------------------------------


def test_summarize_writes_candidate_facts(tmp_path):
    store = MemoryStore(tmp_path / "db" / "memory.db")
    runtime = StubRuntime(json.dumps([
        {"text": "the client prefers weekly calls"},
        {"text": "the renewal date is November 30"},
    ]))
    messages = [
        {"role": "user", "content": "remind me we call the client weekly"},
        {"role": "assistant", "content": "noted, weekly calls it is"},
    ]
    stored = summarize_conversation(messages, runtime, store=store,
                                    model="qwen3:8b", max_facts=4)
    assert len(stored) == 2
    assert all(f["source"] == "conversation" for f in stored)
    assert all(f["confidence"] < 1.0 for f in stored)
    assert all(f["user_edited"] is False for f in stored)
    # the runtime saw the transcript and a model id
    prompt, model = runtime.calls[0]
    assert model == "qwen3:8b"
    joined = " ".join(m["content"] for m in prompt)
    assert "weekly calls" in joined

    # stored rows are the same facts
    assert {f["text"] for f in store.list_facts(source="conversation")} == {
        "the client prefers weekly calls", "the renewal date is November 30"}


def test_summarize_parses_json_embedded_in_prose(tmp_path):
    store = MemoryStore(tmp_path / "db" / "memory.db")
    reply = 'Here you go:\n[{"text": "first"}, {"text": "second"}]\nthanks'
    stored = summarize_conversation([{"role": "user", "content": "hi"}],
                                    StubRuntime(reply), store=store)
    assert [f["text"] for f in stored] == ["first", "second"]


def test_summarize_skips_junk_and_dedupes(tmp_path):
    store = MemoryStore(tmp_path / "db" / "memory.db")
    reply = json.dumps([
        {"text": "dupe"}, {"text": "dupe"}, {"text": " "}, {"nottext": "x"}, 42,
    ])
    stored = summarize_conversation([{"role": "user", "content": "hi"}],
                                    StubRuntime(reply), store=store)
    assert [f["text"] for f in stored] == ["dupe"]


def test_summarize_empty_reply_no_crash(tmp_path):
    store = MemoryStore(tmp_path / "db" / "memory.db")
    stored = summarize_conversation([], StubRuntime(""), store=store)
    assert stored == []


# --------------------------------------------------------------------------
# ICS calendar
# --------------------------------------------------------------------------

ICS = "\r\n".join([
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "BEGIN:VEVENT",
    "DTSTART;VALUE=DATE:20261026",
    "SUMMARY;LANGUAGE=en-US:All hands",
    "END:VEVENT",
    "BEGIN:VEVENT",
    "DTSTART;TZID=America/Los_Angeles:20261026T090000",
    "SUMMARY:Standup",
    "LOCATION:Room 1",
    "END:VEVENT",
    "BEGIN:VEVENT",
    "DTSTART:20261026T160000Z",
    "SUMMARY:Ship",
    "END:VEVENT",
    "BEGIN:VEVENT",
    "DTSTART:20261026T080000",
    "SUMMARY:Long title continued ",
    " on the folded line",
    "END:VEVENT",
    "END:VCALENDAR",
    "",
])


def test_parse_ics_all_day_datetime_utc_tzid_and_folding():
    events = parse_ics(ICS)
    assert len(events) == 4
    by_summary = {e.summary: e for e in events}

    all_day = by_summary["All hands"]
    assert all_day.is_all_day is True
    assert all_day.day == date(2026, 10, 26)

    local = by_summary["Standup"]
    assert isinstance(local.dtstart, datetime)
    assert local.dtstart.tzinfo is None  # TZID kept but not resolved
    assert local.tzid == "America/Los_Angeles"
    assert local.location == "Room 1"

    utc = by_summary["Ship"]
    assert isinstance(utc.dtstart, datetime)
    assert utc.dtstart.tzinfo is not None

    folded = by_summary["Long title continued on the folded line"]
    assert isinstance(folded.dtstart, datetime)
    assert folded.dtstart.hour == 8


def test_events_on_filters_by_calendar_date():
    events = parse_ics(ICS)
    today = [e for e in events_on(events, date(2026, 10, 26))]
    assert {e.summary for e in today} == {
        "All hands", "Standup", "Ship",
        "Long title continued on the folded line"}
    # nothing on another day
    assert events_on(events, date(2026, 10, 27)) == []


def test_load_calendar_events_from_dict_and_file(tmp_path):
    ics_file = tmp_path / "cal.ics"
    ics_file.write_text(ICS, encoding="utf-8")
    events, errors = load_calendar_events(
        {"ics_files": [str(ics_file)]}, root=tmp_path)
    assert len(events) == 4
    assert errors == []

    # missing file is reported, not raised
    events, errors = load_calendar_events({"ics_files": [str(tmp_path / "nope.ics")]})
    assert events == []
    assert len(errors) == 1


def test_calendar_json_picked_up_from_data_root(tmp_path):
    (tmp_path / "calendar.json").write_text(
        json.dumps({"ics_files": ["%s/cal.ics" % tmp_path]}), encoding="utf-8")
    (tmp_path / "cal.ics").write_text(ICS, encoding="utf-8")
    events, errors = load_calendar_events(root=tmp_path)
    assert len(events) == 4
    assert errors == []


def test_no_calendar_configured_is_empty_not_error(tmp_path):
    events, errors = load_calendar_events(root=tmp_path)  # no calendar.json
    assert events == []
    assert errors == []


# --------------------------------------------------------------------------
# Brief prompt assembly and generation
# --------------------------------------------------------------------------


def test_assemble_brief_prompt_sections_and_no_em_dash():
    day = date(2026, 10, 26)
    messages = assemble_brief_prompt(
        day, "today",
        events=[CalendarEvent("Standup", datetime(2026, 10, 26, 9, 0)),
                CalendarEvent("All hands", date(2026, 10, 26))],
        hits=[{"score": 0.9, "path": "/vault/a.md", "snippet": "payments due Monday"}],
        facts=[{"text": "backup runs nightly", "user_edited": True}],
        instruction="Write a morning brief.",
    )
    assert messages[0]["role"] == "system"
    user = messages[1]["content"]
    assert "calendar" in user.lower()
    assert "All hands (all day)" in user
    assert "Standup at 09:00" in user
    assert "payments due Monday" in user
    assert "(confirmed) backup runs nightly" in user
    assert "2026-10-26" in user
    # absolute rule: no em dash anywhere in the assembled prompt
    assert "\u2014" not in user and "\u2013" not in user


def test_build_brief_with_stub_runtime_stores_brief(tmp_path):
    home = tmp_path / "home"
    cfg = make_cfg(home)
    store = MemoryStore(home / "db" / "memory.db")
    confirmed = store.add_candidate("the renewal is due November 30.",
                                    source="conversation")
    store.set_user_edited(confirmed["id"])
    store.add_candidate("client prefers weekly calls.", source="conversation")
    index = FakeIndex([{"score": 0.9, "path": "/vault/notes.md",
                        "snippet": "payments are due Monday"}])
    runtime = StubRuntime("Good morning. One payment is due Monday.")

    result = build_brief("2026-10-26", "today", cfg=cfg, store=store,
                         index=index, runtime=runtime, model="qwen3:8b")
    assert result["date"] == "2026-10-26"
    assert result["model"] == "qwen3:8b"
    assert result["text"] == "Good morning. One payment is due Monday."
    assert result["n_hits"] == 1
    assert result["n_facts"] == 2
    assert result["brief_id"] > 0

    # persisted and readable back
    saved = store.brief_for_date("2026-10-26")
    assert saved is not None
    assert saved["text"] == result["text"]
    assert saved["model"] == "qwen3:8b"

    # the prompt carries the sections and marks the confirmed fact
    prompt, prompt_model = runtime.calls[0]
    assert prompt_model == "qwen3:8b"
    joined = prompt[1]["content"]
    assert "renewal is due November 30" in joined
    assert "client prefers weekly calls" in joined
    assert "payments are due Monday" in joined
    assert "\u2014" not in joined

    # the query asks about what is due or changed and includes the date
    assert any("due or changed" in q for q in index.queries)
    assert "2026-10-26" in index.queries
    # the date query is not duplicated
    assert index.queries.count("2026-10-26") == 1


def test_build_brief_with_fake_server_through_runtime_seam(tmp_path):
    """End to end through the real Runtime transport against the fake server."""
    port = free_port()
    server = fake.run_server(port)
    try:
        home = tmp_path / "home"
        cfg = make_cfg(home, STEWARD_OLLAMA_URL=f"http://127.0.0.1:{port}",
                       STEWARD_MODEL="qwen3:8b")
        store = MemoryStore(home / "db" / "memory.db")
        runtime = SystemOllama(url=f"http://127.0.0.1:{port}", model="qwen3:8b")
        result = build_brief("2026-10-26", "today", cfg=cfg, store=store,
                             index=FakeIndex(), runtime=runtime)
        assert result["text"] == "".join(fake.REPLY_TOKENS)
        assert result["model"] == "qwen3:8b"
    finally:
        server.shutdown()
        server.server_close()


def test_build_brief_with_nothing_configured_still_works(tmp_path):
    home = tmp_path / "home"
    cfg = make_cfg(home)
    store = MemoryStore(home / "db" / "memory.db")
    result = build_brief(date(2026, 10, 26), "today", cfg=cfg, store=store,
                         index=FakeIndex(), runtime=StubRuntime("Nothing due."))
    assert result["n_events"] == 0
    assert result["n_hits"] == 0
    assert result["n_facts"] == 0
    assert result["text"] == "Nothing due."
    assert "\u2014" not in result["text"]


def test_build_brief_broken_index_contributes_no_hits(tmp_path):
    class BrokenIndex:
        def search(self, query, k=8):
            raise RuntimeError("index corrupt")

    home = tmp_path / "home"
    cfg = make_cfg(home)
    result = build_brief("2026-10-26", "today", cfg=cfg,
                         store=MemoryStore(home / "db" / "memory.db"),
                         index=BrokenIndex(),
                         runtime=StubRuntime("fine without hits."))
    assert result["n_hits"] == 0
    assert result["text"] == "fine without hits."


def test_build_brief_empty_model_reply_raises(tmp_path):
    home = tmp_path / "home"
    cfg = make_cfg(home)
    with pytest.raises(RuntimeError, match="empty text"):
        build_brief("2026-10-26", "today", cfg=cfg,
                    store=MemoryStore(home / "db" / "memory.db"),
                    index=FakeIndex(), runtime=StubRuntime("   "))
