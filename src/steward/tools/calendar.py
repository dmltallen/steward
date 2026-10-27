"""calendar tools: read and write ICS calendars inside the configured roots.

Same sandbox as the fs tools (containment, symlink escape and sensitive-name
refusal) plus a content rule: calendar.write only accepts text that actually
looks like an iCalendar feed (both VCALENDAR markers present), and
calendar.read refuses files that are not calendars. calendar.write is a write
risk, so it never runs before a human approves it.

Risk: read for calendar.read, write for calendar.write.
"""

from __future__ import annotations

from steward.tools.sandbox import ToolError, resolve_within_roots

_ICS_BEGIN = "BEGIN:VCALENDAR"
_ICS_END = "END:VCALENDAR"


def _require_ics(content: str) -> None:
    if _ICS_BEGIN not in content or _ICS_END not in content:
        raise ToolError("content is not a valid ICS calendar (missing "
                        "%s / %s markers)" % (_ICS_BEGIN, _ICS_END))


def _read_text(path) -> str:
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _write_text(path, content: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)


def calendar_read(settings, args: dict) -> dict:
    resolved, root = resolve_within_roots(args["path"], settings.roots())
    if not resolved.is_file():
        raise ToolError("not a readable file: %s" % resolved)
    size = resolved.stat().st_size
    if size > settings.max_file_bytes:
        raise ToolError("file too large to read (%d bytes, cap %d)"
                        % (size, settings.max_file_bytes))
    text = _read_text(resolved)
    _require_ics(text)
    return {"path": str(resolved), "root": str(root),
            "bytes": len(text.encode("utf-8")), "format": "ics", "content": text}


def calendar_write(settings, args: dict) -> dict:
    resolved, root = resolve_within_roots(args["path"], settings.roots())
    content = str(args.get("content", ""))
    _require_ics(content)
    resolved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_text(resolved, content)
    return {"path": str(resolved), "root": str(root),
            "bytes": len(content.encode("utf-8")), "format": "ics", "written": True}
