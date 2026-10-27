"""Steward CLI.

Commands:
    steward doctor    hardware, chosen bundle, backend mode, model, data dir
    steward plan      print the auto-selected model bundle
    steward check     verdict: what this machine can run, in plain English
    steward wizard    first-run setup: detect, plan, print install commands
    steward serve     run the local daemon (delegates to server/daemon.py)
    steward version   print the package version

The compatibility verdict (compat.py) is computed first and fed to the
selector, so doctor, plan, check, and wizard all describe the same machine the
same way. doctor and check never raise on an unreachable backend or an odd
machine; they print a clear message instead of a traceback.
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


def _doctor(args) -> int:
    from . import paths
    from .compat import verdict
    from .config import Config
    from .detect import detect
    from .runtime import make_runtime
    from .selector import select_bundle

    cfg = Config.load()
    print("Steward doctor")
    print("==============")
    print(f"data dir:  {cfg.data_dir or paths.data_root()}")
    p = detect()
    v = verdict(p, p.disk_free_gb)
    print(f"hardware:  {p.os_name} {p.arch}, cpu {p.cpu_name or 'unknown'}")
    print(f"           serving memory {v.serving_mem_gb:g} GB ({p.gpu_kind}), "
          f"verdict {v.state}")
    b = select_bundle(p, v)
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
    if getattr(args, "bundle", None):
        from pathlib import Path

        from .diagnostics import build_bundle
        out = build_bundle(Path(args.bundle), runtime=rt)
        print(f"bundle:    {out}")
        print("Send this file to steward support; it contains no tokens, "
              "keys, or personal names.")
    return 0


def _plan() -> None:
    from .compat import verdict
    from .detect import detect
    from .selector import select_bundle

    p = detect()
    v = verdict(p, p.disk_free_gb)
    b = select_bundle(p, v)
    print(json.dumps({
        "class": b.class_name,
        "serving_mem_gb": b.serving_mem_gb,
        "verdict": v.state,
        "capability": b.capability.id,
        "assistant": b.assistant.id,
        "vision": b.vision,
        "capability_est_tok_s": list(b.capability.tok_range),
    }, indent=2))


def _check() -> int:
    from .compat import verdict
    from .detect import detect
    from .selector import select_bundle

    p = detect()
    v = verdict(p, p.disk_free_gb)
    b = select_bundle(p, v)
    state_label = {"no-fit": "no-fit", "lite": "light mode", "full": "full"}[v.state]
    print("Steward check")
    print("=============")
    print(f"machine:  {p.os_name} {p.arch}, {p.gpu_name or p.gpu_kind}, "
          f"cpu {p.cpu_name or 'unknown'}, {p.ram_gb:g} GB memory")
    print(f"serving:  {v.serving_mem_gb:g} GB of usable memory for model weights")
    print(f"verdict:  {v.state} ({state_label}). {v.min_hardware_sentence}")
    print("reasons:")
    for reason in v.reasons:
        print(f"  - {reason}")
    vision = "yes" if b.vision else "no"
    print(f"bundle:   tier {b.class_name}, assistant {b.assistant.id}, "
          f"capability {b.capability.id} (Q4 about {b.capability.q4_gb:g} GB), "
          f"image support {vision}")
    return 0


def _wizard() -> int:
    from .compat import verdict
    from .detect import detect
    from .selector import select_bundle

    p = detect()
    v = verdict(p, p.disk_free_gb)
    b = select_bundle(p, v)
    print("Steward first-run setup")
    print("=======================")
    print("Detected hardware")
    print(f"  os: {p.os_name} {p.arch}, cpu: {p.cpu_name or 'unknown'}")
    print(f"  serving memory: {v.serving_mem_gb:g} GB ({p.gpu_kind}), "
          f"verdict {v.state}")
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
    p_doctor = sub.add_parser("doctor", help="hardware, bundle, backend mode, model, data dir")
    p_doctor.add_argument(
        "--bundle", metavar="PATH",
        help="write a redacted support bundle zip to PATH and print its location",
    )
    sub.add_parser("plan", help="auto-selected model bundle")
    sub.add_parser("check", help="what this machine can run, in plain English")
    sub.add_parser("wizard", help="first-run setup wizard")
    sub.add_parser("serve", help="run the local daemon (src/steward/server/daemon.py)")
    sub.add_parser("version", help="print the package version")
    args = parser.parse_args(argv)
    if args.cmd == "doctor":
        return _doctor(args)
    if args.cmd == "plan":
        _plan()
    elif args.cmd == "check":
        return _check()
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
