"""Steward data paths.

Single source for where Steward keeps state on disk. Everything lives under
one data root: ~/.steward by default, STEWARD_HOME to move it. Subdirectories:

    db/        SQLite databases and index metadata
    models/    model blobs downloaded by the managed sidecar
    index/     local vector indexes built from retrieval_dirs
    logs/      daemon and runtime logs
    licenses/  perpetual license entitlements

Directories are created on demand and are never world-readable (0o700, owner
only). This is the dependency leaf of the runtime stack: config.py and
runtime.py build on it and nothing in here imports them. Import surface:
os and pathlib only.
"""

from __future__ import annotations

import os
from pathlib import Path

DATA_ROOT_ENV = "STEWARD_HOME"
DEFAULT_ROOT_NAME = ".steward"

# Subdirectories kept under the data root. Order does not matter.
SUBDIRS = ("db", "models", "index", "logs", "licenses")


def data_root() -> Path:
    """Resolve the data root from STEWARD_HOME, else the per-user default."""
    override = os.environ.get(DATA_ROOT_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / DEFAULT_ROOT_NAME


def dirs(root: Path | None = None) -> dict[str, Path]:
    """Map of subdir name to its path under the data root. No creation."""
    base = root if root is not None else data_root()
    return {name: base / name for name in SUBDIRS}


def ensure_dir(path: Path) -> Path:
    """Create a directory owner-only (0o700) and return it."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass  # chmod is best effort; mkdir already applied the 0o700 mode
    return path


def ensure_dirs(root: Path | None = None) -> dict[str, Path]:
    """Create the data root and every subdirectory, all owner-only."""
    base = ensure_dir(root if root is not None else data_root())
    return {name: ensure_dir(base / name) for name in SUBDIRS}


def config_path(root: Path | None = None) -> Path:
    """Path to config.json under the data root. No creation."""
    base = root if root is not None else data_root()
    return base / "config.json"


def db_dir() -> Path:
    return dirs()["db"]


def models_dir() -> Path:
    return dirs()["models"]


def index_dir() -> Path:
    return dirs()["index"]


def logs_dir() -> Path:
    return dirs()["logs"]


def licenses_dir() -> Path:
    return dirs()["licenses"]
