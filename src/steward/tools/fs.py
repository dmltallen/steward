"""fs tools: list, read and write files inside the configured roots.

Every path goes through sandbox.resolve_within_roots, so traversal,
symlink-escape and sensitive-material refusals apply uniformly. Reads refuse
binary content (NUL sniff) and oversize files; writes create parent
directories inside the root only and never touch anything outside it.

Risk: read for list/read (no approval), write for fs.write (approval
required before the handler runs, enforced by registry.execute_tool).
"""

from __future__ import annotations

from steward.tools.sandbox import ToolError, resolve_within_roots


def fs_list(settings, args: dict) -> dict:
    resolved, root = resolve_within_roots(args["path"], settings.roots())
    if not resolved.is_dir():
        raise ToolError("not a directory: %s" % resolved)
    entries = []
    for child in sorted(resolved.iterdir(), key=lambda p: p.name.lower()):
        try:
            is_dir = child.is_dir()
            size = None if is_dir else child.stat().st_size
        except OSError:
            continue  # a broken entry is skipped, never a crash
        entries.append({"name": child.name, "type": "dir" if is_dir else "file",
                        "size": size})
    return {"path": str(resolved), "root": str(root), "entries": entries}


def fs_read(settings, args: dict) -> dict:
    resolved, root = resolve_within_roots(args["path"], settings.roots())
    if not resolved.is_file():
        raise ToolError("not a readable file: %s" % resolved)
    size = resolved.stat().st_size
    if size > settings.max_file_bytes:
        raise ToolError("file too large to read (%d bytes, cap %d)"
                        % (size, settings.max_file_bytes))
    data = resolved.read_bytes()
    if b"\x00" in data[:8192]:
        raise ToolError("refusing binary file: %s" % resolved)
    return {"path": str(resolved), "root": str(root),
            "bytes": len(data), "content": data.decode("utf-8", errors="replace")}


def fs_write(settings, args: dict) -> dict:
    resolved, root = resolve_within_roots(args["path"], settings.roots())
    content = str(args.get("content", ""))
    resolved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved.write_text(content, encoding="utf-8")
    return {"path": str(resolved), "root": str(root),
            "bytes": len(content.encode("utf-8")), "written": True}
