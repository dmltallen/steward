"""Memory: facts store, briefs, and the scheduler's sense of time.

    store    SQLite facts (with provenance and user-edited promotion) and
             briefs, plus conversation summarization into candidates
    ics      minimal RFC 5545 reader for the brief's calendar section
    brief    build_brief: calendar + retrieval + facts through the runtime

The scheduler (src/steward/scheduler.py) drives a morning-brief default job
daily at 07:00 when the box is awake.
"""

from steward.memory.brief import (
    DEFAULT_INSTRUCTION,
    assemble_brief_prompt,
    build_brief,
)
from steward.memory.ics import (
    CalendarEvent,
    events_on,
    load_calendar_events,
    parse_ics,
)
from steward.memory.store import (
    MemoryStore,
    summarize_conversation,
)

__all__ = [
    "CalendarEvent",
    "DEFAULT_INSTRUCTION",
    "MemoryStore",
    "assemble_brief_prompt",
    "build_brief",
    "events_on",
    "load_calendar_events",
    "parse_ics",
    "summarize_conversation",
]
