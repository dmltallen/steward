"""Tool settings: every knob the sandbox reads, in one dataclass.

The tools package carries its own settings object rather than extending
steward.config.Config, so the sandbox shape is explicit, tests can build a
throwaway scope, and no persistent config key is invented before the product
needs it. build_settings() optionally starts from a steward.config.Config for
the values that already have a home there:

    fs_roots          allowed file roots. Default: [config.data_dir], else []
    allow_shell       shell.run runs only when true. Default: config.allow_shell
    http_allowlist    exact https hostnames http.fetch may contact (lowercase)

Everything else (timeouts, approval store paths, byte caps) has a default that
overrides can replace, including in the field list below.

Storage for approvals defaults under the data root: <data>/db/approvals.db and
<data>/logs/approvals.log, following paths.py, unless overrides pin them to a
test or demo location.

Import surface: stdlib plus steward.paths (data root and logs dir).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from steward import paths

DEFAULT_APPROVAL_TTL_S = 120      # a pending approval expires after this
DEFAULT_APPROVAL_TIMEOUT_S = 120  # how long the caller blocks, then denies
DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_HTTP_BYTES = 512 * 1024
DEFAULT_MAX_SHELL_OUTPUT = 64 * 1024
DEFAULT_HTTP_TIMEOUT_S = 8.0


@dataclass
class ToolSettings:
    """Sandbox configuration shared by every registered tool."""

    fs_roots: list = field(default_factory=list)          # list[Path]
    allow_shell: bool = False
    http_allowlist: list = field(default_factory=list)    # list[str] hostnames
    data_dir: str = ""                                    # fallback root + logs
    approval_ttl_s: int = DEFAULT_APPROVAL_TTL_S
    approval_timeout_s: int = DEFAULT_APPROVAL_TIMEOUT_S
    approval_db: str = ""       # empty -> <data>/db/approvals.db
    approval_log: str = ""      # empty -> <data>/logs/approvals.log
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_http_bytes: int = DEFAULT_MAX_HTTP_BYTES
    max_shell_output: int = DEFAULT_MAX_SHELL_OUTPUT
    http_timeout_s: float = DEFAULT_HTTP_TIMEOUT_S
    http_tls_context: object | None = None  # ssl context override, tests only

    def roots(self) -> list[Path]:
        out: list[Path] = []
        for entry in self.fs_roots:
            out.append(entry if isinstance(entry, Path) else Path(str(entry)))
        return out


def build_settings(cfg=None, overrides: dict | None = None) -> ToolSettings:
    """Assemble ToolSettings from an optional Config and an override dict.

    cfg is a steward.config.Config when available; overrides wins on every
    key it names. The approvals DB and log follow the data root unless a
    caller pins them (tests and demos always do).
    """
    ov = dict(overrides or {})
    data_dir = str(ov.get("data_dir") or (cfg.data_dir if cfg and cfg.data_dir else paths.data_root()))

    roots: list[Path] = []
    if "fs_roots" in ov and ov["fs_roots"] is not None:
        roots = [r if isinstance(r, Path) else Path(str(r)) for r in ov["fs_roots"]]
    elif cfg is not None and cfg.data_dir:
        roots = [Path(cfg.data_dir)]

    allow_shell = ov.get("allow_shell")
    if allow_shell is None:
        allow_shell = bool(getattr(cfg, "allow_shell", False)) if cfg is not None else False

    settings = ToolSettings(
        fs_roots=roots,
        allow_shell=bool(allow_shell),
        http_allowlist=[str(h).lower() for h in (ov.get("http_allowlist") or [])],
        data_dir=data_dir,
        approval_ttl_s=int(ov.get("approval_ttl_s", DEFAULT_APPROVAL_TTL_S)),
        approval_timeout_s=int(ov.get("approval_timeout_s", DEFAULT_APPROVAL_TIMEOUT_S)),
        approval_db=str(ov.get("approval_db") or str(Path(data_dir) / "db" / "approvals.db")),
        approval_log=str(ov.get("approval_log") or str(Path(data_dir) / "logs" / "approvals.log")),
        max_file_bytes=int(ov.get("max_file_bytes", DEFAULT_MAX_FILE_BYTES)),
        max_http_bytes=int(ov.get("max_http_bytes", DEFAULT_MAX_HTTP_BYTES)),
        max_shell_output=int(ov.get("max_shell_output", DEFAULT_MAX_SHELL_OUTPUT)),
        http_timeout_s=float(ov.get("http_timeout_s", DEFAULT_HTTP_TIMEOUT_S)),
        http_tls_context=ov.get("http_tls_context"),
    )
    return settings
