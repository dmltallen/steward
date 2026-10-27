"""Steward tools: connectors the assistant model can call, all behind gates.

The assistant never touches the machine directly. Every tool call goes through
one choke point (registry.execute_tool) that validates the arguments against
the tool's JSON schema and, for write/exec/net calls, requires a human
approval before anything runs. Package layout:

    settings.py   ToolSettings: fs roots, allowlist, timeouts, sandbox knobs
    registry.py   ToolSpec, JSON-schema validation, execute_tool gate
    sandbox.py    realpath containment plus sensitive-path refusal
    fs.py         fs.list / fs.read / fs.write handlers
    calendar.py   calendar.read / calendar.write (ICS validated) handlers
    shell.py      shell.run handler, disabled unless allow_shell
    http.py       http.fetch handler, https + exact-host allowlist only
    approvals.py  pending-approval store, wait/decide, HTTP surface
    loop.py       model -> tool -> model session loop (4-hop cap)

English only everywhere, no em dashes, no fabrications. The selector rules
live only in selector.py and models.py; this package does not touch them.
"""

from steward.tools.loop import HOP_CAP, extract_tool_call, run_loop
from steward.tools.registry import (
    APPROVAL_RISKS,
    RISK_EXEC,
    RISK_NET,
    RISK_READ,
    RISK_WRITE,
    ToolSpec,
    build_registry,
    execute_tool,
    validate_args,
)
from steward.tools.settings import ToolSettings, build_settings

__all__ = [
    "APPROVAL_RISKS", "RISK_EXEC", "RISK_NET", "RISK_READ", "RISK_WRITE",
    "ToolSpec", "ToolSettings", "build_registry", "build_settings",
    "execute_tool", "validate_args", "run_loop", "extract_tool_call", "HOP_CAP",
]
