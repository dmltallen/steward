"""Sandbox policy helpers shared by the file and network tools.

resolve_within_roots() is the gate every fs-style handler passes a path
through: realpath containment (the fully resolved path, symlinks followed,
must sit under one configured root), refusal of traversal escapes, symlink
escapes, and sensitive material (.ssh, .env, private keys) named per the task
brief. check_url_policy() is the matching gate for network fetches: https
only, exact allowlisted host, no credentials, default port.

Both raise ToolError with a reason a human (and the model) can read; handlers
never expose a raw path outside these helpers.

Import surface: stdlib (pathlib) only.
"""

from __future__ import annotations

import re
from pathlib import Path

RE_SENSITIVE_DOTENV = re.compile(r"^\.env(?:\..*)?$", re.IGNORECASE)

# Path components that are refused no matter where they sit under a root.
SENSITIVE_NAMES = frozenset({
    ".ssh", ".gnupg", ".aws", ".pki",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", "id_ed448",
})

# File suffixes treated as private key material.
SENSITIVE_SUFFIXES = frozenset({
    ".pem", ".key", ".p12", ".pfx", ".asc", ".gpg", ".ppk",
})


class ToolError(RuntimeError):
    """A sandbox or policy refusal carrying a readable reason."""


def _is_sensitive_component(component: str) -> bool:
    low = component.lower()
    if low in SENSITIVE_NAMES:
        return True
    if RE_SENSITIVE_DOTENV.match(low):
        return True
    return False


def resolve_within_roots(path: str, roots: list[Path]) -> tuple[Path, Path]:
    """Return (resolved_absolute, containing_root) or raise ToolError.

    The returned resolved path is the realpath (all symlinks followed) and is
    guaranteed to sit inside one of the configured roots, so I/O on it can
    never touch anything outside the sandbox: '../..' segments, absolute paths
    outside every root, and symlinks pointing out of the root all resolve to a
    location that fails containment.
    """
    if not roots:
        raise ToolError("no fs roots are configured for the file tools")
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        raise ToolError("path must be absolute: %r" % str(raw))
    resolved = raw.resolve(strict=False)

    root_dir: Path | None = None
    for entry in roots:
        base = Path(entry).resolve(strict=False)
        if resolved == base or resolved.is_relative_to(base):
            root_dir = base
            break
    if root_dir is None:
        raise ToolError("path escapes the configured fs roots: %s" % str(resolved))

    rel = resolved.relative_to(root_dir)
    parts = [p for p in rel.parts if p not in ("", ".")]
    for index, component in enumerate(parts):
        if _is_sensitive_component(component):
            raise ToolError("refusing sensitive path component %r" % component)
    if resolved.suffix.lower() in SENSITIVE_SUFFIXES and (parts or rel.parts):
        raise ToolError("refusing private key file %r" % resolved.name)
    return resolved, root_dir


def preflight_path(settings, args: dict) -> None:
    """Reject a file path before an approval is even prompted.

    Traversal, symlink escape, absolute-outside and sensitive-material paths
    are refused here (fail fast) and again inside the handler, so the approval
    queue never shows an obviously invalid request.
    """
    resolve_within_roots(args["path"], settings.roots())


def check_url_policy(url: str, allowlist: list[str]) -> None:
    """Raise ToolError unless the URL is https to an exact allowlisted host.

    The scheme must be https, network credentials are refused, and the URL
    hostname must be an exact member of the allowlist; a subdomain or a
    lookalike suffix never matches ("sub.example.com" and "notexample.com" are
    both refused for an allowlist of "example.com").
    """
    import urllib.parse

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise ToolError("http.fetch allows https only, got scheme %r" % parsed.scheme)
    if parsed.username or parsed.password:
        raise ToolError("credentials in a fetch URL are not allowed")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ToolError("fetch URL has no host")
    allowed = {str(h).lower() for h in allowlist}
    if host not in allowed:
        raise ToolError("host %r is not in the fetch allowlist" % host)
