#!/usr/bin/env python3
"""Sign and verify SHA256SUMS release checksums with the vendor Ed25519 key.

The release publishes a SHA256SUMS file next to its assets. This tool signs
that file with the same vendor key that signs licenses and OTA manifests
(tools/mint_license.py gen-key), so a downloader who does not trust the host
can prove the checksums really came from the vendor: the signature is
checked against the Ed25519 public key that is embedded in the Steward
binary itself.

Usage (from the repo root):

    python3 tools/sign_release.py sign                # reads SHA256SUMS
    python3 tools/sign_release.py verify              # checks SHA256SUMS.sig

The key defaults to ~/.steward-keys/steward-signing.key, or the path in
$STEWARD_SIGNING_KEY. Signing refuses to run when the key file is missing;
verifying never needs the private key, only the embedded public key.

The signature is a detached raw 64-byte Ed25519 signature over the exact
bytes of the SHA256SUMS file, written to SHA256SUMS.sig (or --sig). Because
the signature covers the file bytes, presenting a different SHA256SUMS file
with a signature made for another one always fails verification.

This tool does not touch the license or manifest envelopes and does not
change their format.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

DEFAULT_KEY_DIR = Path.home() / ".steward-keys"
DEFAULT_KEY_PATH = DEFAULT_KEY_DIR / "steward-signing.key"
KEY_ENV = "STEWARD_SIGNING_KEY"

DEFAULT_SUMS = "SHA256SUMS"
ED25519_SIG_BYTES = 64

# Keep steward importable when run straight from a checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _key_path(arg: str | None) -> Path:
    env = os.environ.get(KEY_ENV, "")
    if arg:
        return Path(arg).expanduser()
    if env:
        return Path(env).expanduser()
    return DEFAULT_KEY_PATH


def _crypto():
    """Import the Ed25519 primitives or fail with a readable line."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
        return InvalidSignature, Ed25519PrivateKey, Ed25519PublicKey
    except ImportError:  # pragma: no cover - environment dependent
        sys.exit(
            "refusing: the cryptography package is not installed; "
            "install it with: uv run --with cryptography ... or pip install cryptography"
        )


def _vendor_public_hex() -> str:
    """The embedded vendor public key, read at call time so tests can pin it."""
    from steward import license as lic

    return lic.VENDOR_PUBLIC_KEY_HEX


def _sig_path_for(sums: Path, sig: str | None) -> Path:
    return Path(sig) if sig else Path(str(sums) + ".sig")


def cmd_sign(args) -> int:
    key_path = _key_path(args.key)
    if not key_path.is_file():
        print(
            "refusing: signing key not found at %s "
            "(create it with tools/mint_license.py gen-key; the private key "
            "stays outside the repo and is never committed)" % key_path,
            file=sys.stderr,
        )
        return 2
    sums = Path(args.sums)
    if not sums.is_file():
        print("refusing: %s not found" % sums, file=sys.stderr)
        return 2

    from steward import license as lic

    scalar = lic.private_bytes_from_pem(key_path.read_bytes())
    _InvalidSignature, Ed25519PrivateKey, _Pub = _crypto()
    pem_key = Ed25519PrivateKey.from_private_bytes(scalar)

    data = sums.read_bytes()
    sig = pem_key.sign(data)
    if len(sig) != ED25519_SIG_BYTES:
        print("refusing: internal error, signature is %d bytes" % len(sig),
              file=sys.stderr)
        return 2

    out = _sig_path_for(sums, args.sig)
    out.write_bytes(sig)
    print("signed %s (%d bytes)" % (sums.name, len(data)))
    print("wrote  %s (ed25519 detached signature, %d bytes)" % (out, len(sig)))
    print("public %s" % _vendor_public_hex())
    return 0


def cmd_verify(args) -> int:
    sums = Path(args.sums)
    sig_path = _sig_path_for(sums, args.sig)
    if not sums.is_file():
        print("refusing: %s not found" % sums, file=sys.stderr)
        return 2
    if not sig_path.is_file():
        print("refusing: signature file %s not found" % sig_path, file=sys.stderr)
        return 2
    sig = sig_path.read_bytes()
    if len(sig) != ED25519_SIG_BYTES:
        print(
            "refusing: %s is not a %d-byte ed25519 signature (got %d bytes)"
            % (sig_path.name, ED25519_SIG_BYTES, len(sig)),
            file=sys.stderr,
        )
        return 2

    pub_hex = _vendor_public_hex()
    _InvalidSignature, _Priv, Ed25519PublicKey = _crypto()
    public = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex))
    try:
        public.verify(sig, sums.read_bytes())
    except _InvalidSignature:
        print("signature: INVALID", file=sys.stderr)
        print(
            "refusing: %s does not verify against the embedded vendor "
            "public key %s (the file or the signature is tampered)" % (sig_path.name, pub_hex),
            file=sys.stderr,
        )
        return 1
    print("signature: OK")
    print("signer:    %s (embedded vendor public key)" % pub_hex)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sign_release.py", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, help_ in (
        ("sign", "sign the checksum file and write the .sig"),
        ("verify", "verify the .sig against the embedded public key"),
    ):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--key", help="vendor private key (PEM); defaults to "
                                      "~/.steward-keys/steward-signing.key or $STEWARD_SIGNING_KEY")
        sp.add_argument("--sums", default=DEFAULT_SUMS, help="checksum file to "
                                                             "sign/verify (default SHA256SUMS)")
        sp.add_argument("--sig", help="signature file (default <sums>.sig)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "sign":
        return cmd_sign(args)
    if args.cmd == "verify":
        return cmd_verify(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
