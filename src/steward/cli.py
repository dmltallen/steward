"""Steward CLI.

Commands:
    steward doctor    hardware, chosen bundle, backend mode, model, data dir
    steward plan      print the auto-selected model bundle
    steward check     verdict: what this machine can run, in plain English
    steward wizard    first-run setup: detect, plan, print install commands
    steward setup     first-run wizard (TUI): preflight to phone pairing, resumable
    steward brief     generate today's brief now and print it
    steward serve     run the local daemon (delegates to server/daemon.py)
    steward license   activate a signed perpetual license, print tier status
    steward ota       signed model-catalog updates: check, apply, status
    steward update    signed app + catalog updates: check, apply (schema v2)
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


_SUB_HANDLERS = {"license": "_license_cmd", "ota": "_ota_cmd",
                 "update": "_update_cmd"}


def _nested_handler(name: str):
    return globals()[_SUB_HANDLERS[name]]


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


def _license_cmd(argv) -> int:
    from . import license as lic
    parser = argparse.ArgumentParser(prog="steward license")
    sub = parser.add_subparsers(dest="license_cmd", required=True)
    sub.add_parser("status", help="print the active tier and its features")
    act = sub.add_parser("activate", help="verify and store a signed license file")
    act.add_argument("file", help="path to the signed license JSON")
    args = parser.parse_args(argv)

    if args.license_cmd == "activate":
        try:
            stored = lic.activate(args.file)
        except lic.LicenseError as exc:
            print("refused: %s" % exc)
            return 1
        print("activated %s (%s) on this machine" % (stored.id, stored.tier))
        print("features: " + ", ".join(sorted(stored.features)))
        if not stored.is_perpetual:
            print("expires:  %s" % stored.expires)
        else:
            print("expires:  never (perpetual)")
        return 0

    st = lic.status()
    if st["active"]:
        info = st["license"]
        print("license:  active")
        print("tier:     %s" % st["tier"])
        print("id:       %s" % info["id"])
        print("issued:   %s" % info["issued"])
        print("expires:  %s" % (info["expires"] or "never (perpetual)"))
    else:
        print("license:  not active (free tier)")
        print("reason:   %s" % st["reason"])
    print("features: %s" % ", ".join(st["features"]))
    return 0


def _ota_cmd(argv) -> int:
    from . import license as lic
    from . import ota as ota_mod
    parser = argparse.ArgumentParser(prog="steward ota")
    sub = parser.add_subparsers(dest="ota_cmd", required=True)
    sub.add_parser("status", help="active catalog version and selection plan")
    chk = sub.add_parser("check", help="fetch and verify a signed catalog manifest")
    chk.add_argument("--manifest", default="", help="local file or URL of the manifest")
    aply = sub.add_parser("apply", help="apply a signed catalog update with rollback")
    aply.add_argument("--manifest", default="", help="local file or URL of the manifest")
    aply.add_argument("--model", default="", help="optional model id for a deeper probe")
    args = parser.parse_args(argv)

    if args.ota_cmd == "status":
        st = ota_mod.status()
        print("catalog:  %s (app %s)" % (st["active_catalog"],
                                         st["app_version"] or "embedded"))
        print("models:   %d (%s)" % (st["model_count"], ", ".join(st["models"])))
        if st["hardware_changed"]:
            print("hardware: CHANGED since last selection (re-select pending)")
        if st["plan"]:
            plan = st["plan"]
            print("plan:     %s / %s / vision %s" % (
                plan.get("capability"), plan.get("assistant"),
                "yes" if plan.get("vision") else "no"))
        print("updated:  %s" % (st["updated"] or "never"))
        print("channel:  %s" % st["channel"])
        return 0

    # One source resolution for the CLI, identical to the daemon: an explicit
    # --manifest wins, otherwise STEWARD_OTA_URL, else config.ota_url, else
    # the released DEFAULT_OTA_URL. resolve_source() IS the daemon's path
    # (DaemonContext.update_source -> resolve_source), so a stock install with
    # no env var resolves the same released default the daemon uses. Before
    # this, the CLI refused with "no update source" even though the released
    # default existed, so the shipped default only worked through the daemon.
    source = args.manifest or ota_mod.resolve_source()

    # Every OTA failure below is one plain line for a person, never a
    # traceback: a tampered or corrupt manifest prints "update manifest
    # rejected" (fails closed), an unreachable source prints "update check
    # failed" and names the URL it tried. Signature failures come out of the
    # license layer as lic.LicenseError, not OtaError, so both families have
    # to be caught here; catching only OtaError (as the apply path used to)
    # let a bad signature crash the frozen binary's launcher.
    if args.ota_cmd == "check":
        try:
            info = ota_mod.check_for_update(source)
        except (ota_mod.OtaError, lic.LicenseError) as exc:
            print("update manifest rejected: %s" % exc)
            return 1
        except Exception as exc:  # noqa: BLE001 - a person reads this, not a traceback
            print("update check failed: %s (source: %s)" % (exc, source))
            return 1
        print("update %savailable (current %s -> %s, %d models, released %s)" % (
            "" if info["available"] else "NOT ",
            info["current_version"], info["latest_version"],
            info["model_count"], info["released"] or "unknown"))
        print("source:   %s" % source)
        return 0

    try:
        doc = ota_mod.fetch_document(source)
        result = ota_mod.apply_update(doc, ota_mod.backend_health_probe(args.model))
    except (ota_mod.OtaError, lic.LicenseError) as exc:
        print("update manifest rejected: %s" % exc)
        return 1
    except Exception as exc:  # noqa: BLE001 - a person reads this, not a traceback
        print("update failed: %s (source: %s)" % (exc, source))
        return 1
    if result["applied"]:
        print("applied catalog %s (%d models)" % (result["version"], len(result["models"])))
        plan = result.get("plan") or {}
        print("plan:     %s / %s / vision %s" % (
            plan.get("capability"), plan.get("assistant"),
            "yes" if plan.get("vision") else "no"))
        return 0
    print("NOT applied: %s" % (result.get("reason") or "unknown"))
    return 1


def _update_cmd(argv) -> int:
    from . import ota as ota_mod
    parser = argparse.ArgumentParser(prog="steward update")
    sub = parser.add_subparsers(dest="update_cmd", required=True)
    chk = sub.add_parser("check", help="check for a signed app + catalog update")
    chk.add_argument("--source", default="",
                     help="manifest URL or path (default: the released source)")
    aply = sub.add_parser("apply", help="download and apply a signed update "
                                        "with rollback")
    aply.add_argument("--source", default="")
    aply.add_argument("--no-app", action="store_true",
                      help="apply the catalog only, skip the app payload")
    args = parser.parse_args(argv)
    source = args.source or ota_mod.resolve_source()

    if args.update_cmd == "check":
        info = ota_mod.startup_check_status(source=source)
        if info["state"] != "ok":
            print("update check %s: %s" % (info["state"], info.get("error") or "no source"))
            return 1
        cat, app = info["catalog"], info["app"]
        channel = info.get("channel") or ota_mod.detect_channel()
        print("catalog:  %s -> %s (%d models)%s" % (
            cat["current_version"], cat["latest_version"], cat["model_count"],
            " [AVAILABLE]" if cat["available"] else ""))
        if app.get("latest_version"):
            payload_note = app["notes_url"] or app["notes"] or ""
            if info.get("managed_by"):
                print("app:      %s -> %s (updates handled by %s; "
                      "run: %s)" % (app["current_version"], app["latest_version"],
                                    info["managed_by"],
                                    info.get("managed_update_command") or "?"))
            else:
                print("app:      %s -> %s%s%s" % (
                    app["current_version"], app["latest_version"],
                    " [AVAILABLE]" if app["available"] else " (not available)",
                    ("  " + payload_note) if payload_note else ""))
        else:
            print("app:      manifest carries no app payload for update")
        print("source:   %s" % info["source"])
        print("channel:  %s" % channel)
        return 0

    try:
        doc = ota_mod.fetch_document(source)
        payload = ota_mod.verify_manifest(doc)
        # A package manager owns this install: the one-click apply is not
        # allowed to stage or swap the app binary. Refuse before any download
        # (this is the CLI twin of the daemon route's managed-by guard).
        refusal = ota_mod.managed_by_update_refusal(channel=ota_mod.detect_channel(),
                                                    payload=payload)
        if refusal is not None:
            print("refused: %s" % refusal["error"])
            print("channel: %s" % refusal["channel"])
            return 1
        staged = None
        if payload.get("app") is not None and not args.no_app:
            staged = ota_mod.stage_app_payload(doc, source=source)
            if not staged["staged"]:
                print("app payload not staged: %s" % staged["reason"])
        probe = (ota_mod.app_binary_probe(staged["path"], staged["version"])
                 if staged is not None and staged["staged"]
                 else ota_mod.backend_health_probe())
        result = ota_mod.apply_update(doc, health_probe=probe)
    except ota_mod.OtaError as exc:
        print("refused: %s" % exc)
        return 1
    except Exception as exc:  # noqa: BLE001 - a person reads this, not a traceback
        print("update failed: %s" % exc)
        return 1
    if result["applied"]:
        print("applied catalog %s" % result["version"])
        app = result.get("app")
        if app and app.get("installed"):
            print("applied app %s (%s, %s)" % (
                app["version"], app["platform"], app["binary"]))
        else:
            print("app:      no app install in this apply (%s)"
                  % ((app or {}).get("reason") or "catalog only"))
        return 0
    print("NOT applied: %s" % (result.get("reason") or "unknown"))
    if result.get("rolled_back"):
        print("rolled back to the previous app and catalog")
    elif result.get("app"):
        app_reason = (result["app"] or {}).get("reason")
        print("app:      %s" % (app_reason or "not applied"))
    return 1


def main(argv=None) -> int:
    # license, ota and update get their own nested parsers; route them before
    # the outer argparse, which would reject their subcommand tokens.
    tokens = sys.argv[1:] if argv is None else list(argv)
    if tokens and tokens[0] in ("license", "ota", "update"):
        handler = _nested_handler(tokens[0])
        return handler(tokens[1:])

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
    sp_setup = sub.add_parser("setup", help="first-run wizard, one screen to "
                                           "phone pairing (resumable)")
    # Flags mirror wizard.run_tui; the values go to wizard.run_tui_options.
    sp_setup.add_argument("--model", default="")
    sp_setup.add_argument("--source", default="")
    sp_setup.add_argument("--port", type=int, default=0)
    sp_setup.add_argument("--from", dest="from_state", default="")
    sp_setup.add_argument("--keep-daemon", action="store_true")
    sp_setup.add_argument("--home", default="")
    sp_brief = sub.add_parser("brief", help="generate today's brief now and print it")
    sp_brief.add_argument("--date", default="", help="YYYY-MM-DD, default today")
    sp_brief.add_argument("--scope", default="today")
    sp_serve = sub.add_parser("serve", help="run the local daemon "
                                             "(src/steward/server/daemon.py)")
    # Declared here so the top-level parser accepts them, then forwarded to the
    # daemon's own argparse below. Without this, `steward serve --port N` failed
    # with "unrecognized arguments" even though the daemon supports them.
    sp_serve.add_argument("--host", default="")
    sp_serve.add_argument("--port", type=int, default=0)
    sp_serve.add_argument("--model", default="")
    sp_serve.add_argument("--source", default="")
    sp_serve.add_argument("--ollama-url", dest="ollama_url", default="")
    sub.add_parser("version", help="print the package version")
    sub.add_parser("license", help="perpetual license: activate, status")
    sub.add_parser("ota", help="signed catalog updates: check, apply, status")
    sub.add_parser("update", help="signed app + catalog updates: check, apply")
    args = parser.parse_args(argv)
    if args.cmd == "doctor":
        return _doctor(args)
    if args.cmd == "plan":
        _plan()
    elif args.cmd == "check":
        return _check()
    elif args.cmd == "wizard":
        return _wizard()
    elif args.cmd == "setup":
        from . import wizard as _wizard_mod
        return _wizard_mod.run_tui_options(model=args.model, source=args.source,
                                           port=args.port,
                                           from_state=args.from_state,
                                           keep_daemon=args.keep_daemon,
                                           home=args.home)
    elif args.cmd == "brief":
        from .memory.brief import build_brief
        try:
            out = build_brief(args.date or None, args.scope or "today")
        except Exception as exc:  # noqa: BLE001 - a person reads this, not a traceback
            print("brief failed: %s" % exc)
            print("check that the backend is running (steward doctor).")
            return 1
        print(out.get("text") or "(empty brief)")
        return 0
    elif args.cmd == "serve":
        from .server.daemon import main as daemon_main
        fwd = []
        if args.host:
            fwd += ["--host", args.host]
        if args.port:
            fwd += ["--port", str(args.port)]
        if args.model:
            fwd += ["--model", args.model]
        if args.source:
            fwd += ["--source", args.source]
        if args.ollama_url:
            fwd += ["--ollama-url", args.ollama_url]
        return daemon_main(fwd)
    elif args.cmd == "version":
        return _version()
    return 0


if __name__ == "__main__":
    sys.exit(main())
