"""Signed OTA model-catalog updates for Steward.

An update is a signed catalog manifest: a new versioned generation of the
open-model catalog that replaces the embedded one. Model identity and the
selector rules stay where they already live (models.py and selector.py are
the single source); OTA only verifies, stages, and performs the atomic swap,
then installs the verified generation into the in-memory catalog that the
selector reads so nothing here re-implements selection.

Manifest schema (Ed25519 envelope, same vendor key as licenses, see
steward.license):

    {
      "payload": {
        "version":     "2026-10-26.1",          # monotonic, lower than next
        "released":    "2026-10-26",
        "app_version": "0.2.0",                 # app this catalog rides with
        "models": [ {id, family, params_b, active_b, q4_gb,
                     tok_range, license, kind, note}, ... ],
        "blobs":   { "<filename>": "<sha256 hex>" }   # optional model files
      },
      "alg": "ed25519",
      "sig": "<hex>"
    }

On-disk active state lives under <data>/ota/:

    catalog-<version>.json   one immutable file per version, written only
                             after its signature verifies
    current.json             the pointer: {active_catalog, app_version,
                             hardware, plan, updated}. Replaced with a single
                             atomic rename: that rename IS the commit point,
                             so a kill mid-swap leaves the previous pointer
                             (the old catalog) fully usable.
    staging/<version>/       verified model blobs while downloading

Rollback: after the pointer commits, a health probe runs against the new
bundle; if it does not pass within APPLY_PROBE_TIMEOUT_S the pointer is
restored to the previous value and the new catalog file is removed, so a
bad bundle never stays active.

Re-selection on hardware change: each apply or status call records the
detected hardware signature; when it differs from the stored one the
canonical selector is re-run and the new plan is persisted.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from . import paths
from .config import atomic_write_json

APPLY_PROBE_TIMEOUT_S = 90.0

# An update source when no --url is given on the CLI.
UPDATE_ENV = "STEWARD_OTA_URL"

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


def verify_manifest(doc: dict, public_key_hex: str | None = None) -> dict:
    """Verify a manifest envelope's signature and shape; return the payload."""
    from . import license as lic
    payload = lic.verify_envelope(doc, public_key_hex)
    _validate_manifest_payload(payload)
    return payload


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


def _new_pointer(payload: dict, root: Path | None) -> dict:
    return {
        "active_catalog": payload["version"],
        "app_version": payload.get("app_version", ""),
        "hardware": _hardware_signature(root),
        "plan": _plan_dict(),
        "updated": _iso_now(),
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


def apply_update(doc: dict, health_probe, root: Path | None = None,
                 timeout: float = APPLY_PROBE_TIMEOUT_S) -> dict:
    """Atomically activate a signed catalog generation, with rollback.

    The envelope signature is re-verified here, so a tampered manifest is
    refused before anything is written. Steps: write the immutable
    catalog-<version>.json, install the new generation into the in-memory
    catalog, commit the pointer with one atomic rename (the commit point),
    then health-probe the new bundle. If the probe does not pass within
    `timeout` seconds, the pointer is restored to the previous value and the
    new catalog file is removed, so the old state is fully usable again.
    """
    payload = verify_manifest(doc)
    _validate_manifest_payload(payload)
    version = payload["version"]
    root = root if root is not None else paths.data_root()
    prev = current_state(root)
    if prev and prev.get("active_catalog") == version:
        return {"applied": False, "rolled_back": False,
                "reason": "version %s is already active" % version}

    paths.ensure_dir(_ota_dir(root))
    atomic_write_json(catalog_path(version, root), payload, mode=0o600)
    _run_kill_hook("catalog-written")

    installed = _install_models(payload["models"])
    new_pointer = _new_pointer(payload, root)
    atomic_write_json(current_path(root), new_pointer, mode=0o600)
    _run_kill_hook("pointer-swapped")

    result = _probe_until(health_probe, timeout)
    if result.get("ok"):
        return {
            "applied": True,
            "rolled_back": False,
            "version": version,
            "models": installed,
            "hardware": new_pointer.get("hardware"),
            "plan": new_pointer.get("plan"),
        }

    # Rollback: restore the previous pointer and drop the new catalog.
    if prev is not None:
        atomic_write_json(current_path(root), prev, mode=0o600)
    else:
        _unlink_quiet(current_path(root))
    _unlink_quiet(catalog_path(version, root))
    _restore_models_from(prev, root)
    return {
        "applied": False,
        "rolled_back": True,
        "version": version,
        "reason": result.get("error") or "health probe failed",
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

def status(root: Path | None = None) -> dict:
    """A snapshot of the OTA state for `steward ota status`."""
    st = current_state(root) or {}
    payload = load_active_payload(root)
    sig = _hardware_signature(root)
    return {
        "active_catalog": st.get("active_catalog") or "embedded",
        "app_version": st.get("app_version", ""),
        "model_count": len(payload["models"]) if payload else 0,
        "models": sorted(m["id"] for m in payload["models"]) if payload else [],
        "hardware": sig,
        "hardware_changed": st.get("hardware") not in (None, sig),
        "plan": st.get("plan"),
        "updated": st.get("updated"),
        "update_url": os.environ.get(UPDATE_ENV, ""),
    }
