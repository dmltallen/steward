"""Tool registry: the single catalog of what the assistant may call.

A tool spec is four parts: a stable name, a description for the model, a JSON
schema for argument validation, and a risk in {read, write, exec, net}.
execute_tool() is the one choke point every call passes through: it validates
the arguments against the schema, then for write/exec/net calls opens a
pending approval and blocks until a human decides (auto-deny on timeout). No
handler ever runs before both gates clear.

Registered tools:
    fs.list, fs.read          read risk, file tools inside configured roots
    fs.write                  write risk
    calendar.read             read risk, ICS feeds inside roots
    calendar.write            write risk, ICS content validated
    shell.run                 exec risk, refused unless allow_shell
    http.fetch                net risk, https + exact allowlist only

Import surface: stdlib plus the sibling handler modules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from steward.tools.settings import ToolSettings
from steward.tools.sandbox import preflight_path

RISK_READ = "read"
RISK_WRITE = "write"
RISK_EXEC = "exec"
RISK_NET = "net"
APPROVAL_RISKS = frozenset({RISK_WRITE, RISK_EXEC, RISK_NET})

Handler = Callable[[ToolSettings, dict], dict]
Preflight = Callable[[ToolSettings, dict], None]


@dataclass(frozen=True)
class ToolSpec:
    """One registered connector."""

    name: str
    description: str
    schema: dict
    risk: str
    handler: Handler
    preflight: Preflight | None = None  # fail-fast policy check before approval


# -- minimal JSON Schema validation -------------------------------------------
#
# The subset the tool schemas use: type (string list form included), properties,
# required, additionalProperties, items, enum, pattern, min/max length, min/max
# and exclusive bounds. Everything else in a schema is ignored by design, and
# validate() never raises on the schema itself.

_TYPE_NAMES = {
    "null": type(None),
    "boolean": bool,
    "array": list,
    "integer": int,
    "string": str,
    "object": dict,
}


def _type_of(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def validate_args(schema: dict, args: dict) -> list[str]:
    """Return a list of schema errors for args (empty means valid)."""
    return _validate_value(args, schema, "$")


def _validate_value(value, schema, where: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(schema, dict):
        return errors
    type_spec = schema.get("type")
    if type_spec is not None:
        allowed = type_spec if isinstance(type_spec, list) else [type_spec]
        actual = _type_of(value)
        if actual not in allowed:
            errors.append("%s: expected type %s, got %s"
                          % (where, type_spec, actual or "null"))
            return errors  # other constraints are meaningless on a type error
    if value is None:
        return errors
    if isinstance(value, str):
        if "enum" in schema and value not in schema["enum"]:
            errors.append("%s: value %r is not one of %s"
                          % (where, value, schema["enum"]))
        pattern = schema.get("pattern")
        if pattern:
            if not re.search(pattern, value):
                errors.append("%s: does not match pattern %r" % (where, pattern))
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append("%s: shorter than %d characters"
                          % (where, schema["minLength"]))
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append("%s: longer than %d characters"
                          % (where, schema["maxLength"]))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append("%s: below the minimum %s" % (where, schema["minimum"]))
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append("%s: not above the exclusive minimum %s"
                          % (where, schema["exclusiveMinimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            errors.append("%s: above the maximum %s" % (where, schema["maximum"]))
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            errors.append("%s: not below the exclusive maximum %s"
                          % (where, schema["exclusiveMaximum"]))
    elif isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                errors.extend(_validate_value(item, items,
                                              "%s[%d]" % (where, index)))
    elif isinstance(value, dict):
        properties = schema.get("properties", {}) or {}
        for required in schema.get("required", []) or []:
            if required not in value:
                errors.append("%s: missing required property %r" % (where, required))
        extra_ok = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                errors.extend(_validate_value(item, properties[key],
                                              "%s.%s" % (where, key)))
            elif extra_ok is False:
                errors.append("%s: unexpected property %r" % (where, key))
    return errors


def _path_schema():
    return {"type": "object", "properties": {"path": {"type": "string", "minLength": 1}},
            "required": ["path"], "additionalProperties": False}


def _path_content_schema():
    return {"type": "object",
            "properties": {"path": {"type": "string", "minLength": 1},
                           "content": {"type": "string", "default": ""}},
            "required": ["path"], "additionalProperties": False}


def build_registry(settings: ToolSettings) -> dict[str, ToolSpec]:
    """Build the catalog. settings is not read at build time, only passed to
    handlers, so one registry serves any configuration."""
    from steward.tools import calendar as calendar_mod
    from steward.tools import fs as fs_mod
    from steward.tools import http as http_mod
    from steward.tools import shell as shell_mod

    specs = [
        ToolSpec(
            "fs.list",
            "List the entries of a directory inside an allowed root. "
            "Absolute path only; sensitive paths (dot ssh, env, keys) refused.",
            _path_schema(), RISK_READ, fs_mod.fs_list, preflight_path),
        ToolSpec(
            "fs.read",
            "Read a text file inside an allowed root. Absolute path only; "
            "binary and oversize files refused.",
            _path_schema(), RISK_READ, fs_mod.fs_read, preflight_path),
        ToolSpec(
            "fs.write",
            "Create or overwrite a text file inside an allowed root. "
            "Absolute path only. Requires human approval before it runs.",
            _path_content_schema(), RISK_WRITE, fs_mod.fs_write, preflight_path),
        ToolSpec(
            "calendar.read",
            "Read an ICS calendar feed inside an allowed root. "
            "Absolute path only; non-ICS content refused.",
            _path_schema(), RISK_READ, calendar_mod.calendar_read, preflight_path),
        ToolSpec(
            "calendar.write",
            "Write an ICS calendar feed inside an allowed root. "
            "Content must carry BEGIN:VCALENDAR and END:VCALENDAR. "
            "Requires human approval before it runs.",
            _path_content_schema(), RISK_WRITE, calendar_mod.calendar_write,
            preflight_path),
        ToolSpec(
            "shell.run",
            "Run one shell command without a shell interpreter "
            "(shlex-split tokens, timeout, capped output). Disabled unless "
            "allow_shell is set. Requires human approval before it runs.",
            {"type": "object",
             "properties": {"command": {"type": "string", "minLength": 1},
                            "timeout": {"type": "integer", "minimum": 1,
                                        "maximum": 60, "default": 30}},
             "required": ["command"], "additionalProperties": False},
            RISK_EXEC, shell_mod.shell_run, shell_mod.preflight_shell),
        ToolSpec(
            "http.fetch",
            "Fetch a URL. https only, host must be on the exact allowlist, "
            "redirects re-checked. Requires human approval before it runs.",
            {"type": "object",
             "properties": {"url": {"type": "string", "minLength": 1},
                            "timeout": {"type": "number", "minimum": 0.1,
                                        "maximum": 60.0, "default": 8.0}},
             "required": ["url"], "additionalProperties": False},
            RISK_NET, http_mod.http_fetch, http_mod.preflight_url),
    ]
    return {spec.name: spec for spec in specs}


def execute_tool(registry: dict[str, ToolSpec], settings: ToolSettings,
                 approvals, name: str, args: dict,
                 approval_timeout_s: float | None = None) -> dict:
    """Run one tool call behind both gates: schema validation, then (for
    write/exec/net) a human approval. Returns a structured result, never
    raises on a refusal, so the loop can feed it straight back to the model.
    """
    if not isinstance(args, dict):
        return {"ok": False, "tool": name,
                "error": "tool arguments must be a JSON object"}
    spec = registry.get(name)
    if spec is None:
        return {"ok": False, "tool": name,
                "error": "unknown tool: %s" % name}
    errors = validate_args(spec.schema, args)
    if errors:
        return {"ok": False, "tool": name, "risk": spec.risk,
                "error": "schema validation failed: %s" % "; ".join(errors)}
    if spec.preflight is not None:
        try:
            spec.preflight(settings, args)
        except Exception as exc:  # noqa: BLE001 - a refused tool is a result
            return {"ok": False, "tool": name, "risk": spec.risk,
                    "error": str(exc)}
    if spec.risk in APPROVAL_RISKS:
        if approvals is None:
            return {"ok": False, "tool": name, "risk": spec.risk,
                    "error": "no approval manager is available for %s-risk calls"
                             % spec.risk}
        record = approvals.create(name, args, spec.risk)
        decision, reason = approvals.wait(
            record["id"], approval_timeout_s if approval_timeout_s is not None
            else settings.approval_timeout_s)
        if decision != "approved":
            return {"ok": False, "tool": name, "risk": spec.risk,
                    "approval_id": record["id"],
                    "error": "denied by approval (%s)" % reason}
        try:
            output = spec.handler(settings, args)
        except Exception as exc:  # noqa: BLE001 - a refused tool is a result
            return {"ok": False, "tool": name, "risk": spec.risk,
                    "approval_id": record["id"], "error": str(exc)}
        return {"ok": True, "tool": name, "risk": spec.risk,
                "approval_id": record["id"], "output": output}
    try:
        output = spec.handler(settings, args)
    except Exception as exc:  # noqa: BLE001 - a refused tool is a result
        return {"ok": False, "tool": name, "risk": spec.risk, "error": str(exc)}
    return {"ok": True, "tool": name, "risk": spec.risk, "output": output}
