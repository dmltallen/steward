"""Agent loop: model -> tool -> model, capped at four tool hops.

run_loop() drives a chat session through the runtime seam. Each hop asks the
model. When the model replies with exactly one JSON object of the shape

    {"tool": "fs.list", "args": {"path": "/home"}}

the call is validated against the registry schema, gated by the approval flow
when it is write/exec/net, and the structured result is injected back as a
role="tool" message; then the model is asked again with that result in
context. The tool-call chain is capped at max_hops executions (default 4); a
further tool request at the cap is refused and the final model text returned.
The loop returns only final text, never intermediate state.

Streaming: pass emit= to receive each model delta as {"delta": "..."} plus a
per-step {"event": "tool_call", ...}, {"event": "approval_required", ...} and
{"event": "tool_result", ...} dict (the daemon's SSE contract). Without emit=
the loop keeps the original behavior: model calls are non-streamed and only
final text is returned. Model deltas that form a tool-call reply are buffered
and replaced by the tool_call event, so no raw JSON blob ever reaches the
stream.

The model is told the protocol through tool_system_prompt(); callers may pass
their own system_prompt to run_loop instead.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Callable

from steward.tools.registry import RISK_READ, execute_tool

HOP_CAP = 4

_TOOL_CALL_HINT = (
    "When you need a tool, reply with a single JSON object and nothing else, "
    'exactly like this: {"tool": "fs.list", "args": {"path": "/home"}}'
)


def extract_tool_call(text: str | None) -> tuple[str, dict] | None:
    """Pull {"tool", "args"} out of a model reply, or None when it is prose.

    Only a reply that is entirely one JSON object counts as a tool request, so
    a sentence that merely mentions a tool is never executed. A single fenced
    code block around the object is tolerated.
    """
    t = (text or "").strip()
    if t.startswith("```"):
        _, sep, rest = t.partition("\n")
        if not sep:
            return None  # single-line fences are not a valid tool request
        rest = rest.rstrip()
        if rest.endswith("```"):
            rest = rest[:-3]
        t = rest.strip()
        if t.lower().startswith("json"):
            t = t[4:].strip()
    if not (t.startswith("{") and t.endswith("}")):
        return None
    try:
        data = json.loads(t)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("tool"), str) or not isinstance(data.get("args"), dict):
        return None
    return data["tool"], dict(data["args"])


def tool_system_prompt(registry: dict) -> str:
    """The protocol + catalog shown to the model before any session."""
    lines = [
        "You are Steward, a local personal assistant.",
        "You may call the tools below to act on the user's machine.",
        _TOOL_CALL_HINT,
        "",
        "Tools (name, risk, description):",
    ]
    for spec in registry.values():
        lines.append("- %s (%s): %s" % (spec.name, spec.risk, spec.description))
    lines.append("")
    lines.append(
        "Rules: reply with the tool JSON object alone when you need a tool, "
        "otherwise reply in plain text. Write, exec and net calls wait for a "
        "human to approve them; a denied or timed-out tool returns ok=false "
        "and you must continue without it."
    )
    return "\n".join(lines)


def run_loop(runtime, registry: dict, settings, approvals, messages: list,
             *, max_hops: int = HOP_CAP, approval_timeout_s: float | None = None,
             system_prompt: str | None = None, model: str | None = None,
             emit: Callable[[dict], None] | None = None) -> str:
    """Drive model -> tool -> model up to max_hops tool executions.

    runtime is the existing steward.runtime seam (chat yields {"delta"}).
    registry, settings and approvals come from the tools package; approvals
    may be None only when no write/exec/net tool will be requested. Returns the
    final model text.

    emit is an optional callback for the daemon SSE contract. When set, each
    model call is streamed as {"delta"} events, and every tool step emits a
    {"event": "tool_call"}, {"event": "approval_required"} (only for
    write/exec/net) and {"event": "tool_result"} event. The raw JSON of a
    tool-call reply is never streamed: deltas are held while the reply could
    still be a lone tool call and only emitted once it is clearly prose. When
    emit is None the loop keeps the original behavior (non-streamed model
    calls, final text only). model is passed to every chat call so the caller
    can pin the served model on the shared runtime seam.
    """
    msgs: list[dict] = []
    if system_prompt:
        msgs.append({"role": "system", "content": system_prompt})
    msgs.extend(list(messages))

    hops = 0
    while True:
        reply = _ask(runtime, msgs, model, emit)
        call = extract_tool_call(reply)
        if call is None:
            return reply
        msgs.append({"role": "assistant", "content": reply})
        if hops >= max_hops:
            note = {"tool": "__hop_limit",
                    "output": "tool hop limit reached (%d)" % max_hops}
            msgs.append({"role": "tool", "content": json.dumps(note, sort_keys=True)})
            return _ask(runtime, msgs, model, emit)
        name, args = call
        spec = registry.get(name)
        risk = spec.risk if spec is not None else RISK_READ
        if emit is not None:
            step_id = str(uuid.uuid4())
        else:
            step_id = None

        def _approval_event(record, _id=step_id, _name=name):
            if emit is not None:
                emit({"event": "approval_required", "id": _id, "tool": _name,
                      "args": args, "risk": record["risk"],
                      "expires_at": record["expires_at"],
                      "approval_id": record["id"]})

        if emit is not None:
            emit({"event": "tool_call", "id": step_id, "tool": name,
                  "args": args, "risk": risk})
        started = time.monotonic()
        result = execute_tool(registry, settings, approvals, name, args,
                              approval_timeout_s,
                              on_approval=_approval_event if emit is not None else None)
        ms = int((time.monotonic() - started) * 1000)
        msgs.append({"role": "tool", "content": json.dumps(result, sort_keys=True)})
        if emit is not None:
            emit({"event": "tool_result", "id": step_id,
                  "ok": bool(result.get("ok")),
                  "summary": summarize_result(name, result), "ms": ms})
        hops += 1


def _ask(runtime, messages: list[dict], model: str | None, emit) -> str:
    """One model call. With emit, stream deltas; hold a reply that is still a
    candidate tool call so its JSON never leaks into the delta stream."""
    if emit is None:
        return _model_text(runtime, messages, model)
    reply_parts: list[str] = []
    held: list[str] = []
    live = False
    for item in runtime.chat(messages, stream=True, model=model):
        delta = item.get("delta") or ""
        if not delta:
            continue
        reply_parts.append(delta)
        if live:
            emit({"delta": delta})
            continue
        held.append(delta)
        if not _may_still_be_tool_call("".join(held)):
            flushed, held = held, []
            for piece in flushed:
                emit({"delta": piece})
            live = True
    reply = "".join(reply_parts)
    if extract_tool_call(reply) is None:
        for piece in held:
            emit({"delta": piece})
    return reply


def _may_still_be_tool_call(text: str) -> bool:
    """True while text could still become a lone tool-call JSON reply.

    Drives the streaming hold in _ask: prose (the common case) is flushed the
    moment its first token rules out a lone JSON object, so incremental
    streaming is preserved; a reply that is still a JSON prefix keeps being
    held, and at the end of the model call _ask decides, with extract_tool_call
    as the authority, whether to drop it (tool call) or flush it (prose).
    """
    t = text.lstrip()
    if not t:
        return True  # an empty reply could still become anything
    if t.startswith("```"):
        if t.count("```") < 2:
            return True  # an opening fence could still wrap a JSON object
        inner = t.split("```", 2)[1].lstrip()
        if inner.lower().startswith("json"):
            inner = inner[4:].lstrip()
        if not inner.startswith("{"):
            return False
        return _json_may_be_tool_call(inner)
    if not t.startswith("{"):
        return False
    return _json_may_be_tool_call(t)


def _json_may_be_tool_call(s: str) -> bool:
    try:
        data = json.loads(s)
    except json.JSONDecodeError as exc:
        if exc.msg.startswith("Unterminated string"):
            return True  # still inside a string literal
        if exc.msg == "Extra data":
            return False  # a completed value followed by more: not a lone call
        return exc.pos >= len(s)  # truncated at a token boundary: extendable
    except ValueError:
        return False
    return _is_tool_call_shape(data)


def _is_tool_call_shape(data) -> bool:
    return (isinstance(data, dict)
            and isinstance(data.get("tool"), str)
            and isinstance(data.get("args"), dict))


def _model_text(runtime, messages: list[dict], model: str | None = None) -> str:
    return "".join(item.get("delta", "") for item in runtime.chat(
        messages, stream=False, model=model))


def _first_line(text: str, limit: int = 120) -> str:
    if not text:
        return ""
    line = text.strip().splitlines()[0].strip()
    return line[:limit]


def summarize_result(name: str, result: dict) -> str:
    """A short human-readable line for a tool_result SSE event. The model sees
    the full structured result; the event summary is just for the client."""
    if not result.get("ok"):
        return str(result.get("error") or "tool failed")
    out = result.get("output")
    if not isinstance(out, dict):
        return "tool completed"
    if name == "fs.list":
        entries = out.get("entries")
        return "listed %d entries" % len(entries) if isinstance(entries, list) else "listed"
    if name == "fs.read":
        return "read %s (%d bytes)" % (
            Path(str(out.get("path", "?") or "?")).name, int(out.get("bytes", 0) or 0))
    if name == "fs.write":
        return "wrote %s (%d bytes)" % (
            Path(str(out.get("path", "?") or "?")).name, int(out.get("bytes", 0) or 0))
    if name == "calendar.read":
        return "read calendar %s" % Path(str(out.get("path", "?") or "?")).name
    if name == "calendar.write":
        return "wrote calendar %s" % Path(str(out.get("path", "?") or "?")).name
    if name == "shell.run":
        if out.get("timed_out"):
            return "command timed out after %ss" % out.get("timeout_s", "?")
        detail = _first_line(out.get("stdout") or out.get("stderr") or "")
        return "command exited %s%s" % (out.get("exit_code", "?"),
                                        (": %s" % detail) if detail else "")
    if name == "http.fetch":
        return "http %s (%d bytes)" % (out.get("status", "?"),
                                        int(out.get("bytes", 0) or 0))
    return "tool completed"
