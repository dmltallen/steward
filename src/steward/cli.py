"""Steward CLI.

Commands:
    steward doctor    hardware, chosen bundle, backend mode, model, data dir
    steward plan      print the auto-selected model bundle
    steward wizard    first-run setup: detect, plan, print install commands
    steward serve     run the local daemon (delegates to server/daemon.py)
    steward version   print the package version

doctor never raises on an unreachable backend: it prints a clear degraded
message so the failure mode is obvious instead of a traceback.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _package_version() -> str:
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
    return "0.1.0"


def _doctor() -> int:
    from . import paths
    from .config import Config
    from .detect import detect
    from .runtime import make_runtime
    from .selector import select_bundle

    cfg = Config.load()
    print("Steward doctor")
    print("==============")
    print(f"data dir:  {cfg.data_dir or paths.data_root()}")
    p = detect()
    print(f"hardware:  {p.os_name} {p.arch}, cpu {p.cpu_name or 'unknown'}")
    print(f"           memory pool {p.usable_mem_gb:g} GB ({p.gpu_kind})")
    b = select_bundle(p)
    vision = ", vision" if b.vision else ""
    print(f"bundle:    {b.class_name}, assistant {b.assistant.id}, "
          f"capability {b.capability.id}{vision}")
    print(f"model:     {cfg.model or 'unset (daemon default)'}")
    rt = make_runtime(cfg)
    try:
        health = rt.health()
        print(f"backend:   {rt.kind}")
        if health.get("ok"):
            print(f"           ok, version {health.get('version')}, "
                  f"{len(health.get('models') or [])} model(s) cached")
        else:
            error = health.get("error") or "unreachable"
            print(f"           DEGRADED: {error}")
            if rt.kind == "sidecar-ollama":
                print("           sidecar_available="
                      f"{str(health.get('sidecar_available')).lower()}")
                if not health.get("sidecar_available"):
                    print("           searched: "
                          + ", ".join(health.get("searched_paths") or []))
    except Exception as exc:  # noqa: BLE001 - doctor must never traceback
        print(f"backend:   {rt.kind} DEGRADED: {exc}")
    return 0


def _plan() -> None:
    from .detect import detect
    from .selector import select_bundle

    p = detect()
    b = select_bundle(p)
    print(json.dumps({
        "class": b.class_name,
        "serving_mem_gb": b.serving_mem_gb,
        "capability": b.capability.id,
        "assistant": b.assistant.id,
        "vision": b.vision,
        "capability_est_tok_s": list(b.capability.tok_range),
    }, indent=2))


def _wizard() -> int:
    from .detect import detect
    from .selector import select_bundle

    p = detect()
    b = select_bundle(p)
    print("Steward first-run setup")
    print("=======================")
    print("Detected hardware")
    print(f"  os: {p.os_name} {p.arch}, cpu: {p.cpu_name or 'unknown'}")
    print(f"  memory pool: {p.usable_mem_gb:g} GB ({p.gpu_kind})")
    print()
    print("Chosen bundle")
    print(b.describe())
    print()
    pulls = [b.capability.id]
    if b.assistant.id != b.capability.id:
        pulls.insert(0, b.assistant.id)
    print("Setup: install the backend, then pull the bundle models")
    print("  1. ollama serve")
    for m in pulls:
        print(f"  2. ollama pull {m}")
    return 0


def _version() -> int:
    print(f"steward {_package_version()}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="steward")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="hardware, bundle, backend mode, model, data dir")
    sub.add_parser("plan", help="auto-selected model bundle")
    sub.add_parser("wizard", help="first-run setup wizard")
    sub.add_parser("serve", help="run the local daemon (src/steward/server/daemon.py)")
    sub.add_parser("version", help="print the package version")
    args = parser.parse_args(argv)
    if args.cmd == "doctor":
        return _doctor()
    if args.cmd == "plan":
        _plan()
    elif args.cmd == "wizard":
        return _wizard()
    elif args.cmd == "serve":
        from .server.daemon import main as daemon_main
        return daemon_main((argv or [])[1:])
    elif args.cmd == "version":
        return _version()
    return 0


if __name__ == "__main__":
    sys.exit(main())
