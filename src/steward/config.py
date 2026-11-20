"""Steward configuration.

A single JSON file at <data>/config.json holds persistent settings; the data
root is ~/.steward unless STEWARD_HOME points elsewhere (see paths.py).
Environment variables win over the file on every load:

    STEWARD_PORT         server listen port (must parse as an integer)
    STEWARD_MODEL        served assistant model id
    STEWARD_OLLAMA_URL   backend base URL for SystemOllama
    STEWARD_OLLAMA_BIN   managed-sidecar binary (see runtime.py)
    STEWARD_OTA_URL      update source override (default: the released one)
    STEWARD_HOME         data root, controls where config.json lives

Persisted fields and defaults:

    port            3877
    model           ""             (empty means the daemon default)
    data_dir        <data root>    (where db/models/index/logs live)
    ollama_url      http://localhost:11434
    ollama_bin      ""             (sidecar binary path or search)
    telemetry       false          (always false, forced on load)
    allow_shell     false
    retrieval_dirs  []
    ota_url         DEFAULT_OTA_URL (the released update source; a consumer
                    never sets an env var for a normal install)

Writes are atomic: a temp file in the same directory is fsynced and renamed
over the target, and the file is never left world-readable (0o600). Import
surface: stdlib plus steward.paths.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

import steward.paths as paths

DEFAULT_PORT = 3877
DEFAULT_OLLAMA_URL = "http://localhost:11434"

# The released update source: the signed update manifest that release builds
# publish into the repo's releases/ dir. STEWARD_OTA_URL overrides it; an
# operator can also pin a different source in <data>/config.json. Reaching
# it is never required for a normal start: an unreachable source records a
# status on the daemon instead of failing the unit.
# This is the private gitea raw path, so it answers only to a client with repo
# access; `tools/cut_release.py` publishes the manifest to this exact path on
# master (it commits releases/update.json as part of a release). A public
# GitHub mirror changes the URL, and the new default is this one constant.
DEFAULT_OTA_URL = (
    "https://github.com/dmltallen/steward/releases/latest/download/update.json"
)

_PORT_ENV = "STEWARD_PORT"
_ENV_TO_ATTR = {
    "STEWARD_MODEL": "model",
    "STEWARD_OLLAMA_URL": "ollama_url",
    "STEWARD_OLLAMA_BIN": "ollama_bin",
    "STEWARD_OTA_URL": "ota_url",
}


@dataclass
class Config:
    """Effective runtime configuration (file values plus env overrides)."""

    port: int = DEFAULT_PORT
    model: str = ""
    data_dir: str = ""
    ollama_url: str = DEFAULT_OLLAMA_URL
    ollama_bin: str = ""
    telemetry: bool = False
    allow_shell: bool = False
    retrieval_dirs: list = field(default_factory=list)
    ota_url: str = DEFAULT_OTA_URL

    def to_dict(self) -> dict:
        return {
            "port": self.port,
            "model": self.model,
            "data_dir": self.data_dir,
            "ollama_url": self.ollama_url,
            "ollama_bin": self.ollama_bin,
            "telemetry": self.telemetry,
            "allow_shell": self.allow_shell,
            "retrieval_dirs": list(self.retrieval_dirs),
            "ota_url": self.ota_url,
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "Config":
        known = {
            "port": int,
            "model": str,
            "data_dir": str,
            "ollama_url": str,
            "ollama_bin": str,
            "telemetry": bool,
            "allow_shell": bool,
            "retrieval_dirs": list,
            "ota_url": str,
        }
        out: dict = {}
        for key, cast in known.items():
            if key in data:
                try:
                    out[key] = cast(data[key])
                except (TypeError, ValueError):
                    pass  # corrupt entry: keep default for that field
        cfg = cls(**out)
        # Telemetry is always off, no matter what the file or env claims.
        cfg.telemetry = False
        return cfg

    @classmethod
    def load(cls, env: Mapping | None = None) -> "Config":
        """Load config from <data>/config.json merged with env overrides.

        env may be passed explicitly for tests; otherwise os.environ is used.
        STEWARD_HOME from the env dict decides the data root.
        """
        env = os.environ if env is None else env
        root = _root_from_env(env)
        cfg = cls.from_dict(_read_json_quiet(paths.config_path(root)))
        if not cfg.data_dir:
            cfg.data_dir = str(root)
        _apply_env_overrides(cfg, env)
        return cfg

    def save(self, env: Mapping | None = None) -> "Config":
        """Write this config atomically to <data>/config.json. Returns self."""
        env = os.environ if env is None else env
        root = _root_from_env(env)
        if not self.data_dir:
            self.data_dir = str(root)
        atomic_write_json(paths.config_path(root), self.to_dict())
        return self


def _root_from_env(env: Mapping) -> Path:
    override = env.get("STEWARD_HOME")
    if override:
        return Path(override)
    return paths.data_root()


def _read_json_quiet(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _apply_env_overrides(cfg: Config, env: Mapping) -> None:
    port_raw = env.get(_PORT_ENV)
    if port_raw:
        try:
            cfg.port = int(port_raw)
        except (TypeError, ValueError):
            pass  # invalid STEWARD_PORT: keep the file or default value
    for name, attr in _ENV_TO_ATTR.items():
        value = env.get(name)
        if value:
            setattr(cfg, attr, value)


def atomic_write_json(path: Path, data: dict, mode: int = 0o600) -> None:
    """Write data to path atomically (tmp + fsync + rename), owner-only."""
    paths.ensure_dir(path.parent)
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
