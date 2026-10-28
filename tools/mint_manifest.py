#!/usr/bin/env python3
"""Mint a signed Steward v2 update manifest (releases/update.json).

Schema-v2 manifests carry the open-model catalog generation AND the app
block that points at the per-platform Steward payloads for this release. The
catalog generation is serialized from the embedded stewards.models catalog in
this tree, so one source keeps model identity. The app payload sha256 and
size are computed here from the built artifacts, never typed by hand.

Usage (from the repo root, with src on the path):

    PYTHONPATH=src python3 tools/mint_manifest.py \\
        --catalog-version 2026-10-28.1 \\
        --notes-url https://github.com/dmltallen/steward/raw/branch/master/docs/CHANGELOG.md \\
        --payload linux-aarch64 dist/steward_1.1.0_arm64.deb \\
        --payload linux-x86_64 dist/steward_1.1.0_amd64.deb \\
        --out releases/update.json

The signature uses the Steward vendor key: --key defaults to
~/.steward-keys/steward-signing.key (or $STEWARD_SIGNING_KEY), the same key
that signs licenses (tools/mint_license.py gen-key). The releases/ dir is
committed and the daemons released config points at it, so a consumer never
sets an env var on a normal install.

Re-running with the same inputs is byte-deterministic except for the payload
content (which is hashed, not embedded), so CI can diff the gitignored
payloads delta without churning update.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _log(msg: str) -> None:
    print("mint: %s" % msg)


def _load_private_key(path: Path) -> bytes:
    from steward import license as lic

    pem = path.read_bytes()
    return lic.private_bytes_from_pem(pem)


def _catalog_payload(catalog_version: str, released: str) -> dict:
    from steward import ota as ota_mod
    from steward import __version__

    embedded = ota_mod.embedded_catalog()
    return {
        "version": catalog_version,
        "released": released,
        "app_version": __version__,
        "models": embedded["models"],
        "note": "signed catalog + app manifest published by the release flow",
    }


def _payloads(args) -> dict:
    out = {}
    for platform, path in args.payload:
        blob = Path(path).read_bytes()
        digest = hashlib.sha256(blob).hexdigest()
        name = Path(path).name
        out[platform] = {
            "url": name,
            "sha256": digest,
            "size": len(blob),
        }
        _log("payload %s <- %s (%d bytes, %s)" % (platform, path, len(blob), digest[:16]))
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="mint_manifest", description=__doc__)
    parser.add_argument("--key", default=os.environ.get("STEWARD_SIGNING_KEY",
                                                       str(Path.home() / ".steward-keys" / "steward-signing.key")))
    parser.add_argument("--catalog-version", default="")
    parser.add_argument("--released", default=date.today().isoformat())
    parser.add_argument("--notes", default="")
    parser.add_argument("--notes-url", default="")
    parser.add_argument("--min-source", default="0.0.0")
    parser.add_argument("--payload", action="append", nargs=2, metavar=("PLATFORM", "PATH"),
                        default=[], help="repeatable: app payload for one platform")
    parser.add_argument("--out", default=str(REPO_ROOT / "releases" / "update.json"))
    args = parser.parse_args(argv)

    key_path = Path(args.key).expanduser()
    if not key_path.is_file():
        print("refusing: signing key not found at %s" % key_path)
        return 2

    sys.path.insert(0, str(REPO_ROOT / "src"))
    from steward import license as lic

    catalog_version = args.catalog_version or date.today().isoformat() + ".1"
    payload = _catalog_payload(catalog_version, args.released)
    app_block = {
        "version": payload["app_version"],
        "released": args.released,
        "min_source_version": args.min_source,
        "payloads": _payloads(args),
    }
    if args.notes:
        app_block["notes"] = args.notes
    if args.notes_url:
        app_block["notes_url"] = args.notes_url
    if "notes" not in app_block and "notes_url" not in app_block:
        parser.error("give --notes or --notes-url for the app block")
        return 2
    payload["app"] = app_block

    private = _load_private_key(key_path)
    envelope = lic.make_envelope(payload, private)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(envelope, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _log("wrote %s (catalog %s, app %s, %d platform(s))"
         % (out, catalog_version, payload["app_version"], len(args.payload)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
