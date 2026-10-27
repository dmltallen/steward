"""Minimal ICS (RFC 5545) calendar reading for the morning brief.

Steward does not ship a full iCal library; this module parses just enough to
answer one question honestly: what is on today's calendar? It reads VEVENT
blocks, unfolds folded lines, and extracts DTSTART and SUMMARY. DTSTART with a
DATE value becomes an all-day event; a DATE-TIME value keeps its wall-clock
time, with a trailing Z treated as UTC. TZID parameters are recorded on the
event but not resolved to an instant, so a TZID event is compared by its
wall-clock date (documented, not hidden).

Sources: a local .ics file, or an http(s) .ics URL. Calendar sources come from
<data>/calendar.json:

    {"ics_files": ["/home/user/calendar.ics"], "ics_urls": ["https://..."], "open_timeout_s": 8}

They can also come from cfg.ical_urls / cfg.ical_files when a Config object
carries them (config.py is not owned by this card, so the file is the primary
channel). Nothing configured means an empty calendar, which the brief reports
honestly, never an exception.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from steward.paths import data_root

DEFAULT_TIMEOUT_S = 8.0


@dataclass
class CalendarEvent:
    summary: str
    dtstart: datetime | date
    tzid: str | None = None
    location: str = ""

    @property
    def is_all_day(self) -> bool:
        return isinstance(self.dtstart, date) and not isinstance(self.dtstart, datetime)

    @property
    def day(self) -> date:
        if isinstance(self.dtstart, datetime):
            return self.dtstart.date()
        return self.dtstart


def _unfold(text: str) -> list[str]:
    """Join CRLF continuation lines (start with space or tab) into one line."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _parse_dt(value: str, tzid: str | None) -> datetime | date:
    """Parse an ICS date or date-time value into datetime or date."""
    value = value.strip()
    if len(value) == 8 and value.isdigit():
        return date(int(value[0:4]), int(value[4:6]), int(value[6:8]))
    if len(value) >= 15 and value[8:9] == "T" and _digit_dt_body(value):
        # YYYYMMDDTHHMMSS, optional trailing Z for UTC.
        utc = value.endswith("Z")
        base = value[:15]
        dt = datetime(
            int(base[0:4]), int(base[4:6]), int(base[6:8]),
            int(base[9:11]), int(base[12:14]), int(base[14:16]),
        )
        if utc and tzid is None:
            # Aware UTC datetime so the day is correct regardless of local tz.
            from datetime import timezone

            return dt.replace(tzinfo=timezone.utc)
        return dt
    raise ValueError("unrecognized DTSTART value: %r" % value)


def _digit_dt_body(value: str) -> bool:
    """True when value is YYYYMMDDTHHMMSS with a single trailing Z allowed."""
    body = value[:-1] if value.endswith("Z") else value
    return len(body) == 15 and body[0:8].isdigit() and body[9:15].isdigit()


def _param_value(name: str, key: str) -> str | None:
    """Read a parameter value from a property header, e.g. TZID out of
    'DTSTART;TZID=America/Los_Angeles'. Returns None when absent."""
    for token in name.split(";"):
        if token.upper().startswith(key.upper() + "="):
            return token.split("=", 1)[1].strip('"')
    return None


def parse_ics(text: str) -> list[CalendarEvent]:
    """Parse VEVENT blocks from an ICS document into CalendarEvents."""
    events: list[CalendarEvent] = []
    current: dict = {}
    inside_vevent = False
    for line in _unfold(text):
        name, _, payload = line.partition(":")
        prop = name.split(";", 1)[0].upper()
        if prop == "BEGIN":
            if payload.strip().upper() == "VEVENT":
                current = {}
                inside_vevent = True
            continue
        if prop == "END":
            if payload.strip().upper() == "VEVENT" and inside_vevent:
                event = _build_event(current)
                if event is not None:
                    events.append(event)
                inside_vevent = False
            continue
        if not inside_vevent:
            continue
        if prop in ("DTSTART", "SUMMARY", "LOCATION", "DTSTAMP", "UID"):
            current[prop] = payload
            tzid = _param_value(name, "TZID")
            if tzid:
                current[prop + "_TZID"] = tzid
    return events


def _build_event(fields: dict) -> CalendarEvent | None:
    if "SUMMARY" not in fields:
        return None
    summary = fields["SUMMARY"].strip()
    if not summary:
        return None
    if "DTSTART" not in fields:
        return None
    dtstart = _parse_dt(fields["DTSTART"], fields.get("DTSTART_TZID"))
    return CalendarEvent(
        summary=summary,
        dtstart=dtstart,
        tzid=fields.get("DTSTART_TZID"),
        location=fields.get("LOCATION", "").strip(),
    )


def events_on(events: list[CalendarEvent], day: date) -> list[CalendarEvent]:
    """Events whose start falls on the given calendar date, start-sorted."""
    seen = [e for e in events if _event_falls_on(e, day)]
    seen.sort(key=lambda e: (_sort_key(e), e.summary))
    return seen


def _sort_key(event: CalendarEvent):
    """Sort key normalized to naive datetime: UTC becomes wall-clock local."""
    if isinstance(event.dtstart, datetime):
        dt = event.dtstart
        dt = dt.astimezone() if dt.tzinfo is not None else dt
        return dt.replace(tzinfo=None)
    d = event.dtstart
    return datetime(d.year, d.month, d.day)


def _event_falls_on(event: CalendarEvent, day: date) -> bool:
    start = event.dtstart
    if isinstance(start, datetime):
        if start.tzinfo is not None:
            return start.astimezone().date() == day
        return start.date() == day
    return start == day  # all-day (date-only) events match their start day


def load_calendar_events(calendar_config=None, *, timeout: float = DEFAULT_TIMEOUT_S,
                         root: str | Path | None = None):
    """Load events from the configured sources.

    calendar_config: a dict with 'ics_files' and/or 'ics_urls' lists, or None
    to auto-load <data>/calendar.json under `root` (default: the data root).
    A file that does not exist, a URL that fails, or an unparsable feed is
    skipped and reported rather than raised, so a broken calendar source never
    stops the brief.
    """
    sources = _resolve_sources(calendar_config, root=root)
    errors: list[str] = []
    events: list[CalendarEvent] = []
    for path in sources.get("ics_files", []):
        fp = Path(path).expanduser()
        try:
            events.extend(parse_ics(fp.read_text(encoding="utf-8")))
        except OSError as exc:
            errors.append("file %s: %s" % (path, exc))
        except ValueError as exc:
            errors.append("file %s: %s" % (path, exc))
    for url in sources.get("ics_urls", []):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                events.extend(parse_ics(resp.read().decode("utf-8", errors="replace")))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            errors.append("url %s: %s" % (url, exc))
    return events, errors


def _resolve_sources(calendar_config, *, root: str | Path | None = None) -> dict:
    if isinstance(calendar_config, dict) and calendar_config:
        return {
            "ics_files": list(calendar_config.get("ics_files") or []),
            "ics_urls": list(calendar_config.get("ics_urls") or []),
        }
    path = (Path(root) if root is not None else data_root()) / "calendar.json"
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return {
                "ics_files": list(data.get("ics_files") or []),
                "ics_urls": list(data.get("ics_urls") or []),
            }
    except (OSError, json.JSONDecodeError):
        pass
    return {"ics_files": [], "ics_urls": []}
