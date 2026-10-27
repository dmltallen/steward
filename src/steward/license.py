"""Steward perpetual offline license verification.

The business model in one module: a Steward install is free by default. To
unlock paid tiers the user drops a signed license file on the machine,
bought out of band. No subscription and no server round-trip after purchase;
the license file IS the product.

License file schema (JSON, envelope-signed Ed25519):

    {
      "payload": {
        "id":       "STW-AB12-CD34-...",
        "tier":     "free" | "personal" | "home" | "power",
        "machine":  "sha256:<64 hex>",      # fingerprint of the target box
        "issued":   "YYYY-MM-DD",
        "features": ["chat", "retrieval", "daily_brief", ...],
        "expires":  "YYYY-MM-DD"            # optional; absent = perpetual
      },
      "alg":  "ed25519",
      "sig":  "<hex signature over the canonical payload JSON>"
    }

verify_document() checks, in order: signature, machine fingerprint, expiry.
A bad signature, a wrong-machine fingerprint, or an expired license all
reject the file, so the feature gate can never pass a tampered grant and a
license for one box never runs on another.

Feature gating lives ONLY here. enabled() reads the signed features of the
active license and never consults config, so a free tier cannot enable a
paid feature through config alone. No active license = the free tier.

Crypto choice: Ed25519 via the `cryptography` package (OpenSSL-backed).
PyNaCl was considered and is equally correct; cryptography is used because
it is already present on the reference host, shares one backend, and has a
stable Ed25519 API. The import is lazy: if cryptography is absent the free
tier keeps working, and verification raises LicenseCryptoUnavailable with a
clear message instead of importing with a hard dependency.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import platform
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import paths

# ---------------------------------------------------------------------------
# Tiers and feature grants (single source of truth for gating)
# ---------------------------------------------------------------------------

TIERS = ("free", "personal", "home", "power")
TIER_ORDER = {tier: i for i, tier in enumerate(TIERS)}

# Canonical grant per tier. Free keeps chat, retrieval over one folder, and
# the daily brief. Paid tiers add unlimited retrieval roots, scheduled jobs
# beyond one, and tool exec; multi-device pairing is a Home-and-up feature
# (matches docs/PRICING.md where Home is the multi-device tier).
FREE_FEATURES = frozenset({"chat", "retrieval", "daily_brief"})
PAID_FEATURES = frozenset({"unlimited_retrieval_roots", "scheduled_jobs", "tool_exec"})
HOME_FEATURES = frozenset({"multi_device_pairing"})

TIER_FEATURES = {
    "free": FREE_FEATURES,
    "personal": FREE_FEATURES | PAID_FEATURES,
    "home": FREE_FEATURES | PAID_FEATURES | HOME_FEATURES,
    "power": FREE_FEATURES | PAID_FEATURES | HOME_FEATURES,
}

ALL_FEATURES = (
    FREE_FEATURES | PAID_FEATURES | HOME_FEATURES
)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LicenseError(ValueError):
    """Base class for anything that makes a license unacceptable."""


class LicenseCryptoUnavailable(LicenseError):
    """The cryptography package is not installed; no signature work is possible."""


class SignatureError(LicenseError):
    """The envelope signature does not match the embedded public key."""


class MachineMismatch(LicenseError):
    """The license targets a different machine than the one running."""


class ExpiredLicense(LicenseError):
    """The license carried an expiry date in the past."""


class MalformedLicense(LicenseError):
    """The license body is missing required fields or has wrong types."""


class FingerprintError(LicenseError):
    """No stable hardware identity could be read for a fingerprint."""


# ---------------------------------------------------------------------------
# Low-level Ed25519 helpers (cryptography package, lazy import)
# ---------------------------------------------------------------------------

def _ed25519():
    """Import the Ed25519 pieces we need, or raise a clear error."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
        return InvalidSignature, serialization, Ed25519PrivateKey, Ed25519PublicKey
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise LicenseCryptoUnavailable(
            "the cryptography package is not installed; signature "
            "verification is unavailable (the free tier still works). "
            "Install it with: uv run --with cryptography ... or pip install cryptography"
        ) from exc


def generate_keypair() -> tuple[bytes, str]:
    """Generate a vendor keypair. Returns (PEM private key bytes, public key hex)."""
    _InvalidSignature, serialization, Ed25519PrivateKey, _Pub = _ed25519()
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return pem, _public_hex(private.public_key())


def private_bytes_from_pem(pem: bytes) -> bytes:
    """Load a PEM private key and return its raw 32-byte scalar."""
    _InvalidSignature, serialization, _Priv, _Pub = _ed25519()
    private = serialization.load_pem_private_key(pem, password=None)
    return private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )


def _public_hex(public) -> str:
    _InvalidSignature, serialization, _Priv, _Pub = _ed25519()
    raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return raw.hex()


def _public_from_hex(public_key_hex: str):
    _InvalidSignature, _serialization, _Priv, Ed25519PublicKey = _ed25519()
    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))


def _canonical_bytes(payload: dict) -> bytes:
    """Deterministic canonical form of a payload, stable across Python versions."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_payload(payload: dict, private_bytes: bytes) -> bytes:
    """Sign a payload dict with the raw Ed25519 scalar; returns the signature."""
    _InvalidSignature, _serialization, Ed25519PrivateKey, _Pub = _ed25519()
    return Ed25519PrivateKey.from_private_bytes(private_bytes).sign(_canonical_bytes(payload))


def make_envelope(payload: dict, private_bytes: bytes) -> dict:
    """Wrap a payload in the signed envelope {payload, alg, sig}."""
    return {
        "payload": payload,
        "alg": "ed25519",
        "sig": sign_payload(payload, private_bytes).hex(),
    }


def verify_signature_bytes(payload: dict, sig_hex: str, public_key_hex: str | None = None) -> dict:
    """Verify a signature over a payload; returns the payload on success.

    Raises SignatureError on a bad signature and LicenseCryptoUnavailable if
    cryptography is missing. public_key_hex defaults to the embedded vendor key.
    """
    _InvalidSignature, _serialization, _Priv, _Pub = _ed25519()
    pub_hex = public_key_hex if public_key_hex is not None else VENDOR_PUBLIC_KEY_HEX
    try:
        sig = bytes.fromhex(sig_hex)
    except (TypeError, ValueError) as exc:
        raise SignatureError("signature is not hex") from exc
    public = _public_from_hex(pub_hex)
    try:
        public.verify(sig, _canonical_bytes(payload))
    except _InvalidSignature as exc:
        raise SignatureError("signature does not match the payload or the vendor key") from exc
    return payload


def verify_envelope(doc: dict, public_key_hex: str | None = None) -> dict:
    """Verify a whole envelope {payload, alg, sig} and return its payload."""
    if not isinstance(doc, dict):
        raise MalformedLicense("license file must be a JSON object")
    payload = doc.get("payload")
    sig = doc.get("sig")
    alg = doc.get("alg")
    if not isinstance(payload, dict):
        raise MalformedLicense("envelope payload must be a JSON object")
    if alg != "ed25519":
        raise MalformedLicense("unsupported signature algorithm: %r" % (alg,))
    if not isinstance(sig, str) or not sig:
        raise MalformedLicense("envelope is missing its signature")
    return verify_signature_bytes(payload, sig, public_key_hex)


# ---------------------------------------------------------------------------
# Vendor key and license model
# ---------------------------------------------------------------------------

# Steward vendor verification key (Ed25519 public key, 32 bytes as hex). The
# matching private key is never committed; it lives at
#   ~/.steward-keys/steward-signing.key   (outside the repo, chmod 0o600)
# and was generated with `tools/mint_license.py gen-key`. Custody: that key,
# not this one, is what signs every license and every OTA manifest shipped.
# This public constant is safe to embed in the package.
VENDOR_PUBLIC_KEY_HEX = "3c05e05ea195d286aa124d6687728a1d9954025b7cb7533f39ee977fd14631bd"

LICENSE_FIELDS = {"id", "tier", "machine", "issued", "features"}


@dataclass(frozen=True)
class License:
    """A verified license, normalized for the feature gate."""

    id: str
    tier: str
    machine: str
    issued: str
    features: frozenset
    expires: str | None = None

    @property
    def is_perpetual(self) -> bool:
        return self.expires is None

    def has_feature(self, feature: str) -> bool:
        return feature in self.features


def today_iso() -> str:
    return _dt.date.today().isoformat()


def new_license_id() -> str:
    """A readable vendor license id: STW-<12 uppercase base32 chars>."""
    import secrets
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no O, 0, I, 1
    raw = secrets.choice(alphabet) + "".join(
        secrets.choice(alphabet) for _ in range(11)
    )
    return "STW-" + raw


def tier_features(tier: str) -> frozenset:
    """The canonical feature grant for a tier."""
    if tier not in TIER_FEATURES:
        raise MalformedLicense("unknown tier: %r" % (tier,))
    return TIER_FEATURES[tier]


def all_features() -> frozenset:
    return ALL_FEATURES


def validate_payload_shape(payload: dict) -> License:
    """Type-check the payload fields and return a normalized License.

    Does not check the signature; call verify_envelope first.
    """
    missing = LICENSE_FIELDS - set(payload)
    if missing:
        raise MalformedLicense("license missing field(s): %s" % ", ".join(sorted(missing)))
    tier = payload["tier"]
    if tier not in TIER_FEATURES:
        raise MalformedLicense("unknown tier: %r" % (tier,))
    features = payload["features"]
    if not isinstance(features, list) or not all(isinstance(f, str) for f in features):
        raise MalformedLicense("features must be a list of strings")
    expires = payload.get("expires")
    if expires is not None and not isinstance(expires, str):
        raise MalformedLicense("expires must be an ISO date string")
    if not isinstance(payload.get("machine"), str):
        raise MalformedLicense("machine must be a fingerprint string")
    return License(
        id=str(payload["id"]),
        tier=tier,
        machine=payload["machine"],
        issued=str(payload["issued"]),
        features=frozenset(features),
        expires=expires,
    )


def verify_document(doc: dict, public_key_hex: str | None = None,
                    fingerprint: str | None = None, now: _dt.date | str | None = None) -> License:
    """Full verification of a license envelope: signature, machine, expiry.

    fingerprint overrides the machine fingerprint to check against (tests);
    defaults to the live machine. now overrides "today" (tests an expiry);
    pass an ISO date string or a datetime.date.
    """
    payload = verify_envelope(doc, public_key_hex)
    license = validate_payload_shape(payload)
    expected = fingerprint if fingerprint is not None else machine_fingerprint()
    if license.machine != expected:
        raise MachineMismatch(
            "license is bound to %s but this machine is %s"
            % (license.machine, expected)
        )
    if license.expires is not None:
        today = now if isinstance(now, str) else (now or _dt.date.today()).isoformat()
        if license.expires < today:
            raise ExpiredLicense(
                "license expired on %s (today %s)" % (license.expires, today)
            )
    return license


def verify_file(path: Path | str, **kwargs) -> License:
    """Read and fully verify a signed license file on disk."""
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    return verify_document(doc, **kwargs)


# ---------------------------------------------------------------------------
# Machine fingerprint
# ---------------------------------------------------------------------------

def _read_first(paths_to_try: list[Path]) -> list[str]:
    """Read the first readable ascii value from a list of candidate paths."""
    for p in paths_to_try:
        try:
            value = Path(p).read_text(encoding="ascii").strip()
        except OSError:
            continue
        if value:
            return ["%s:%s" % (Path(p).name, value)]
    return []


def _linux_sources() -> list[str]:
    out: list[str] = []
    out.extend(_read_first([Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")]))
    out.extend(_read_first([
        Path("/sys/class/dmi/id/product_uuid"),
        Path("/sys/class/dmi/id/board_serial"),
        Path("/sys/class/dmi/id/product_serial"),
    ]))
    if out:
        return out
    # Documented fallback: the first stable physical-ish MAC address.
    netdir = Path("/sys/class/net")
    try:
        ifaces = sorted(netdir.iterdir())
    except OSError:
        ifaces = []
    skip = ("lo", "dummy", "veth", "docker", "br-", "virbr", "tun", "tap", "vlan")
    for iface in ifaces:
        if any(iface.name.startswith(k) for k in skip):
            continue
        try:
            addr = (iface / "address").read_text(encoding="ascii").strip()
        except OSError:
            continue
        if addr and addr.lower() != "00:00:00:00:00:00":
            out.append("mac:" + addr.lower())
            break
    return out


def _darwin_sources() -> list[str]:
    import subprocess
    try:
        raw = subprocess.run(
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:  # noqa: BLE001 - any probe failure falls through to MAC
        raw = ""
    m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', raw)
    if m:
        return ["io-platform-uuid:" + m.group(1)]
    node = uuid.getnode()
    if node and (node >> 40) % 2 == 0:  # globally administered MAC bit
        mac = ":".join("%02x" % ((node >> (8 * i)) & 0xFF) for i in range(5, -1, -1))
        return ["mac:" + mac]
    return []


def _machine_id_sources() -> list[str]:
    system = platform.system().lower()
    if system == "linux":
        return _linux_sources()
    if system == "darwin":
        return _darwin_sources()
    return []


def machine_fingerprint(sources: list[str] | None = None) -> str:
    """sha256 of the stable hardware identity of this machine.

    Sources, in order (documented fallback): the OS machine-id, then a DMI
    product/board serial, then the first stable physical-ish MAC address.
    If none are available the fingerprint cannot be computed at all: we raise
    instead of silently hashing the hostname, which is too weak a binding.

    Deterministic: same box, same fingerprint, so a license minted with
    --local activates on the box it was made for. sources may be injected for
    tests; when None a fresh hardware read is used.
    """
    material = sources if sources is not None else _machine_id_sources()
    material = [s for s in material if s]
    if not material:
        raise FingerprintError(
            "no stable hardware identity is readable on this machine; cannot "
            "compute a machine fingerprint (free tier is unaffected)"
        )
    joined = "\x00".join(sorted(set(material)))
    return "sha256:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Activation, status, and the feature gate
# ---------------------------------------------------------------------------

ACTIVE_LICENSE_NAME = "active.json"


def license_path(root: Path | None = None) -> Path:
    base = root if root is not None else paths.data_root()
    return paths.dirs(base)["licenses"] / ACTIVE_LICENSE_NAME


def _atomic_write_json(path: Path, doc: dict) -> None:
    paths.ensure_dir(path.parent)
    payload = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def activate(path: Path | str, root: Path | None = None) -> License:
    """Verify a signed license file and store it atomically as active.json.

    Verification (signature, machine, expiry) happens first, so a tampered,
    wrong-machine, or expired file is refused and never stored. The verified
    original envelope (payload + signature) is what gets stored.
    """
    import json as _json
    with open(path, encoding="utf-8") as fh:
        doc = _json.load(fh)
    license = verify_document(doc)
    _atomic_write_json(license_path(root), doc)
    return license


def active_license(root: Path | None = None) -> License | None:
    """The currently active license, re-verified from disk each call.

    Returns None when there is no license or when the stored file fails any
    check (tampered on disk, copied from another machine, expired), in which
    case the free tier is in effect. Never trusts config.
    """
    target = license_path(root)
    if not target.is_file():
        return None
    try:
        return verify_file(target)
    except LicenseError:
        return None


def enabled(feature: str, root: Path | None = None) -> bool:
    """Is a feature enabled on this install? The single gate.

    Reads the signed features of the active license. With no license the free
    grant applies. The gate never consults config, so config alone cannot
    enable a paid feature.
    """
    license = active_license(root)
    if license is None:
        return feature in FREE_FEATURES
    return license.has_feature(feature)


def status(root: Path | None = None) -> dict:
    """A human- and machine-readable summary for `steward license status`."""
    target = license_path(root)
    license = active_license(root)
    if license is None:
        reason = "no active license"
        if target.is_file():
            try:
                verify_file(target)
                reason = "unknown"
            except LicenseError as exc:
                reason = "%s: %s" % (type(exc).__name__, exc)
        return {
            "active": False,
            "tier": "free",
            "license": None,
            "features": sorted(FREE_FEATURES),
            "reason": reason,
            "path": str(target),
            "machine": machine_fingerprint(),
        }
    return {
        "active": True,
        "tier": license.tier,
        "license": {"id": license.id, "issued": license.issued,
                    "perpetual": license.is_perpetual,
                    "expires": license.expires,
                    "machine": license.machine},
        "features": sorted(license.features),
        "reason": None,
        "path": str(target),
        "machine": license.machine,
    }
