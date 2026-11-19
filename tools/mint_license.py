#!/usr/bin/env python3
"""Steward vendor-side license minting tool.

Developer tool, never shipped to end users. It petitions no server: it signs
a license file with the vendor Ed25519 key and prints the result, so a
license can be minted for a customer offline and handed over out of band.

Usage:

    tools/mint_license.py gen-key --out ~/.steward-keys/steward-signing.key
        Generate the vendor signing keypair once. Writes the private key
        (PEM, chmod 0o600) to --out and prints the matching public key hex.
        Embed that public key in src/steward/license.py (VENDOR_PUBLIC_KEY_HEX)
        and keep the private key out of the repo.

    tools/mint_license.py mint --tier personal --local --key PATH --out a.json
        Mint a license for THIS machine (computes its hardware fingerprint).
        Or pass --fingerprint sha256:<hex> to target another machine.

    tools/mint_license.py mint --tier free --local \\
            --pack browser_agent --pack-version 1 --out pack.json
        Mint a license that carries a one-time feature pack on top of its
        tier grant (repeatable: --pack can be passed several times, each
        with a matching --pack-version). Pack ids must be registered in
        src/steward/features.py; an unknown id is refused at mint time.
        Packs are additive and perpetual unless --pack-expires sets an
        explicit ISO date.

    tools/mint_license.py verify --file a.json
        Check a file's signature against the embedded vendor public key.

Every --key accepts the default ~/.steward-keys/steward-signing.key (set
STEWARD_SIGNING_KEY to override). The private key never leaves the machine it
was generated on and is never committed. No subscriptions: a minted license
is perpetual by default, --expires makes the rare time-boxed copy explicit.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

DEFAULT_KEY_DIR = Path.home() / ".steward-keys"
DEFAULT_KEY_PATH = DEFAULT_KEY_DIR / "steward-signing.key"
KEY_ENV = "STEWARD_SIGNING_KEY"

# Keep steward importable when run straight from a checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _key_path(arg: str | None) -> Path:
    env = os.environ.get(KEY_ENV, "")
    if arg:
        return Path(arg).expanduser()
    if env:
        return Path(env).expanduser()
    return DEFAULT_KEY_PATH


def _need_key(path: Path) -> bytes:
    if not path.is_file():
        sys.exit(
            "no signing key at %s (create it with:\n"
            "  %s gen-key --out %s)\n"
            "The private key must stay outside the repo; it is never committed."
            % (path, os.path.basename(sys.argv[0]), path)
        )
    return path.read_bytes()


def cmd_gen_key(args) -> int:
    from steward.license import generate_keypair

    path = Path(args.out).expanduser()
    if path.exists() and not args.force:
        sys.exit("refusing to overwrite %s (use --force) " % path)
    pem, pub_hex = generate_keypair()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, str(path))
    print("wrote private key: %s (0o600, keep out of the repo)" % path)
    print("public key hex (embed as VENDOR_PUBLIC_KEY_HEX in src/steward/license.py):")
    print("  " + pub_hex)
    return 0


def _fingerprint(args) -> str:
    if args.fingerprint:
        return args.fingerprint
    if args.local:
        from steward.license import machine_fingerprint
        return machine_fingerprint()
    sys.exit("one of --fingerprint or --local is required")


def _packs_from_args(args) -> list[dict]:
    """Build the signed packs list from the --pack / --pack-version flags.

    Every pack id must be registered in steward.features (is_pack True), so
    a vendor cannot mint a pack this build cannot honor. Fail closed at the
    minting desk, not at the customer's machine.
    """
    ids = args.pack or []
    if not ids:
        return []
    from steward import features as feat

    unknown = [i for i in ids if not feat.is_pack(i)]
    if unknown:
        sys.exit("unknown pack id(s), not in the feature registry: %s"
                 % ", ".join(sorted(unknown)))
    versions = list(args.pack_version or [])
    if len(versions) > len(ids):
        sys.exit("more --pack-version values than --pack ids")
    versions = versions + [1] * (len(ids) - len(versions))
    packs = []
    for pack_id, version in zip(ids, versions):
        if isinstance(version, bool) or version < 1:
            sys.exit("--pack-version must be a positive integer for %s" % pack_id)
        entry = {"id": pack_id, "version": version}
        if args.pack_expires:
            entry["expires"] = args.pack_expires
        packs.append(entry)
    return packs


def cmd_mint(args) -> int:
    from steward import license as lic

    key_path = _key_path(args.key)
    private_bytes = lic.private_bytes_from_pem(_need_key(key_path))
    tier = args.tier or "personal"
    issuable = lic.tier_features(tier)
    if args.feature:
        extra = set(args.feature)
        unknown = extra - lic.all_features()
        if unknown:
            sys.exit("unknown feature(s): %s" % ", ".join(sorted(unknown)))
        issuable = frozenset(issuable) | extra
    payload = {
        "id": args.id or lic.new_license_id(),
        "tier": tier,
        "machine": _fingerprint(args),
        "issued": args.issued or lic.today_iso(),
        "features": sorted(issuable),
    }
    packs = _packs_from_args(args)
    if packs:
        payload["packs"] = packs
    if args.expires:
        payload["expires"] = args.expires
    if args.note:
        payload["note"] = args.note
    doc = lic.make_envelope(payload, private_bytes)

    out = args.out
    if not out or out == "-":
        print(json.dumps(doc, indent=2, sort_keys=True))
    else:
        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print("wrote %s (signed with %s)" % (out_path, key_path))
    return 0


def cmd_verify(args) -> int:
    from steward import license as lic

    with open(args.file, encoding="utf-8") as fh:
        doc = json.load(fh)
    payload = lic.verify_envelope(doc)
    lic.validate_payload_shape(payload)  # force full per-license checks
    print("signature: OK")
    print("id:        %s" % payload["id"])
    print("tier:      %s" % payload["tier"])
    print("machine:   %s" % payload["machine"])
    print("issued:    %s" % payload["issued"])
    print("features:  %s" % ", ".join(payload["features"]))
    for pack in payload.get("packs") or []:
        print("pack:      %s v%d (%s)"
              % (pack["id"], pack["version"],
                 pack.get("expires") or "perpetual"))
    print("expires:   %s (absent = perpetual)" % payload.get("expires", "never"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mint_license.py", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gen-key", help="generate the vendor signing keypair")
    g.add_argument("--out", default=str(DEFAULT_KEY_PATH))
    g.add_argument("--force", action="store_true")

    m = sub.add_parser("mint", help="sign a license file")
    m.add_argument("--tier", choices=["free", "personal", "home", "power"],
                   default="personal")
    m.add_argument("--fingerprint", dest="fingerprint",
                   help="target machine fingerprint, e.g. sha256:<hex>")
    m.add_argument("--local", action="store_true",
                   help="target the fingerprint of this machine")
    m.add_argument("--expires", help="ISO date; absent means perpetual")
    m.add_argument("--id", help="license id (auto-generated if omitted)")
    m.add_argument("--issued", help="ISO issue date (default today)")
    m.add_argument("--feature", action="append",
                   help="extra feature beyond the tier grant (repeatable)")
    m.add_argument("--pack", action="append", default=None, metavar="ID",
                   help="attach a feature pack by its registered id (repeatable); "
                        "must be a pack known to the feature registry")
    m.add_argument("--pack-version", type=int, action="append", default=None,
                   metavar="N",
                   help="pack version per --pack, in order (default 1)")
    m.add_argument("--pack-expires", metavar="ISO",
                   help="expiry date applied to every pack minted in this run "
                        "('YYYY-MM-DD'); absent = perpetual")
    m.add_argument("--note", help="optional human note")
    m.add_argument("--key", help="path to the vendor private key (PEM)")
    m.add_argument("--out", help="output file; default prints to stdout")

    v = sub.add_parser("verify", help="verify a license file")
    v.add_argument("--file", required=True, help="path to the signed license")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "gen-key":
        return cmd_gen_key(args)
    if args.cmd == "mint":
        return cmd_mint(args)
    if args.cmd == "verify":
        return cmd_verify(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
