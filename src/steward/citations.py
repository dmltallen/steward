"""Citation layer for retrieval-grounded chat answers.

Bridges two surfaces in Steward: the retrieval index
(steward.retrieval.index.RetrievalIndex.search) that returns scored hits over
the user's own files, and the chat reply that should not read like a guess.
This module turns hits into a numbered context, builds the exact prompt that
asks the model to answer only from those numbered sources and cite each claim
as [n], then resolves the [n] markers a reply actually used back to source
path, line range and snippet. The web and phone clients render that resolved
list as plain text or markdown.

Guarantees:

  * select_context always keeps the top hit (even when it alone exceeds the
    budget), never exceeds the budget once at least one hit is in, and numbers
    what it keeps 1..n in best-first order.
  * build_prompt with no hits returns an instruction to answer honestly that
    nothing in the user's files covers the question; it must not invent.
  * extract_citations only resolves markers that point inside 1..len(selected).
    A marker outside that range cannot resolve to a source, is never reported
    as a citation, and is returned under "dropped".

Import surface: stdlib only (re and pathlib). No third-party dependencies, no
network, no model. The module never claims a citation that was not in the
provided context.

The index keeps its behavior and on-disk format unchanged; it already returns
everything the resolver needs (path, offset, text, snippet, score).
"""

from __future__ import annotations

import re
from pathlib import Path

# A marker is [n] with n a non-negative integer. Anything else in a reply is
# left alone; only this shape resolves to a numbered source.
MARKER_RE = re.compile(r"\[(\d+)\]")

NO_HITS_INSTRUCTION = (
    "A search of the user's own files returned no passage that covers this "
    "question. Answer that nothing in the files covers it. Do not invent an "
    "answer and do not use outside knowledge. Do not use source markers."
)


def _copy_hit(hit: dict, number: int) -> dict:
    """A shallow copy of a hit with the stable citation number attached."""
    copy = dict(hit)
    copy["number"] = number
    return copy


def select_context(hits: list, budget_chars: int) -> list[dict]:
    """Pick the retrieval hits that fit a char budget, best first, numbered 1..n.

    Hits are ranked by descending score and taken greedily while the summed
    length of their ``text`` fits within ``budget_chars``. The best hit is
    always included even when it alone overflows the budget, so a non-empty hit
    list never yields an empty context. Ties keep the order they arrived in, so
    identical inputs always produce the same numbering.

    The returned hits are copies with an extra ``number`` key; the input list
    and its dicts are never mutated.
    """
    if not hits:
        return []
    budget = max(0, int(budget_chars))
    ranked = sorted(hits, key=lambda h: -float(h.get("score", 0.0)))
    selected: list[dict] = []
    used = 0
    for hit in ranked:
        size = len(hit.get("text", ""))
        if selected and used + size > budget:
            break
        selected.append(_copy_hit(hit, len(selected) + 1))
        used += size
    return selected


def build_prompt(question: str, selected: list) -> str:
    """The exact prompt text for answering only from the numbered sources.

    With a non-empty ``selected`` the prompt lists each source as
    ``[n] <path>`` followed by its literal text and asks the model to answer
    only from those sources, cite every claim as [n], and say plainly when a
    part of the question is not covered.

    With an empty ``selected`` (no hits) the prompt is the honest no-hits
    instruction: nothing in the files covers the question, do not invent an
    answer, do not use outside knowledge, do not use source markers.
    """
    question = (question or "").strip()
    if not selected:
        return NO_HITS_INSTRUCTION + "\n\nQuestion: " + question
    blocks = []
    for index, hit in enumerate(selected):
        number = hit.get("number")
        if number is None:
            number = index + 1
        new_path = str(hit.get("path", "<unknown>"))
        body = (hit.get("text", "") or "").rstrip("\n")
        blocks.append("[%d] %s\n%s" % (number, new_path, body))
    return (
        "Answer the question using ONLY the numbered sources below. Base every "
        "claim on a source and cite it as [n] where n is the source number. If "
        "part of the question is not covered by the sources, say so plainly; "
        "never invent an answer. Do not use knowledge outside these sources.\n\n"
        "Sources:\n" + "\n\n".join(blocks) + "\n\nQuestion: " + question
    )


def line_range(source: str, offset: int, length: int) -> tuple[int, int]:
    """1-based inclusive (start, end) lines of source[offset:offset + length].

    A character at position p sits on line 1 + (number of newline characters
    strictly before p). A trailing newline terminates the line it closes, so a
    chunk that ends in a newline does not spill into the next line. Returns
    (0, 0) for an empty length.
    """
    if not length:
        return (0, 0)
    start = source.count("\n", 0, max(offset, 0)) + 1
    last = max(offset, 0) + length - 1
    end = source.count("\n", 0, last) + 1
    return (start, end)


def _read_source(path: str) -> str | None:
    """The file's text as the index would see it, or None when unreadable."""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _one_line(text: str | None) -> str:
    """Collapse whitespace so a snippet never spans rendered lines."""
    return " ".join((text or "").split())


def _resolve(hit: dict, number: int, count: int) -> dict:
    """Build the resolvable citation entry for one marker inside the context."""
    path = str(hit.get("path", ""))
    offset = hit.get("offset")
    text = hit.get("text", "")
    line_start = None
    line_end = None
    if path and offset is not None and text:
        source = _read_source(path)
        if source is not None:
            line_start, line_end = line_range(source, int(offset), len(text))
    snippet = hit.get("snippet")
    if not snippet:
        snippet = _one_line(text)
    return {
        "number": number,
        "path": path,
        "line_start": line_start,
        "line_end": line_end,
        "offset": offset,
        "snippet": snippet,
        "text": text,
        "count": count,
    }


def extract_citations(reply: str, selected: list) -> dict:
    """Resolve the [n] markers a reply actually used back to its sources.

    Returns ``{"used": [...], "dropped": [...]}``:

      * ``used`` is ordered by marker number and de-duplicated: a marker that
        appears twice resolves to one entry whose ``count`` is its occurrences.
        Each entry carries number, path, line_start, line_end, offset, snippet,
        text and count.
      * ``dropped`` is the sorted list of marker numbers that fell outside
        1..len(selected). They are not resolvable to any provided source and
        are never reported as citations.

    A marker can only resolve when it points inside 1..len(selected), so the
    module never claims a citation that was not in the provided context. Line
    numbers are computed from the current file on disk (utf-8 lossy, the same
    decode the index uses). When the file cannot be read, line_start and
    line_end are None and rendering falls back to the char offset.
    """
    if not selected:
        return {"used": [], "dropped": []}
    counts: dict[int, int] = {}
    for match in MARKER_RE.finditer(reply or ""):
        number = int(match.group(1))
        counts[number] = counts.get(number, 0) + 1
    used: list[dict] = []
    dropped: list[int] = []
    for number in sorted(counts):
        if 1 <= number <= len(selected):
            used.append(_resolve(selected[number - 1], number, counts[number]))
        else:
            dropped.append(number)
    return {"used": used, "dropped": dropped}


def _location(cite: dict) -> str:
    """The human-readable location part of a rendered source line."""
    start = cite.get("line_start")
    end = cite.get("line_end")
    if start is not None and end is not None:
        if start == end:
            return "line %d" % start
        return "lines %d-%d" % (start, end)
    offset = cite.get("offset")
    if offset is not None:
        return "at char %d" % offset
    return ""


def render_sources(used: list) -> str:
    """Plain-text/markdown source list to append to a reply.

    One line per cited source: ``[n] <path> <line range>: <one-line snippet>``,
    prefixed with a ``Sources:`` header. Rendering is markdown-safe plain text
    that the web and phone clients can show as-is. An empty ``used`` renders to
    an empty string: when the reply cited nothing, there is nothing to list.
    """
    if not used:
        return ""
    lines = ["Sources:"]
    for index, cite in enumerate(used):
        number = cite.get("number", index + 1)
        path = str(cite.get("path", "<unknown>"))
        location = _location(cite)
        snippet = _one_line(cite.get("snippet")) or _one_line(cite.get("text"))
        prefix = "[%d] %s" % (number, path)
        if location:
            prefix += " " + location
        lines.append("%s: %s" % (prefix, snippet))
    return "\n".join(lines)
