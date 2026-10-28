"""Signed OTA updates for Steward: the model catalog and the app itself.

An update is a signed manifest envelope. Schema v2 adds an `app` block so
one envelope carries both a new versioned generation of the open-model
catalog and the Steward application payload for this host; a v1 manifest
(no `app` block) verifies exactly as before. Model identity and the
selector rules stay where they already live (models.py and selector.py are
the single source); OTA verifies, stages, and performs the atomic swap for
both halves.

Manifest schema v2 (Ed25519 envelope, same vendor key as licenses, see
steward.license):

    {
      "payload": {
        "version":     "2026-10-26.1",          # catalog version, monotonic
        "released":    "2026-10-26",
        "app_version": "1.1.0",                 # app this catalog rides with
        "models": [ {id, family, params_b, active_b, q4_gb,
                     tok_range, license, kind, note}, ... ],
        "blobs":   { "<filename>": "<sha256 hex>" },   # optional model files
        "app": {                                 # optional, schema v2
          "version":            "1.1.0",         # semver, the app release
          "released":           "2026-10-28",
          "notes":              "What is new",   # or
          "notes_url":          "https://.../CHANGELOG.md",  # notes | notes_url
          "min_source_version": "1.0.0",         # oldest source that may install
          "payloads": {
            "linux-x86_64":   { "url": "steward_1.1.0_amd64.deb",
                                "sha256": "<64 hex>", "size": 12345678 },
            "linux-aarch64":  { ... },
            "darwin-x86_64":  { ... },
            "darwin-arm64":   { ... },
            "windows-x86_64": { ... }
          }
        }
      },
      "alg": "ed25519",
      "sig": "<hex>"
    }

`url` may be absolute (http/https) or relative; a relative url resolves
against the directory of the manifest source. Platform keys are the lower-
cased "<os>-<arch>" names normalized like detect(): linux-x86_64,
linux-aarch64, darwin-x86_64, darwin-arm64, windows-x86_64. They are
vendor-chosen in the manifest; this module matches the running platform
against the exact keys it carries.

On-disk active state lives under <data>/ota/:

    catalog-<version>.json   one immutable file per catalog version, written
                             only after its signature verifies
    app/<name>               immutable verified app payload files, one per
                             applied release (written only after their sha
                             verifies, first at staging then again at install)
    staging/<version>/       verified model blobs while downloading
    staging/app/             verified app payload while downloading (via
                             steward.download, resumable and cancelable)
    current.json             the pointer: {active_catalog, app_version,
                             hardware, plan, app, updated}. Replaced with a
                             single atomic rename: that rename IS the commit
                             point, so a kill mid-swap (between the app file
                             write and the pointer commit) leaves the previous
                             pointer - the old catalog AND the old app binary
                             - fully usable.

Rollback: after the pointer commits, a health probe runs against the new
bundle (for an app install it runs the new binary with `version` and
requires the expected version back); if it does not pass within
APPLY_PROBE_TIMEOUT_S the pointer is restored to the previous value, the
new catalog file is removed, and the new app binary is removed, so the old
app binary still answers.

Re-selection on hardware change: each apply or status call records the
detected hardware signature; when it differs from the stored one the
canonical selector is re-run and the new plan is persisted.

The update source comes from STEWARD_OTA_URL (override) or the released
config default (config.ota_url); consumers never have to set an env var
for a normal install. Daemon and CLI use the same check/apply functions
here, so the HTTP route and `steward update check/apply` behave identically.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from . import download as _download
from . import paths
from .config import atomic_write_json

APPLY_PROBE_TIMEOUT_S = 90.0

# An update source when no --url is given on the CLI.
UPDATE_ENV = "STEWARD_OTA_URL"
APP_DIR_NAME = "app"

# Test seam: {step_name: callable} run at checkpoints inside apply_update().
# A subprocess test uses it to kill the process mid-swap and prove the old
# state survives. Default empty; never set outside tests.
_kill_hooks: dict[str, Callable] = {}

MANIFEST_MODEL_KEYS = ("id", "family", "params_b", "active_b", "q4_gb",
                       "tok_range", "license", "kind")


class OtaError(RuntimeError):
    """Anything that stops an OTA update from being applied safely."""


class ManifestSignatureError(OtaError):
    """The manifest signature did not verify (tampered or wrong key)."""


class MalformedManifest(OtaError):
    """The manifest payload is missing required fields or has wrong types."""


class CatalogStateError(OtaError):
    """The active catalog on disk is corrupt or unreadable."""


class ChecksumError(OtaError):
    """A staged blob does not match the sha256 in the manifest."""


# ---------------------------------------------------------------------------
# Paths and small helpers
# ---------------------------------------------------------------------------

def _ota_dir(root: Path | None = None) -> Path:
    base = root if root is not None else paths.data_root()
    return base / "ota"


def catalog_path(version: str, root: Path | None = None) -> Path:
    return _ota_dir(root) / ("catalog-%s.json" % version)


def current_path(root: Path | None = None) -> Path:
    return _ota_dir(root) / "current.json"


def staging_dir(root: Path | None = None) -> Path:
    return _ota_dir(root) / "staging"


def _read_json(path: Path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data
    except (OSError, json.JSONDecodeError):
        return None


def _iso_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _run_kill_hook(step: str) -> None:
    hook = _kill_hooks.get(step)
    if hook is not None:
        hook()


# ---------------------------------------------------------------------------
# Catalog payload <-> models.Model mapping
# ---------------------------------------------------------------------------

def entry_to_model(entry: dict):
    """Convert a manifest model entry to a steward.models.Model."""
    from .models import Model
    missing = [k for k in MANIFEST_MODEL_KEYS if k not in entry]
    if missing:
        raise MalformedManifest("model entry missing field(s): %s" % ", ".join(missing))
    try:
        return Model(
            id=str(entry["id"]),
            family=str(entry["family"]),
            params_b=float(entry["params_b"]),
            active_b=float(entry["active_b"]),
            q4_gb=float(entry["q4_gb"]),
            tok_range=tuple(entry["tok_range"]),
            license=str(entry["license"]),
            kind=str(entry["kind"]),
            note=str(entry.get("note", "")),
        )
    except (TypeError, ValueError) as exc:
        raise MalformedManifest("bad model entry for %r: %s" % (entry.get("id"), exc)) from exc


def _model_to_entry(m) -> dict:
    return {
        "id": m.id,
        "family": m.family,
        "params_b": float(m.params_b),
        "active_b": float(m.active_b),
        "q4_gb": float(m.q4_gb),
        "tok_range": [float(m.tok_range[0]), float(m.tok_range[1])],
        "license": m.license,
        "kind": m.kind,
        "note": m.note,
    }


def embedded_catalog() -> dict:
    """The built-in catalog as a manifest payload (baseline / fallback)."""
    from .models import all_models
    from . import __version__
    return {
        "version": "embedded",
        "released": "",
        "app_version": __version__,
        "models": [_model_to_entry(m) for m in all_models()],
        "note": "built-in catalog of this Steward release",
    }


def _install_models(entries: list) -> list[str]:
    """Install a verified catalog generation into models.MODELS in place.

    models.py owns model identity; OTA swaps the generation it verifies into
    that dict so all_models() and the canonical selector see the new set with
    no rule duplication here. `entries` is the models list from a verified
    manifest payload.
    """
    from . import models as _models
    new = {}
    for entry in entries:
        model = entry_to_model(entry)
        new[model.id] = model
    _models.MODELS.clear()
    _models.MODELS.update(new)
    return list(new)


# ---------------------------------------------------------------------------
# Manifest verification
# ---------------------------------------------------------------------------

def _validate_app_block(app: dict) -> None:
    """Type-check the optional v2 `app` block of a manifest payload."""
    if not isinstance(app, dict):
        raise MalformedManifest("app block must be a JSON object")
    version = app.get("version")
    if not isinstance(version, str) or not version:
        raise MalformedManifest("app block must carry a version string")
    min_source = app.get("min_source_version")
    if min_source is not None and not isinstance(min_source, str):
        raise MalformedManifest("app min_source_version must be a string")
    has_notes = "notes" in app and isinstance(app["notes"], str) and bool(app["notes"])
    has_url = "notes_url" in app and isinstance(app["notes_url"], str) and bool(app["notes_url"])
    if not (has_notes or has_url):
        raise MalformedManifest("app block must carry a non-empty notes or notes_url string")
    for key in ("notes", "notes_url"):
        if key in app and not isinstance(app[key], str):
            raise MalformedManifest("app %s must be a string" % key)
    payloads = app.get("payloads")
    if payloads is not None:
        if not isinstance(payloads, dict) or not payloads:
            raise MalformedManifest("app payloads must be a non-empty object")
        for plat, pl in payloads.items():
            if not isinstance(pl, dict):
                raise MalformedManifest("app payload for %r must be an object" % (plat,))
            sha = pl.get("sha256")
            if not isinstance(sha, str) or len(sha) != 64 \
                    or any(c not in "0123456789abcdefABCDEF" for c in sha):
                raise MalformedManifest(
                    "app payload %r has a non-sha256 checksum" % (plat,))
            url = pl.get("url")
            if not isinstance(url, str) or not url:
                raise MalformedManifest(
                    "app payload %r is missing a url" % (plat,))
            size = pl.get("size")
            if size is not None and (not isinstance(size, int)
                                     or isinstance(size, bool) or size < 0):
                raise MalformedManifest(
                    "app payload %r has an invalid size" % (plat,))


def _validate_manifest_payload(payload: dict) -> None:
    if not isinstance(payload, dict):
        raise MalformedManifest("manifest payload must be a JSON object")
    version = payload.get("version")
    if not isinstance(version, str) or not version:
        raise MalformedManifest("manifest is missing a version string")
    models = payload.get("models")
    if not isinstance(models, list) or not models:
        raise MalformedManifest("manifest must carry a non-empty models list")
    for entry in models:
        if not isinstance(entry, dict):
            raise MalformedManifest("model entries must be JSON objects")
        entry_to_model(entry)
    blobs = payload.get("blobs", {})
    if blobs is not None:
        if not isinstance(blobs, dict):
            raise MalformedManifest("blobs must be a dict of name -> sha256 hex")
        for name, sha in blobs.items():
            if not isinstance(sha, str) or len(sha) != 64:
                raise MalformedManifest("blob %r has a non-sha256 checksum" % name)
    app = payload.get("app")
    if app is not None:
        _validate_app_block(app)


def verify_manifest(doc: dict, public_key_hex: str | None = None) -> dict:
    """Verify a manifest envelope's signature and shape; return the payload."""
    from . import license as lic
    payload = lic.verify_envelope(doc, public_key_hex)
    _validate_manifest_payload(payload)
    return payload


# ---------------------------------------------------------------------------
# Update source, versions, and platform matching (schema v2)
# ---------------------------------------------------------------------------

def resolve_source(env: dict | None = None) -> str:
    """The update source: STEWARD_OTA_URL override, else the released default.

    A consumer never sets an env var on a normal install; STEWARD_OTA_URL
    is only an override. Reads the live config so an operator can pin a
    different source in <data>/config.json.
    """
    env = os.environ if env is None else env
    override = (env.get(UPDATE_ENV) or "").strip()
    if override:
        return override
    from .config import DEFAULT_OTA_URL, Config
    cfg = Config.load(env)
    return (cfg.ota_url or "").strip() or DEFAULT_OTA_URL


def installed_app_version() -> str:
    """The app version this install reports today (package metadata)."""
    try:
        from . import __version__
        return str(__version__ or "0.0.0")
    except Exception:  # noqa: BLE001 - never fail a status over a version
        return "0.0.0"


def _semver_key(value) -> list | None:
    """Split a version into comparable tokens, or None when unparseable.

    "1.10.0-beta.1" -> [("n",1),("n",10),("n",0),("s","beta"),("n",1)].
    Returns None for empty or garbage so callers can fall back to a plain
    string inequality (never a silent crash on a vendor typo).
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._+\-]*$", text):
        return None
    out = []
    for part in re.split(r"[._+\-]", text):
        out.append(("n", int(part)) if part.isdigit() else ("s", part.lower()))
    return out


def _version_gt(a: str, b: str) -> bool:
    """True when app version a is newer than b (semver-ish).

    Unparseable inputs fall back to a != b so a downgrade never reports
    itself as available, while an unrecognized format still triggers a
    comparison the operator can see.
    """
    ka, kb = _semver_key(a), _semver_key(b)
    if ka is None or kb is None:
        return a != b
    for ta, tb in zip(ka, kb):
        if ta == tb:
            continue
        if ta[0] == tb[0]:
            return ta[1] > tb[1]
        return ta[0] == "n"  # numeric releases sort above pre-release labels
    return len(ka) > len(kb)


def _arch_normalize(arch: str) -> str:
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(arch, arch)


def platform_key(root: Path | None = None) -> str:
    """The '<os>-<arch>' key for this host, matching manifest payload keys."""
    from .detect import detect
    profile = detect(paths.data_root() if root is None else root)
    os_name = prof_os(profile)
    return "%s-%s" % (os_name, _arch_normalize(profile.arch).lower())


def prof_os(profile) -> str:
    os_name = (profile.os_name or "").lower()
    return {"macos": "darwin", "win32": "windows"}.get(os_name, os_name)


def current_app_state(root: Path | None = None) -> dict | None:
    """The `app` sub-object stored in the current pointer, if any."""
    st = current_state(root)
    if not st or not isinstance(st.get("app"), dict):
        return None
    return st["app"]


def app_binary_path(root: Path | None = None) -> str:
    """The on-disk path of the currently active app binary, or ''."""
    app = current_app_state(root)
    base = _ota_dir(root)
    if not app or not app.get("binary"):
        return ""
    path = base / APP_DIR_NAME / app["binary"]
    return str(path)


def _app_entry(app_block: dict, root: Path | None = None,
               platform: str | None = None) -> tuple[str | None, dict | None]:
    """(platform, payload-entry) for this host's payload in the app block."""
    payloads = app_block.get("payloads") or {}
    key = platform or platform_key(root)
    entry = payloads.get(key)
    if not isinstance(entry, dict):
        return None, None
    return key, entry


def _safe_basename(url: str) -> str:
    """The leaf filename of a payload url, refusing path escapes."""
    no_query = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    name = no_query.rsplit("/", 1)[-1]
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise MalformedManifest("app payload url has an unsafe filename: %r" % url)
    return name


def _resolve_payload_url(url: str, source: str | None) -> str:
    if url.startswith(("http://", "https://")):
        return url
    if not source:
        raise MalformedManifest(
            "cannot resolve relative app payload url %r without an update source" % url)
    if source.startswith(("http://", "https://")):
        base = source.rsplit("/", 1)[0]
        return base + "/" + _safe_basename(url)
    base = Path(source).resolve().parent
    return str(base / _safe_basename(url))


# ---------------------------------------------------------------------------
# Fetching (manifest and blobs), with an injectable fetcher for tests
# ---------------------------------------------------------------------------

def _is_local_source(source: str) -> bool:
    return not source.startswith(("http://", "https://"))


def _fetch_bytes(source: str, fetcher=None) -> bytes:
    if fetcher is not None:
        return fetcher(source)
    if _is_local_source(source):
        return Path(source).read_bytes()
    req = urllib.request.Request(source, headers={"User-Agent": "steward-ota/1"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


def fetch_document(source: str, fetcher=None) -> dict:
    """Fetch bytes and parse them as a manifest envelope (no crypto yet).

    Callers run verify_manifest() before trusting the document; check_for_update
    and apply_update do that for you.
    """
    data = _fetch_bytes(source, fetcher)
    if isinstance(data, bytes):
        import json as _json
        try:
            doc = _json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MalformedManifest("manifest source is not valid JSON: %s" % exc) from exc
    else:
        doc = data
    if not isinstance(doc, dict):
        raise MalformedManifest("manifest source is not a JSON object")
    return doc


def check_for_update(source: str, fetcher=None, root: Path | None = None) -> dict:
    """Fetch and verify a manifest, then report whether it is newer.

    Returns {ok, available, current_version, latest_version, released,
    model_count, reason, manifest}. available is True only when the verified
    manifest's version is higher than the active one.
    """
    payload = verify_manifest(fetch_document(source, fetcher))
    st = current_state(root) or {}
    current = (st.get("active_catalog") or "embedded")
    if current == "embedded":
        # An embedded catalog is not a persisted generation, so the state file
        # has no model list: report the built-in one rather than 0.
        st = dict(st)
        st["models"] = [m["id"] for m in embedded_catalog()["models"]]
    return {
        "ok": True,
        "available": current == "embedded" or payload["version"] != current,
        "current_version": current,
        "latest_version": payload["version"],
        "released": payload.get("released", ""),
        "model_count": len(payload["models"]),
        "reason": None,
        "manifest": payload,
    }


def check_update(source: str | None = None, fetcher=None,
                 root: Path | None = None) -> dict:
    """A full update check: the catalog and the app (schema v2).

    Verifies the manifest (a tampered one raises SignatureError before
    anything is reported, so a refusal is the state, never a silent pass).
    Returns {ok, state, catalog: {...}, app: {...}, available, source,
    manifest}. available is True when either half has a newer, installable
    version. The manifest's exact payload is included so callers can render
    notes or a changelog link.
    """
    src = source if source is not None else resolve_source()
    payload = verify_manifest(fetch_document(src, fetcher))
    st = current_state(root) or {}
    current_catalog = st.get("active_catalog") or "embedded"
    catalog = {
        "available": current_catalog == "embedded" or payload["version"] != current_catalog,
        "current_version": current_catalog,
        "latest_version": payload["version"],
        "model_count": len(payload["models"]),
        "released": payload.get("released", ""),
    }

    app = {
        "available": False,
        "current_version": current_app_version(root),
        "latest_version": None,
        "released": "",
        "notes": None,
        "notes_url": None,
        "min_source_version": None,
        "payload_for_platform": False,
        "reason": "manifest has no app block",
    }
    app_block = payload.get("app")
    if app_block is not None:
        key, entry = _app_entry(app_block, root)
        latest = app_block["version"]
        min_source = app_block.get("min_source_version")
        notes = app_block.get("notes")
        notes_url = app_block.get("notes_url")
        app.update({
            "latest_version": latest,
            "released": app_block.get("released", ""),
            "notes": notes,
            "notes_url": notes_url,
            "min_source_version": min_source,
            "payload_for_platform": entry is not None,
            "current_version": current_app_version(root),
        })
        warnings = []
        if entry is None:
            warnings.append("no payload for platform %s" % platform_key(root))
        elif not _version_gt(latest, app["current_version"]):
            warnings.append("already on the newest app version")
        if not _source_supports(min_source):
            warnings.append("requires source >= %s" % min_source)
        # available strictly means: newer than what we run AND installable here.
        app["available"] = (
            entry is not None
            and _version_gt(latest, app["current_version"])
            and _source_supports(min_source)
        )
        if not app["available"]:
            app["reason"] = "; ".join(warnings) or "not available"
    return {
        "ok": True,
        "state": "ok",
        "available": bool(catalog["available"] or app["available"]),
        "catalog": catalog,
        "app": app,
        "source": src,
        "checked_at": _iso_now(),
        "manifest": payload,
    }


def current_app_version(root: Path | None = None) -> str:
    """The app version this install is on: the pointer's, else package metadata."""
    app = current_app_state(root)
    if app and app.get("version"):
        return str(app["version"])
    return installed_app_version()


def _source_supports(min_source: str | None) -> bool:
    """Can this install run an app release gated by min_source_version?"""
    if not min_source:
        return True
    return not _version_gt(min_source, installed_app_version())


# ---------------------------------------------------------------------------
# Download to staging with checksum verification
# ---------------------------------------------------------------------------

def download_update(doc: dict, base_url: str = "", fetcher=None,
                    root: Path | None = None) -> dict:
    """Verify an envelope, then download every blob it lists into staging.

    The signature is checked here, so a tampered manifest cannot stage
    anything. base_url prefixes blob filenames. Returns {staged, staging_dir,
    bytes}. Without blobs there is nothing to download beyond the catalog.
    """
    payload = verify_manifest(doc)
    blobs = payload.get("blobs") or {}
    target = staging_dir(root) / payload["version"]
    paths.ensure_dir(target)
    total = 0
    for name, sha in sorted(blobs.items()):
        url = name if not base_url else base_url.rstrip("/") + "/" + name
        data = _fetch_bytes(url, fetcher)
        digest = hashlib.sha256(data).hexdigest()
        if digest != sha:
            raise ChecksumError(
                "sha256 mismatch for %s: expected %s, got %s" % (name, sha, digest)
            )
        out = target / name
        with open(out, "wb") as fh:
            fh.write(data)
        total += len(data)
    return {"staged": len(blobs), "staging_dir": str(target), "bytes": total}


def stage_app_payload(doc: dict, source: str | None = None, fetcher=None,
                      root: Path | None = None, progress: Callable | None = None,
                      cancel=None) -> dict:
    """Verify the envelope, then download the app payload for this host into
    staging using steward.download (sha256 enforced, resumable, cancelable).

    The manifest signature is verified first, so a tampered manifest cannot
    stage anything. The payload sha256 is enforced by download.fetch against
    the per-platform entry, so a tampered payload is refused at staging.
    Returns {staged, platform, version, path, filename, bytes} or a refusal
    {staged: False, reason}.
    """
    payload = verify_manifest(doc)
    app_block = payload.get("app")
    if app_block is None:
        return {"staged": False, "reason": "manifest has no app block"}
    key, entry = _app_entry(app_block, root)
    if entry is None:
        return {"staged": False,
                "reason": "no app payload for platform %s" % platform_key(root)}
    url = entry["url"]
    resolved = _resolve_payload_url(url, source)
    name = _safe_basename(url)
    target = staging_dir(root) / APP_DIR_NAME
    paths.ensure_dir(target)
    spec = _download.DownloadSpec(
        url=resolved,
        dest=target / name,
        expected_bytes=int(entry.get("size") or 0),
        sha256=entry["sha256"],
    )
    dest = _download.fetch(spec, progress=progress, cancel=cancel)
    # Re-verify the staged bytes: defense in depth, mirrors apply's own check.
    if hashlib.sha256(dest.read_bytes()).hexdigest() != entry["sha256"]:
        raise ChecksumError("staged app payload %s failed sha256 after download" % name)
    # A Steward payload is an executable; download.fetch creates files with the
    # default umask (0644), so make the verified staged copy owner-executable
    # to match the installed file's 0700 mode. This is what lets the health
    # probe run `staged_binary version` before the pointer commits.
    try:
        os.chmod(dest, 0o700)
    except OSError:
        pass  # chmod is best effort; a read-only filesystem still stages
    return {
        "staged": True,
        "platform": key,
        "version": app_block["version"],
        "path": str(dest),
        "filename": name,
        "bytes": dest.stat().st_size,
    }


# ---------------------------------------------------------------------------
# Active state, atomic swap, and rollback
# ---------------------------------------------------------------------------

def current_state(root: Path | None = None) -> dict | None:
    data = _read_json(current_path(root))
    return data if isinstance(data, dict) else None


def load_active_payload(root: Path | None = None) -> dict | None:
    """The active catalog payload (from the pointer), else None.

    The pointer references an immutable catalog-<version>.json file, so the
    active generation is always a complete, previously verified payload.
    """
    st = current_state(root)
    if not st or not st.get("active_catalog"):
        return None
    version = st["active_catalog"]
    path = catalog_path(version, root)
    if not path.is_file():
        raise CatalogStateError(
            "pointer references catalog %s but %s is missing" % (version, path)
        )
    payload = _read_json(path)
    if not isinstance(payload, dict) or not payload.get("models"):
        raise CatalogStateError("catalog %s on disk is corrupt" % version)
    return payload


def _hardware_signature(root: Path | None = None) -> str:
    from .detect import detect
    profile = detect(paths.data_root() if root is None else root)
    material = [
        profile.os_name,
        profile.arch,
        profile.gpu_kind,
        profile.gpu_name or "",
        "%.1f" % profile.serving_mem_gb,
    ]
    return "sha256:" + hashlib.sha256("|".join(material).encode("utf-8")).hexdigest()


def _plan_dict() -> dict:
    from .compat import verdict
    from .detect import detect
    from .selector import select_bundle
    p = detect()
    v = verdict(p, p.disk_free_gb)
    b = select_bundle(p, v)
    return {
        "class": b.class_name,
        "capability": b.capability.id,
        "assistant": b.assistant.id,
        "vision": b.vision,
        "serving_mem_gb": b.serving_mem_gb,
        "selected_at": _iso_now(),
    }


def _new_pointer(payload: dict, root: Path | None,
                 app_result: dict | None = None) -> dict:
    ptr = {
        "active_catalog": payload["version"],
        "app_version": payload.get("app_version", ""),
        "hardware": _hardware_signature(root),
        "plan": _plan_dict(),
        "updated": _iso_now(),
    }
    app_block = payload.get("app")
    if app_block is not None:
        if app_result is not None and app_result.get("installed"):
            # A real app swap: point at the newly verified binary.
            ptr["app"] = {
                "version": app_result["version"],
                "platform": app_result["platform"],
                "binary": app_result["binary"],
                "min_source_version": app_result.get("min_source_version"),
                "updated": _iso_now(),
            }
        elif app_result is not None and app_result.get("reason") == "already active":
            # A catalog-only apply with a manifest that carries the app we
            # already run: keep the existing app pointer intact.
            cur = current_app_state(root)
            if cur is not None:
                ptr["app"] = dict(cur)
    return ptr


def _atomic_copy(src: Path, dst: Path) -> None:
    """Copy src to dst atomically (tmp in the same dir + fsync + rename)."""
    paths.ensure_dir(dst.parent)
    fd, tmp = tempfile.mkstemp(dir=str(dst.parent), prefix=dst.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as out:
            with open(src, "rb") as fh:
                shutil.copyfileobj(fh, out, length=_download.CHUNK_SIZE)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, 0o700)
        os.replace(tmp, str(dst))
    except BaseException:
        _unlink_quiet(Path(tmp))
        raise


def _app_install_needed(payload: dict, root: Path | None) -> bool:
    """True when the manifest's app block has a newer payload for this host."""
    app_block = payload.get("app")
    if app_block is None:
        return False
    _, entry = _app_entry(app_block, root)
    if entry is None:
        return False
    cur = current_app_state(root)
    return cur is None or (cur.get("version") != app_block["version"]
                           and _version_gt(app_block["version"],
                                           cur.get("version") or installed_app_version()))


def _stage_app_install(payload: dict, root: Path | None) -> dict | None:
    """Move the verified staged app payload into place for the pointer commit.

    Returns None when the manifest has no app payload for this host, a
    fresh dict on a real install, and an {installed: False, reason} refusal
    when the version is already active or unsupported. The staged file is
    re-hashed here (defense in depth) before it is copied into the immutable
    app dir; this copy happens BEFORE the pointer rename, so a kill between
    this write and the commit leaves the previous pointer (the old app
    binary) fully usable.
    """
    app_block = payload.get("app")
    if app_block is None:
        return None
    key, entry = _app_entry(app_block, root)
    if entry is None:
        return {"installed": False, "platform": platform_key(root),
                "version": app_block["version"],
                "reason": "no payload for platform"}
    cur = current_app_state(root)
    if cur is not None and cur.get("version") == app_block["version"]:
        return {"installed": False, "platform": key,
                "version": app_block["version"], "reason": "already active"}
    if not _source_supports(app_block.get("min_source_version")):
        return {"installed": False, "platform": key,
                "version": app_block["version"],
                "reason": "requires source >= %s" % app_block.get("min_source_version")}
    name = _safe_basename(entry["url"])
    staged = staging_dir(root) / APP_DIR_NAME / name
    if not staged.is_file():
        raise MalformedManifest(
            "app payload %s is not staged; run the download step first" % name)
    if hashlib.sha256(staged.read_bytes()).hexdigest() != entry["sha256"]:
        raise ChecksumError("staged app payload %s fails its sha256; refusing" % name)
    app_dir = _ota_dir(root) / APP_DIR_NAME
    installed_path = app_dir / name
    _atomic_copy(staged, installed_path)
    _run_kill_hook("app-installed")
    return {
        "installed": True,
        "platform": key,
        "version": app_block["version"],
        "binary": installed_path.name,
        "min_source_version": app_block.get("min_source_version"),
        "path": str(installed_path),
    }


def _probe_until(health_probe, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    result = {"ok": False, "error": "health probe never ran"}
    while time.monotonic() < deadline:
        try:
            result = health_probe() or {}
        except Exception as exc:  # noqa: BLE001 - any probe failure is retried
            result = {"ok": False, "error": str(exc)}
        if result.get("ok"):
            return result
        time.sleep(0.1)
    return {"ok": False, "error": result.get("error") or "health probe failed"}


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _restore_models_from(st: dict | None, root: Path | None = None) -> None:
    """Rebuild the in-memory catalog from a previous pointer state."""
    if st and st.get("active_catalog") and st["active_catalog"] != "embedded":
        payload = load_active_payload_for_version(st["active_catalog"], root)
        if payload:
            _install_models(payload["models"])
            return
    _install_models(embedded_catalog()["models"])


def load_active_payload_for_version(version: str, root: Path | None = None) -> dict | None:
    path = catalog_path(version, root)
    if not path.is_file():
        return None
    payload = _read_json(path)
    return payload if isinstance(payload, dict) else None


def app_binary_probe(binary_path: str | None = None,
                     expect_version: str | None = None,
                     timeout: float = APPLY_PROBE_TIMEOUT_S):
    """A health probe for an app install: run a binary with `version`.

    `binary_path` defaults to the currently active app binary (from the
    pointer); `expect_version` defaults to the pointer's app version. The
    probe passes only when the binary runs and its `version` output matches
    the expected version, which is exactly what "the old binary still
    answers" means after a rollback. Never raises; a bad binary is a failed
    probe.
    """
    def probe() -> dict:
        exe = binary_path
        expect = expect_version
        if exe is None or expect is None:
            app = current_app_state(None)
            app = app or {}
            exe = exe or (app.get("binary") and str(
                _ota_dir() / APP_DIR_NAME / app["binary"])) or ""
            expect = expect or app.get("version")
        if not exe or not Path(exe).is_file():
            return {"ok": False, "error": "app binary is missing: %r" % (exe,)}
        try:
            run = subprocess.run([exe, "version"], capture_output=True,
                                 text=True, timeout=timeout, check=False)
            out = (run.stdout or "") + (run.stderr or "")
        except Exception as exc:  # noqa: BLE001 - a broken binary is a failed probe
            return {"ok": False, "error": "app binary did not answer: %s" % exc}
        out = out.strip()
        ok = bool(expect) and str(expect) in out
        return {"ok": ok, "version": out or "(no output)",
                "expected": str(expect or "")}
    return probe


def _auto_probe(app_result: dict | None):
    """Pick the strictest probe that applies: the app binary, else the backend."""
    if app_result is not None and app_result.get("installed"):
        return app_binary_probe(app_result.get("path"),
                                app_result.get("version"))
    return backend_health_probe()


def apply_update(doc: dict, health_probe=None, root: Path | None = None,
                 timeout: float = APPLY_PROBE_TIMEOUT_S) -> dict:
    """Atomically activate a signed catalog and/or app update, with rollback.

    The envelope signature is re-verified here, so a tampered manifest is
    refused before anything is written. Steps: write the immutable
    catalog-<version>.json, install the new generation into the in-memory
    catalog, move the verified staged app payload into the immutable app dir
    (when the manifest carries one for this host), commit the pointer with
    one atomic rename (the commit point), then health-probe the new bundle
    (for an app install: run the new binary and require its version). If the
    probe does not pass within `timeout` seconds, the pointer is restored to
    the previous value and the new catalog file and new app binary are
    removed, so the old state - and the old app binary - fully answer again.

    `health_probe` may be None: an app install then probes the new binary,
    anything else falls back to the backend probe.
    """
    payload = verify_manifest(doc)
    _validate_manifest_payload(payload)
    version = payload["version"]
    root = root if root is not None else paths.data_root()
    prev = current_state(root)
    if (prev and prev.get("active_catalog") == version
            and not _app_install_needed(payload, root)):
        return {"applied": False, "rolled_back": False,
                "reason": "version %s is already active" % version}

    paths.ensure_dir(_ota_dir(root))
    atomic_write_json(catalog_path(version, root), payload, mode=0o600)
    _run_kill_hook("catalog-written")

    installed = _install_models(payload["models"])

    app_result = _stage_app_install(payload, root)
    new_pointer = _new_pointer(payload, root, app_result)
    atomic_write_json(current_path(root), new_pointer, mode=0o600)
    _run_kill_hook("pointer-swapped")

    probe = health_probe if health_probe is not None else _auto_probe(app_result)
    result = _probe_until(probe, timeout)
    if result.get("ok"):
        return {
            "applied": True,
            "rolled_back": False,
            "version": version,
            "models": installed,
            "hardware": new_pointer.get("hardware"),
            "plan": new_pointer.get("plan"),
            "app": app_result,
        }

    # Rollback: restore the previous pointer and drop both new artifacts.
    if prev is not None:
        atomic_write_json(current_path(root), prev, mode=0o600)
    else:
        _unlink_quiet(current_path(root))
    _unlink_quiet(catalog_path(version, root))
    if app_result is not None and app_result.get("path"):
        _unlink_quiet(Path(app_result["path"]))
    _restore_models_from(prev, root)
    app_fail = None
    if app_result is not None:
        app_fail = {
            "installed": False,
            "platform": app_result.get("platform"),
            "version": app_result.get("version"),
            "reason": result.get("error") or "health probe failed",
        }
    return {
        "applied": False,
        "rolled_back": True,
        "version": version,
        "reason": result.get("error") or "health probe failed",
        "app": app_fail,
    }


def backend_health_probe(model_id: str | None = None):
    """A health probe for CLI apply: ask the configured backend if it answers.

    Uses the same runtime seam as the daemon, so this honors STEWARD_OLLAMA_URL
    and the system/sidecar auto-pick. A deeper selftest (one actual turn
    through the served model) can be passed instead for stricter rollback.
    """
    def probe() -> dict:
        from .config import Config
        from .runtime import make_runtime
        rt = make_runtime(Config.load())
        info = rt.health()
        if not info.get("ok"):
            return {"ok": False, "error": info.get("error") or "backend unreachable",
                    "url": getattr(rt, "url", "")}
        if model_id and model_id in (info.get("models") or []):
            try:
                list(rt.chat([{"role": "user", "content": "ping"}],
                              stream=False, model=model_id))
            except Exception as exc:  # noqa: BLE001 - selftest failure is a probe failure
                return {"ok": False, "error": "selftest failed: %s" % exc}
        return {"ok": True, "backend": info.get("backend") or rt.kind,
                "version": info.get("version"), "url": getattr(rt, "url", "")}
    return probe


def reselect_if_hardware_changed(root: Path | None = None) -> dict:
    """Re-run selection when the detected hardware changed; persist the plan.

    The pointer stores the hardware signature of the box at selection time.
    When the live signature differs (GPU swapped, RAM added, a new steady
    state) the canonical selector is re-run and the fresh plan is stored.
    """
    sig = _hardware_signature(root)
    st = current_state(root) or {}
    if st.get("hardware") == sig:
        return {"changed": False, "hardware": sig}
    plan = _plan_dict()
    merged = dict(st)
    merged.update({"hardware": sig, "plan": plan, "reselect_at": _iso_now()})
    atomic_write_json(current_path(root), merged, mode=0o600)
    return {"changed": True, "hardware": sig, "plan": plan}


# ---------------------------------------------------------------------------
# Status / reporting
# ---------------------------------------------------------------------------

def startup_check_status(source: str | None = None, root: Path | None = None) -> dict:
    """One best-effort update check with a recorded status (daemon boot / CLI).

    Never raises: an unreachable or tampered source records state=refused or
    state=error, so a daemon boot with a bad source is a normal start with
    a recorded status, never a failed unit.
    """
    src = source if source is not None else resolve_source()
    try:
        result = check_update(source=src, root=root)
        result["state"] = "ok"
    except Exception as exc:  # noqa: BLE001 - every source failure is a status
        result = {"ok": False, "state": "error", "error": str(exc), "source": src}
    result["checked_at"] = _iso_now()
    return result


def status(root: Path | None = None) -> dict:
    """A snapshot of the OTA state for `steward ota status`."""
    st = current_state(root) or {}
    payload = load_active_payload(root)
    sig = _hardware_signature(root)
    app = current_app_state(root)
    return {
        "active_catalog": st.get("active_catalog") or "embedded",
        "app_version": st.get("app_version", ""),
        "app": app,
        "model_count": len(payload["models"]) if payload else 0,
        "models": sorted(m["id"] for m in payload["models"]) if payload else [],
        "hardware": sig,
        "hardware_changed": st.get("hardware") not in (None, sig),
        "plan": st.get("plan"),
        "updated": st.get("updated"),
        "update_url": resolve_source(),
    }
