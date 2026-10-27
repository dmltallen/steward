"""shell tool: run one command, exec risk.

shell.run is registered in the catalog (so the model knows it exists) but the
handler refuses to run anything unless settings.allow_shell is true. Even when
enabled the command runs with no shell interpretation (shlex-split tokens),
a bounded timeout, a cut-down environment and capped output, and the call
still requires a human approval because it is exec risk.

Risk: exec.
"""

from __future__ import annotations

import os
import shlex
import subprocess

from steward.tools.sandbox import ToolError

_SHELL_TIMEOUT_DEFAULT = 30


def preflight_shell(settings, args: dict) -> None:
    """Refuse a shell call before an approval is prompted when disabled."""
    if not settings.allow_shell:
        raise ToolError("shell.run is disabled by config (allow_shell=false)")


def shell_run(settings, args: dict) -> dict:
    if not settings.allow_shell:
        raise ToolError("shell.run is disabled by config (allow_shell=false)")
    command = str(args["command"])
    timeout = int(args.get("timeout") or _SHELL_TIMEOUT_DEFAULT)
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise ToolError("could not parse command: %s" % exc)
    if not tokens:
        raise ToolError("empty command")
    roots = settings.roots()
    cwd = str(roots[0]) if roots else None
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    try:
        proc = subprocess.run(tokens, capture_output=True, timeout=timeout,
                              cwd=cwd, env=env)
    except subprocess.TimeoutExpired as exc:
        return {"exit_code": None, "timed_out": True, "timeout_s": timeout,
                "stdout": _decode(settings, exc.stdout or b""),
                "stderr": _decode(settings, exc.stderr or b"")}
    except OSError as exc:
        raise ToolError("could not run %r: %s" % (tokens[0], exc))
    return {"exit_code": proc.returncode, "timed_out": False,
            "stdout": _decode(settings, proc.stdout),
            "stderr": _decode(settings, proc.stderr)}


def _decode(settings, data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    if len(text) <= settings.max_shell_output:
        return text
    return text[:settings.max_shell_output] + "\n[... truncated]"
