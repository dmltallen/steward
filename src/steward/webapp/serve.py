"""Webapp mount hook: static client and the thin API the client needs.

This module is the S5 mounting hook. The daemon calls its ``dispatch``
function at the top of request routing (one line in daemon.py):

    from steward.webapp import serve as webapp_serve
    ...
    if webapp_serve.dispatch(self, method, parts, path):
        return

``dispatch`` returns True for every path the webapp owns and False for
everything else, so the daemon keeps routing all of its API. The webapp
owns two families of paths:

Static assets (no token, served at the root):
    GET  /                 index.html, the single-page app
    GET  /index.html       the shell
    GET  /app.js           the client
    GET  /style.css        design tokens and styles

Webapp API (Bearer-gated like every daemon write path):
    GET    /v1/webapp/info     settings data for the Settings screen
    GET    /v1/files           retrieval index status and folders
    POST   /v1/files/folders   add a folder to config.retrieval_dirs
    DELETE /v1/files/folders   remove a folder (?path=)
    POST   /v1/files/index     build or refresh the embedding index
    POST   /v1/files/search    {query, k} -> hits with citations

Other screens read daemon routes that other cards own and that the
integrator merges into the same daemon: conversation and chat routes (S2),
brief routes at /v1/briefs (S8), approval routes at /v1/approvals (S9) and
diagnostics at /doctor/bundle (S12). The webapp resolves its data root from
the daemon's SQLite path (the parent of <root>/db/steward.db) so tests and
installs can point the daemon at a throwaway home and never touch
~/.steward. Config writes use the same root, so the Files folders list
persists next to the daemon's own databases.

Auth is delegated to the daemon handler: ``_authed`` calls the handler's
``_require_auth`` and only runs the route once a valid bearer token passed,
matching the S2 security model. The client reacts to a 401 by showing the
pair screen.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from steward.config import Config, atomic_write_json
from steward.paths import config_path
from steward.retrieval.index import BackendError, RetrievalIndex

WEBAPP_DIR = Path(__file__).resolve().parent

ASSET_CONTENT_TYPE = {
    "": "text/html; charset=utf-8",
    "index.html": "text/html; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
}
ASSET_NAMES = {"", "index.html", "app.js", "style.css"}

# Index override so tests and the headless evidence harness can inject a
# deterministic fake instead of calling the real Ollama embedder.
_INDEX_OVERRIDE = None


def set_index_override(index=None) -> None:
    """Pin the retrieval index used by the webapp routes (tests/harness only)."""
    global _INDEX_OVERRIDE
    _INDEX_OVERRIDE = index


# -- routing ------------------------------------------------------------------


def dispatch(handler, method: str, parts: list[str], path: str) -> bool:
    """Handle a request the webapp owns; return True when it did.

    ``handler`` is the daemon's request handler instance, so the webapp can
    reuse its auth, JSON and response helpers. ``path`` has no query string
    (the daemon strips it before dispatch); query access goes through
    ``handler.path`` when a route needs it.
    """
    if method == "GET":
        if path in ("", "/", "/index.html"):
            return _send_asset(handler, "index.html")
        if path in ("/app.js", "/style.css"):
            return _send_asset(handler, path.lstrip("/"))
        if path == "/v1/webapp/info":
            return _authed(handler, lambda h, device: _api_info(h, device))
        if path == "/v1/files":
            return _authed(handler, lambda h, device: _api_files_status(h))
    elif method == "POST":
        if path == "/v1/files/folders":
            return _authed(handler, lambda h, device: _api_add_folder(h))
        if path == "/v1/files/index":
            return _authed(handler, lambda h, device: _api_build_index(h))
        if path == "/v1/files/search":
            return _authed(handler, lambda h, device: _api_search(h))
    elif method == "DELETE":
        if path == "/v1/files/folders":
            return _authed(handler, lambda h, device: _api_remove_folder(h))
    return False


def _authed(handler, fn) -> bool:
    """Run fn only after the daemon accepted the bearer token.

    Returns True here means the webapp answered (401 sent by the daemon when
    the token was bad), so dispatch reports that the path was handled.
    """
    device = handler._require_auth()  # type: ignore[attr-defined]
    if device is None:
        return True
    try:
        fn(handler, device)
    except Exception as exc:  # noqa: BLE001 - a broken route is a 500
        handler._json(500, {"error": str(exc)})  # type: ignore[attr-defined]
    return True


def _send_asset(handler, name: str) -> bool:
    asset = WEBAPP_DIR / (name or "index.html")
    if not asset.is_file():
        handler._json(404, {"error": "not found"})  # type: ignore[attr-defined]
        return True
    body = asset.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", ASSET_CONTENT_TYPE.get(name, "text/html; charset=utf-8"))
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(body)
    return True


# -- data helpers ---------------------------------------------------------------


def _data_root(ctx) -> Path:
    """The data root for the daemon: <root>/db/steward.db -> <root>."""
    return Path(ctx.db_path).resolve().parent.parent


def _read_json_quiet(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _load_cfg(root: Path) -> Config:
    cfg = Config.from_dict(_read_json_quiet(config_path(root)))
    if not cfg.data_dir:
        cfg.data_dir = str(root)
    return cfg


def _save_cfg(root: Path, cfg: Config) -> None:
    if not cfg.data_dir:
        cfg.data_dir = str(root)
    atomic_write_json(config_path(root), cfg.to_dict())


def _index_for(ctx) -> RetrievalIndex:
    if _INDEX_OVERRIDE is not None:
        return _INDEX_OVERRIDE
    return RetrievalIndex(data_root_path=_data_root(ctx))


# -- routes ---------------------------------------------------------------------


def _api_info(handler, device: str) -> None:
    ctx = handler.ctx  # type: ignore[attr-defined]
    root = _data_root(ctx)
    cfg = _load_cfg(root)
    try:
        health = ctx.runtime.health()
    except Exception as exc:  # noqa: BLE001 - health is a report, not a gate
        health = {"ok": False, "error": str(exc), "models": []}
    handler._json(200, {  # type: ignore[attr-defined]
        "model": ctx.model,
        "model_configured": cfg.model,
        "models_available": list(health.get("models") or []),
        "backend_mode": getattr(ctx.runtime, "kind", "unknown"),
        "backend_ok": bool(health.get("ok")),
        "backend_version": health.get("version"),
        "data_dir": str(root),
        "license": {"state": "free", "subscription": False},
        "telemetry": False,
        "pairing_url": "steward://pair?host=%s&port=%d" % (ctx.host_ip, ctx.port),
        "version": ctx.version,
        "device": device,
    })


def _api_files_status(handler) -> None:
    ctx = handler.ctx  # type: ignore[attr-defined]
    root = _data_root(ctx)
    cfg = _load_cfg(root)
    handler._json(200, {  # type: ignore[attr-defined]
        "folders": list(cfg.retrieval_dirs),
        "index": _index_for(ctx).stats(),
        "data_dir": str(root),
    })


def _api_add_folder(handler) -> None:
    ctx = handler.ctx  # type: ignore[attr-defined]
    root = _data_root(ctx)
    cfg = _load_cfg(root)
    data = handler._read_json()  # type: ignore[attr-defined]
    raw = str(data.get("path", "")).strip()
    if not raw:
        handler._json(400, {"error": "path is required"})  # type: ignore[attr-defined]
        return
    folder = Path(raw).expanduser()
    if not folder.is_dir():
        handler._json(400, {"error": "not a directory: %s" % raw})  # type: ignore[attr-defined]
        return
    key = str(folder)
    if key not in cfg.retrieval_dirs:
        cfg.retrieval_dirs.append(key)
        _save_cfg(root, cfg)
    handler._json(201, {"folders": list(cfg.retrieval_dirs)})  # type: ignore[attr-defined]


def _api_remove_folder(handler) -> None:
    ctx = handler.ctx  # type: ignore[attr-defined]
    root = _data_root(ctx)
    cfg = _load_cfg(root)
    query = parse_qs(urlparse(handler.path).query)  # type: ignore[attr-defined]
    raw = (query.get("path") or [""])[0]
    before = list(cfg.retrieval_dirs)
    cfg.retrieval_dirs = [d for d in cfg.retrieval_dirs if d != raw]
    if cfg.retrieval_dirs != before:
        _save_cfg(root, cfg)
    handler._json(200, {"folders": list(cfg.retrieval_dirs)})  # type: ignore[attr-defined]


def _api_build_index(handler) -> None:
    ctx = handler.ctx  # type: ignore[attr-defined]
    root = _data_root(ctx)
    cfg = _load_cfg(root)
    folders = list(cfg.retrieval_dirs)
    if not folders:
        handler._json(400, {"error": "add a folder before building the index"})  # type: ignore[attr-defined]
        return
    summary = _index_for(ctx).index_paths(paths=folders)
    handler._json(200, {"summary": summary, "index": _index_for(ctx).stats()})  # type: ignore[attr-defined]


def _api_search(handler) -> None:
    ctx = handler.ctx  # type: ignore[attr-defined]
    data = handler._read_json()  # type: ignore[attr-defined]
    query = str(data.get("query", "")).strip()
    if not query:
        handler._json(400, {"error": "query is required"})  # type: ignore[attr-defined]
        return
    try:
        k = int(data.get("k", 8))
    except (TypeError, ValueError):
        k = 8
    try:
        hits = _index_for(ctx).search(query, k=max(1, min(k, 50)))
    except BackendError as exc:
        handler._json(502, {"error": str(exc)})  # type: ignore[attr-defined]
        return
    handler._json(200, {"query": query, "hits": hits})  # type: ignore[attr-defined]
