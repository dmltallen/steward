"""Brief generation: turn today into a short plain-language brief.

build_brief(date, scope) collects three inputs and asks the runtime to write a
short brief from them:

    calendar    today's events from configured ICS sources (memory/ics.py);
                an empty calendar is reported honestly, never fabricated
    retrieval   index hits for "what is due or changed" plus the scope
                (the S7 RetrievalIndex at <data>/db/index.db, when it has
                documents; an empty index simply contributes no hits)
    facts       open memory facts, user-edited first (memory/store.py)

The prompt assembly is pure and unit-testable (assemble_brief_prompt); the
model head is the runtime seam (Runtime.chat, stream=False). The finished
brief is stored in the briefs table with the model that produced it, so a
later reader knows which engine wrote it.

No fallback text is fabricated when the model is unreachable: build_brief
raises and the scheduler records the error, rather than inventing a brief.
"""

from __future__ import annotations

from datetime import date as Date, datetime
from pathlib import Path

from steward.paths import data_root
from steward.runtime import make_runtime

from . import ics as ics_mod
from .store import MemoryStore

DEFAULT_MODEL = "qwen3:8b"
DEFAULT_HIT_LIMIT = 6

DEFAULT_INSTRUCTION = (
    "Write a short morning brief in plain English. Lead with anything due today "
    "or changed since yesterday, then open items from my memory, then one line "
    "of outlook. Use short sentences. Never use em dashes; use a period or a "
    "hyphen instead."
)


def _fmt_date(day: Date) -> str:
    return day.isoformat()


def _retrieval_queries(scope: str) -> list[str]:
    scope = (scope or "").strip()
    queries = ["what is due or changed"]
    if scope and scope not in ("today", "daily", "all"):
        queries.append(scope)
    return queries


def assemble_brief_prompt(day: Date, scope: str, *,
                          events: list | None = None,
                          hits: list | None = None,
                          facts: list | None = None,
                          instruction: str | None = None,
                          now: str | None = None) -> list[dict]:
    """Build the system and user messages for a brief. Pure and deterministic.

    All inputs are already materialized (CalendarEvents, hit dicts, fact
    dicts), so callers can test assembly with retrieval and calendar stubs
    without a model.
    """
    instruction = (instruction or DEFAULT_INSTRUCTION).strip()
    events = list(events or [])
    hits = list(hits or [])
    facts = list(facts or [])
    now = now or datetime.now().astimezone().isoformat(timespec="minutes")

    sections: list[str] = ["Today is %s (%s). Scope: %s."
                           % (day.isoformat(), day.strftime("%A"), scope or "today")]

    if events:
        lines = []
        for event in events:
            when = ""
            if isinstance(event.dtstart, datetime):
                when = event.dtstart.strftime(" at %H:%M")
            elif event.is_all_day:
                when = " (all day)"
            title = event.summary
            if event.location:
                title += " (%s)" % event.location
            lines.append("- %s%s" % (title, when))
        sections.append("## Today's calendar\n" + "\n".join(lines))
    else:
        sections.append("## Today's calendar\nNone scheduled or no calendar configured.")

    if hits:
        lines = []
        for hit in hits:
            lines.append("- %.3f %s: %s"
                         % (float(hit.get("score", 0.0)),
                            hit.get("path", "?"),
                            hit.get("snippet", "")))
        sections.append("## Due or changed (from your notes)\n" + "\n".join(lines))
    else:
        sections.append("## Due or changed (from your notes)\nNo matching notes indexed.")

    if facts:
        lines = []
        for fact in facts:
            mark = "(confirmed) " if fact.get("user_edited") else ""
            lines.append("- %s%s" % (mark, fact.get("text", "")))
        sections.append("## Open memory facts\n" + "\n".join(lines))
    else:
        sections.append("## Open memory facts\nNone stored yet.")

    sections.append(
        "Write the brief now. It will be shown at the top of the day. "
        "Never use em dashes. Do not invent any fact that is not in the inputs."
    )

    system = (
        "You are Steward, a local personal assistant. You write short, accurate, "
        "plain-language briefs from the given inputs. You never invent numbers, "
        "dates, or facts. You never use em dashes. Generated at %s."
        % now
    )
    user = instruction + "\n\n" + "\n\n".join(sections)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _merge_hits(raw_hits: list[list[dict]], limit: int = DEFAULT_HIT_LIMIT) -> list[dict]:
    """Dedupe merged retrieval hits by chunk id, keep the highest score."""
    by_id: dict = {}
    for group in raw_hits:
        for hit in group:
            cid = hit.get("chunk_id")
            key = cid or (hit.get("path"), hit.get("offset"))
            prev = by_id.get(key)
            if prev is None or float(hit.get("score", 0.0)) > float(prev.get("score", 0.0)):
                by_id[key] = hit
    merged = sorted(by_id.values(), key=lambda h: -float(h.get("score", 0.0)))
    return merged[:limit]


def build_brief(date=None, scope: str = "today", *,
                cfg=None, store: MemoryStore | None = None,
                index=None, runtime=None, model: str | None = None,
                instruction: str | None = None) -> dict:
    """Generate one brief for a date and persist it.

    date: a date, a 'YYYY-MM-DD' string, or None for local today.
    cfg: a steward.config.Config for data dir and model (optional; defaults
         loaded). store/index/runtime may be injected for tests; when omitted,
         store is the real MemoryStore, index is the real RetrievalIndex, and
         runtime is make_runtime(cfg).

    Returns a dict with date, text, model, message_count, brief_id plus the
    input summary (n_events, n_hits, n_facts). Raises when the backend is
    unreachable; the scheduler catches that and records it.
    """
    from steward.config import Config

    cfg = cfg if cfg is not None else Config.load()
    data_dir = cfg.data_dir or str(data_root())
    if isinstance(date, str):
        day = Date.fromisoformat(date)
    elif isinstance(date, datetime):
        day = date.date()
    elif isinstance(date, Date):
        day = date
    else:
        day = datetime.now().astimezone().date()

    store = store if store is not None else MemoryStore(Path(data_dir) / "db" / "memory.db")
    runtime = runtime if runtime is not None else make_runtime(cfg)
    model = model or cfg.model or DEFAULT_MODEL

    raw_events, calendar_errors = ics_mod.load_calendar_events(root=data_dir)
    events = ics_mod.events_on(raw_events, day)

    if index is None:
        index = _default_index(data_dir)
    raw_hits = []
    for query in _retrieval_queries(scope):
        try:
            raw_hits.append(index.search(query, k=4))
        except Exception:  # noqa: BLE001 - a broken index contributes no hits
            raw_hits.append([])
    hits = _merge_hits(raw_hits)

    facts = store.for_prompt(limit=12)

    messages = assemble_brief_prompt(
        day, scope, events=events, hits=hits, facts=facts,
        instruction=instruction,
    )
    text = "".join(d["delta"] for d in runtime.chat(messages, stream=False, model=model))
    text = (text or "").strip()
    if not text:
        raise RuntimeError("brief generation returned empty text")

    stored = store.add_brief(_fmt_date(day), text, model)
    return {
        "brief_id": stored["id"],
        "date": stored["date"],
        "text": stored["text"],
        "model": stored["model"],
        "created_at": stored["created_at"],
        "n_events": len(events),
        "n_hits": len(hits),
        "n_facts": len(facts),
        "calendar_errors": calendar_errors,
    }


def _default_index(data_dir: str):
    from steward.retrieval import RetrievalIndex

    return RetrievalIndex(data_root_path=data_dir)
