"""Steward CLI (prototype).

Commands:
    steward doctor    print the detected hardware profile
    steward plan      print the auto-selected model bundle
    steward wizard    first-run setup: detect, plan, print install commands
"""

from __future__ import annotations

import argparse
import json
import sys


def _doctor() -> None:
    from .detect import detect
    p = detect()
    d = {
        "os": p.os_name,
        "arch": p.arch,
        "cpu": p.cpu_name,
        "npu_tops": p.npu_tops,
        "gpu_kind": p.gpu_kind,
        "gpu": p.gpu_name,
        "vram_gb": p.vram_gb,
        "ram_gb": p.ram_gb,
        "unified_gb": p.unified_gb,
        "bandwidth_gb_s": p.bandwidth_gb_s,
        "serving_mem_gb": p.usable_mem_gb,
        "notes": p.notes,
    }
    print(json.dumps(d, indent=2))


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
    print(f"  1. ollama serve")
    for m in pulls:
        print(f"  2. ollama pull {m}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="steward")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="detected hardware profile")
    sub.add_parser("plan", help="auto-selected model bundle")
    sub.add_parser("wizard", help="first-run setup wizard")
    args = parser.parse_args(argv)
    if args.cmd == "doctor":
        _doctor()
    elif args.cmd == "plan":
        _plan()
    elif args.cmd == "wizard":
        return _wizard()
    return 0


if __name__ == "__main__":
    sys.exit(main())
