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

The model is told the protocol through tool_system_prompt(); callers may pass
their own system_prompt to run_loop instead.
"""

from __future__ import annotations

import json

from steward.tools.registry import execute_tool

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
             system_prompt: str | None = None) -> str:
    """Drive model -> tool -> model up to max_hops tool executions.

    runtime is the existing steward.runtime seam (chat yields {"delta"}).
    registry, settings and approvals come from the tools package; approvals
    may be None only when no write/exec/net tool will be requested. Returns the
    final model text.
    """
    msgs: list[dict] = []
    if system_prompt:
        msgs.append({"role": "system", "content": system_prompt})
    msgs.extend(list(messages))

    hops = 0
    while True:
        reply = _model_text(runtime, msgs)
        call = extract_tool_call(reply)
        if call is None:
            return reply
        msgs.append({"role": "assistant", "content": reply})
        if hops >= max_hops:
            note = {"tool": "__hop_limit",
                    "output": "tool hop limit reached (%d)" % max_hops}
            msgs.append({"role": "tool", "content": json.dumps(note, sort_keys=True)})
            return _model_text(runtime, msgs)
        name, args = call
        result = execute_tool(registry, settings, approvals, name, args,
                              approval_timeout_s)
        msgs.append({"role": "tool", "content": json.dumps(result, sort_keys=True)})
        hops += 1


def _model_text(runtime, messages: list[dict]) -> str:
    return "".join(item.get("delta", "") for item in runtime.chat(messages, stream=False))
