"""Steward diagnostics: redacted support bundles and log rotation.

A stranger should be able to open a support bundle and work out what went
wrong without being handed secrets. build_bundle() writes a zip of plain
members: the system profile, selector output, versions, redacted config, the
last 200 log lines, index stats, and license state.

Redaction lives in one place, Redactor: session tokens (sv1.*), Bearer
values, sk-* API keys, secret key/value assignments, license blobs, home
paths (hashed by default) and hostnames (hashed by default). Anything a
secret-shaped value could sit in is scrubbed before it reaches the zip, and
the patterns match inside free text, so a token embedded mid-sentence is
still removed.

Log rotation is stdlib-only: rotate_log() rolls <data>/logs/steward.log at a
size cap and keeps exactly a fixed number of files (the current file plus
numbered rolls). No external logger dependency; call rotate_log() before
appending, or wire it to a stdlib RotatingFileHandler.

Import surface: stdlib plus steward.config, steward.compat, steward.detect,
steward.selector and steward.paths. retrieval.index (which optionally pulls
numpy) is imported lazily inside the bundle builder so it only loads when a
bundle is actually being made.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import socket
import time
import zipfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from . import paths
from .compat import verdict as compute_verdict
from .config import Config
from .detect import detect
from .selector import select_bundle

# ---------------------------------------------------------------------------
# Log rotation
# ---------------------------------------------------------------------------

LOG_MAX_BYTES = 5 * 1024 * 1024   # rotate at 5 MB
LOG_KEEP_TOTAL = 3                # current file plus two rolls = exactly 3 files
LOG_NAME = "steward.log"


def steward_log_path(root: Path | None = None) -> Path:
    """The rotation-managed log file under the data root's logs directory."""
    base = root if root is not None else paths.data_root()
    return paths.dirs(base)["logs"] / LOG_NAME


def rotate_log(log_path: str | Path, max_bytes: int = LOG_MAX_BYTES,
               keep: int = LOG_KEEP_TOTAL) -> int:
    """Rotate a log file at the size cap, keeping exactly `keep` files.

    The current file becomes <name>.1, each roll bumps up one, and the oldest
    numbered roll that would exceed the keep budget is dropped. A fresh empty
    current file is created so the invariant ``exactly keep files total``
    holds right after rotation. Returns 1 when a rotation happened, else 0.

    keep=3 means steward.log, steward.log.1 and steward.log.2. This is
    stdlib-only; call it before appending a line (or after a write that grows
    the file past the cap).
    """
    log_path = Path(log_path)
    if not log_path.exists():
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch()
        return 0
    try:
        size = log_path.stat().st_size
    except OSError:
        return 0
    if size < max_bytes:
        return 0

    def roll(name: int) -> Path:
        return Path(f"{log_path}.{name}")

    # Drop any numeric roll at or beyond the keep budget (defensive; a stale
    # .3 or higher should never survive an earlier buggy run).
    name = keep
    while True:
        stale = roll(name)
        if not stale.exists():
            break
        stale.unlink()
        name += 1

    # Drop the oldest roll that would be pushed out of the budget, then shift
    # the remaining rolls up by one: .1 -> .2, .2 -> .3, ... never beyond the
    # cap. With keep=3 that leaves at most .1 and .2 plus the current file.
    oldest = roll(keep - 1)
    if oldest.exists():
        oldest.unlink()
    for i in range(keep - 2, 0, -1):
        src = roll(i)
        if not src.exists():
            continue
        dst = roll(i + 1)
        if dst.exists():
            dst.unlink()
        src.rename(dst)

    if log_path.exists():
        first = roll(1)
        if first.exists():
            first.unlink()
        log_path.rename(first)
    log_path.touch()
    return 1


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

REDACTED = "[REDACTED]"


def _sha12(value: str) -> str:
    """Short content hash for identity-hiding substitutions."""
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:12]


# A Steward session token: sv1.<exp>.<b64(device)>.<b64(nonce)>.<b64(sig)>,
# where each base64url part is unpadded. Matched standalone so a token
# embedded mid-sentence is still removed.
_TOKEN_RE = re.compile(
    r"\bsv1\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}"
    r"\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}"
)

_BEARER_RE = re.compile(r"\bbearer[ \t]*[:=]?[ \t]+[^\s,;]{6,}", re.IGNORECASE)

_SK_RE = re.compile(r"\bsk-[A-Za-z0-9]{16,}")

# Secret-shaped key = value (or key: value) assignments in free text. The
# value must be at least 4 characters so ordinary prose words after words
# like "the key: for" are left alone; real tokens and keys are far longer.
_KEYVAL_RE = re.compile(
    r"\b((?:[A-Za-z0-9_.-]*)?(?:token|secret|password|passwd|passphrase|"
    r"authorization|api[_-]?key|license|licence|key))\b"
    r"[ \t]*[=:][ \t]*([^\s,;\"']{4,})",
    re.IGNORECASE,
)

_HOME_RE = re.compile(r"/home/[A-Za-z0-9_.-]+")

_WINUSER_RE = re.compile(r"(?i)([A-Za-z]:\\Users\\)([^\\]+)")

# JSON keys whose whole value is replaced, whatever it looks like.
_SECRET_WORDS = frozenset({
    "token", "secret", "password", "passwd", "passphrase", "license",
    "licence", "authorization", "auth", "bearer", "key",
})


def _is_secret_key(name: str) -> bool:
    """True when a JSON field name marks its value as secret material."""
    normalized = re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_")
    if normalized in _SECRET_WORDS:
        return True
    words = set(normalized.split("_"))
    if words == {"api", "key"}:
        return True
    return bool(words & {
        "token", "secret", "password", "passwd", "passphrase",
        "license", "licence", "authorization", "bearer",
    })


class Redactor:
    """Scrub secrets and optional identifiers from diagnostic text.

    hash_home=True hashes the per-user component of /home/<user> paths and
    the per-user component of Windows Users dirs, so structure survives but
    the account name does not. hash_hostname=True hashes the local hostname
    where it appears verbatim. Both are defaults; pass False to keep the
    literals (a user-chosen tradeoff, never on by default).
    """

    def __init__(self, hash_home: bool = True, hash_hostname: bool = True) -> None:
        self.hash_home = hash_home
        self.hash_hostname = hash_hostname
        self._host = socket.gethostname() or ""
        self._node = platform.node() or ""

    # -- text ---------------------------------------------------------------

    def redact_text(self, text: str) -> str:
        """Return text with every recognized secret or identifier scrubbed."""
        text = _TOKEN_RE.sub(REDACTED, text)
        text = _BEARER_RE.sub(lambda m: "bearer " + REDACTED, text)
        text = _SK_RE.sub("sk-" + REDACTED, text)
        text = _KEYVAL_RE.sub(lambda m: m.group(1) + "=" + REDACTED, text)
        if self.hash_home:
            text = _HOME_RE.sub(self._hash_home, text)
            text = _WINUSER_RE.sub(self._hash_winuser, text)
        if self.hash_hostname:
            for candidate in (self._host, self._node):
                if candidate:
                    text = text.replace(candidate, "host-" + _sha12(candidate))
        return text

    def _hash_home(self, match: re.Match) -> str:
        user = match.group(0).rsplit("/", 1)[-1]
        return "/home/" + _sha12(user)

    def _hash_winuser(self, match: re.Match) -> str:
        return match.group(1) + _sha12(match.group(2))

    # -- structured json --------------------------------------------------------------

    def redact_json_value(self, value, key: str | None = None) -> Any:
        """Recursively scrub a parsed JSON value.

        Secret-shaped keys are blanked entirely; every other string passes
        through redact_text (token patterns plus optional home and hostname
        hashing), so paths and inline secrets are covered inside values too.
        The return type mirrors the input shape (dict, list, str or scalar).
        """
        if isinstance(value, dict):
            return {k: self.redact_json_value(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact_json_value(v, key) for v in value]
        if isinstance(value, str):
            if key is not None and _is_secret_key(key):
                return REDACTED
            return self.redact_text(value)
        return value


# ---------------------------------------------------------------------------
# Bundle members
# ---------------------------------------------------------------------------

BUNDLE_MEMBERS = (
    "system.txt",
    "bundle.json",
    "versions.txt",
    "config.redacted.json",
    "recent-errors.log",
    "index-stats.json",
    "license-state.txt",
)

RECENT_LOG_LINES = 200


def _steward_version() -> str:
    try:
        from importlib.metadata import version as _meta_version
        return _meta_version("steward")
    except Exception:
        pass  # not installed as a package; fall through to pyproject.toml
    here = Path(__file__).resolve()
    for base in (here.parent, here.parents[2]):
        project = base / "pyproject.toml"
        if not project.exists():
            continue
        try:
            import tomllib
            with open(project, "rb") as fh:
                return str(tomllib.load(fh)["project"]["version"])
        except Exception:
            continue
    return "unknown"


def _model_dict(model) -> dict:
    data = asdict(model)
    data["tok_range"] = list(model.tok_range)
    return data


def _system_text(profile, verdict) -> str:
    lines = [
        "Steward system report",
        "=====================",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"OS: {profile.os_name} {platform.release()}",
        f"Arch: {profile.arch}",
        "Hardware profile:",
        f"  cpu: {profile.cpu_name or 'unknown'}",
        f"  gpu kind: {profile.gpu_kind} ({profile.gpu_name or 'onboard'})",
        f"  ram: {profile.ram_gb:g} GB",
        f"  unified memory: {profile.unified_gb:g} GB",
        f"  vram: {profile.vram_gb:g} GB",
        f"  disk free at data dir: {profile.disk_free_gb:g} GB",
        f"  detection confidence: {profile.confidence}",
        f"Serving memory: {verdict.serving_mem_gb:g} GB, verdict {verdict.state}",
        "Verdict reasons:",
    ]
    for reason in verdict.reasons:
        lines.append(f"  - {reason}")
    return "\n".join(lines) + "\n"


def _bundle_json_text(profile, verdict, bundle) -> str:
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "class": bundle.class_name,
        "serving_mem_gb": verdict.serving_mem_gb,
        "verdict": verdict.state,
        "recommended_tier": verdict.recommended_tier,
        "vision": bundle.vision,
        "capability": _model_dict(bundle.capability),
        "assistant": _model_dict(bundle.assistant),
        "reasons": list(verdict.reasons),
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _versions_text(runtime) -> str:
    ollama = "unreachable"
    backend = runtime.kind if runtime is not None else "unknown"
    try:
        health = runtime.health() if runtime is not None else {}
        if health.get("ok") and health.get("version"):
            ollama = str(health["version"])
        elif health.get("version"):
            ollama = str(health["version"]) + " (degraded)"
    except Exception as exc:  # noqa: BLE001 - a broken backend never kills doctor
        ollama = f"unreachable ({exc})"
    return (
        f"steward {_steward_version()}\n"
        f"ollama {ollama} ({backend})\n"
        f"python {platform.python_version()}\n"
    )


def _redact_config_text(redactor: Redactor, root: Path,
                        env: Mapping | None = None) -> str:
    cfg = Config.load(env)
    redacted = redactor.redact_json_value(cfg.to_dict())
    return json.dumps(redacted, indent=2, sort_keys=True) + "\n"


def _recent_log_text(redactor: Redactor, root: Path) -> str:
    log = steward_log_path(root)
    if not log.exists():
        location = redactor.redact_text(str(log))
        return (
            "No steward log file present yet (expected at the data root's logs "
            f"directory; resolved path: {location}).\n"
        )
    try:
        raw = log.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"Could not read steward log: {redactor.redact_text(str(exc))}\n"
    lines = raw.splitlines()[-RECENT_LOG_LINES:]
    redacted = [redactor.redact_text(line) for line in lines]
    return "\n".join(redacted) + "\n" if redacted else "(empty log file)\n"


def _index_stats_text(redactor: Redactor, root: Path) -> str:
    from .retrieval.index import RetrievalIndex

    try:
        stats = RetrievalIndex(data_root_path=root).stats()
    except Exception as exc:  # noqa: BLE001 - a broken index never kills doctor
        stats = {"error": redactor.redact_text(str(exc))}
    return json.dumps(redactor.redact_json_value(stats), indent=2, sort_keys=True) + "\n"


def _license_state_text(redactor: Redactor, root: Path) -> str:
    lic_dir = paths.dirs(root)["licenses"]
    entries = []
    if lic_dir.is_dir():
        entries = sorted(p.name for p in lic_dir.iterdir() if p.is_file())
    lines = [
        "Steward license state",
        "=====================",
        f"Entitlement files present: {len(entries)}",
    ]
    for name in entries:
        lines.append(f"  file name only: {redactor.redact_text(name)}")
    if not entries:
        lines.append(
            "No license entitlement files found; running without an "
            "entitlement file (community usage)."
        )
    lines.append("No license keys or blobs are included in this bundle.")
    return "\n".join(lines) + "\n"


def build_bundle(out_path: str | Path, *, root: Path | None = None,
                 runtime=None, redactor: Redactor | None = None,
                 env: Mapping | None = None) -> Path:
    """Write a redacted support bundle zip to out_path. Returns the path.

    root selects the Steward data root to read config, logs, index and
    licenses from; it defaults to paths.data_root() (STEWARD_HOME). runtime
    is any object with .kind and .health() (defaults to make_runtime(cfg));
    pass a stub in tests. env is the STEWARD_* environment mapping for
    Config.load (defaults to os.environ).

    Every member is gathered defensively: one failing collector writes a
    short redacted error into its member instead of aborting the bundle, so
    support still gets everything else.
    """
    base = root if root is not None else paths.data_root()
    base = Path(base)
    redactor = redactor or Redactor()
    env = env if env is not None else os.environ

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    p = detect(base)
    v = compute_verdict(p, p.disk_free_gb)
    b = select_bundle(p, v)
    runtime = runtime if runtime is not None else _default_runtime(env)

    collectors = {
        "system.txt": lambda: _system_text(p, v),
        "bundle.json": lambda: _bundle_json_text(p, v, b),
        "versions.txt": lambda: _versions_text(runtime),
        "config.redacted.json": lambda: _redact_config_text(redactor, base, env),
        "recent-errors.log": lambda: _recent_log_text(redactor, base),
        "index-stats.json": lambda: _index_stats_text(redactor, base),
        "license-state.txt": lambda: _license_state_text(redactor, base),
    }

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in BUNDLE_MEMBERS:
            try:
                text = collectors[name]()
            except Exception as exc:  # noqa: BLE001 - one member fails, bundle survives
                text = (
                    f"Failed to collect {name}: "
                    f"{redactor.redact_text(str(exc))}\n"
                )
            if not text.endswith("\n"):
                text += "\n"
            archive.writestr(name, text)
    return out_path


def _default_runtime(env: Mapping | None = None):
    from .runtime import make_runtime

    return make_runtime(Config.load(env))


def rotate_steward_log(root: Path | None = None) -> int:
    """Rotate <data>/logs/steward.log at the 5 MB cap. Returns 0 or 1."""
    return rotate_log(steward_log_path(root))
