"""Steward first-run wizard: one screen from zero to a paired assistant.

The wizard is a resumable state machine that takes a machine from "installed"
to "the assistant works and the phone knows how to reach it":

    preflight  probes the machine and backend, reports actionable failures
    verdict    computes the install-time compatibility verdict (S3 compat.py)
    explain    explains the chosen model bundle in plain English
    download   pulls the models Steward serves (resumable, sha256 verified)
               or reuses ones already cached
    runtime    makes sure a model backend is running (system or sidecar)
    selftest   one real chat + one real note summary through the model
    pair       prints the QR payload and pairing code for the phone
    done

Every state is resumable: before a state starts, the wizard records it as the
cursor in <data>/wizard-state.json, and it only advances the cursor after that
state's work completed durably. A crash mid-download leaves a .part file and
the cursor at "download"; the next `steward setup` picks up exactly there.

Failure handling is written for a normal person: no network, model too big
for disk, port already in use, backend already running, GPU visible but
Ollama not using it. Each condition gets a specific message plus one next
step, and never a traceback.

Two surfaces drive the same machine:
    `steward setup`           plain-text TUI, stdlib only, no curses
    POST /v1/setup/*          the web wizard contract served by the daemon
                              (S5 renders these when present)

Preflight and the pairing routes are loopback-only in the daemon because they
mutate machine state; the contract is documented in the daemon and in the
completion.

Import surface: steward modules plus stdlib. The backend seam is injected in
tests (tests/test_wizard.py) so the whole machine runs against a fake runtime
with no Ollama and no network beyond loopback.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import steward.download as dl
import steward.paths as paths
from steward.config import Config

STATES = ("preflight", "verdict", "explain", "download", "runtime",
          "selftest", "pair")
DONE = "done"
STATE_FILE = "wizard-state.json"
MANIFEST_FILE = "manifest.json"
MODEL_DIR = "models"

# Shared across Wizard instances per data root so a web /v1/setup/cancel can
# reach an in-flight download in another request's thread.
_ACTIVE_SCOPES: dict[str, dl.CancelScope] = {}
_ACTIVE_PROGRESS: dict[str, dict] = {}

DEFAULT_PAIR_PORT = 3877
SELFTEST_PROMPT_CHAT = "Reply with exactly the word hello."
SELFTEST_PROMPT_SUMMARY = (
    "These are notes from the machine Steward just got installed on. In two "
    "sentences or less, tell me the top upcoming item and any deadline."
)


class WizardError(Exception):
    """A setup-step failure with a human-readable message as the argument."""


class NoManifestError(WizardError):
    """A model must be downloaded but has no Steward-managed manifest entry."""


@dataclass
class StepResult:
    """What one state did: whether it succeeded and what a human should read."""

    state: str
    ok: bool
    message: str
    next: str | None   # the state to run next on success
    data: dict = field(default_factory=dict)


# -- UI -----------------------------------------------------------------------


class UI:
    """Sink for wizard output. Default write = no-op; subclasses print or buffer."""

    def emit(self, text: str) -> None:
        del text  # base sink does nothing

    def progress(self, done: int, expected: int) -> None:
        del done, expected  # base sink does nothing

    def confirm(self, prompt: str) -> bool:
        del prompt
        return True  # non-interactive by default: Steward never blocks on a prompt


class PrintUI(UI):
    """Writes straight to stdout, one line per emit."""

    def emit(self, text: str) -> None:
        print(text)

    def progress(self, done: int, expected: int) -> None:
        percent = 100.0 * done / expected if expected else 0.0
        print("\r  progress: %s of %s (%.1f%%)"
              % (dl.human_bytes(done), dl.human_bytes(expected), percent),
              end="", flush=True)


class BufferUI(UI):
    """Records every emitted line so a caller (tests, web routes) can read the
    transcript back."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def emit(self, text: str) -> None:
        self.lines.append(text)


# -- state persistence --------------------------------------------------------


def _root_for(cfg: Config) -> Path:
    if getattr(cfg, "data_dir", ""):
        return Path(cfg.data_dir).expanduser()
    return paths.data_root()


def _read_cursor(state_file: Path) -> str | None:
    try:
        with open(state_file, encoding="utf-8") as fh:
            data = json.load(fh)
        value = data.get("state")
        return value if value in STATES or value == DONE else None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _write_cursor(state_file: Path, state: str) -> None:
    payload = json.dumps({
        "version": 1,
        "state": state,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, indent=2, sort_keys=True) + "\n"
    paths.ensure_dir(state_file.parent)
    fd, tmp = tempfile.mkstemp(dir=str(state_file.parent),
                               prefix=state_file.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, str(state_file))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_manifest(models_dir: Path) -> dict:
    """Read <data>/models/manifest.json -> {"models": {id: {url, bytes, sha256}}}.

    A missing or corrupt manifest is an empty dict, never an error.
    """
    path = models_dir / MANIFEST_FILE
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    models = data.get("models") if isinstance(data, dict) else {}
    return models if isinstance(models, dict) else {}


def ollama_gpu_status(profile) -> dict:
    """Check whether a visible NVIDIA GPU is actually used by the backend.

    Scoped to discrete GPUs, where nvidia-smi reliably lists compute apps; on
    a unified pool (Apple, GB10) the serving memory is the whole pool and the
    check is skipped as not applicable. Never raises: reports 'unknown' instead.
    Only a definitive "GPU visible, ollama running as a compute app nowhere"
    marks uses_gpu=False, which the wizard surfaces as an actionable warning.
    """
    gpu_visible = profile.gpu_kind == "discrete" and bool(profile.gpu_name)
    if not gpu_visible:
        return {"gpu_visible": False, "uses_gpu": True, "note": None}
    try:
        import subprocess
        out = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,process_name",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0:
            return {"gpu_visible": True, "uses_gpu": "unknown",
                    "note": "could not read GPU compute apps"}
        rows = [r for r in out.stdout.splitlines() if r.strip()]
        if not rows:
            # No compute apps at all: a discrete GPU with nothing using it.
            return {"gpu_visible": True, "uses_gpu": False,
                    "note": "the NVIDIA GPU is visible but no model process is "
                            "using it (nvidia-smi shows zero compute apps)"}
        return {"gpu_visible": True, "uses_gpu": True, "note": None}
    except Exception:  # noqa: BLE001 - advisory check must not break setup
        return {"gpu_visible": True, "uses_gpu": "unknown",
                "note": "could not check GPU usage"}


def find_notes_source(cfg: Config, root: Path, override: str = "") -> Path | None:
    """Locate a folder of .md/.txt notes for the self-test summary.

    Candidates in order: an explicit --source, the configured retrieval dirs,
    the shipped demo/source folder, and <data>/notes. Returns the first that
    exists and contains at least one note file.
    """
    candidates: list[str] = []
    if override:
        candidates.append(override)
    candidates.extend(getattr(cfg, "retrieval_dirs", None) or [])
    candidates.append(str(Path(__file__).resolve().parents[2] / "demo" / "source"))
    candidates.append(str(root / "notes"))
    for candidate in candidates:
        path = Path(candidate)
        if not path.is_dir():
            continue
        try:
            if any(p.suffix.lower() in (".md", ".txt") and not p.name.startswith(".")
                   for p in path.iterdir() if p.is_file()):
                return path
        except OSError:
            continue
    return None


def summarize_notes(source_dir: Path) -> str:
    """Flat note text for the self-test summary prompt, newest first."""
    files = sorted(
        (p for p in source_dir.iterdir()
         if p.is_file() and p.suffix.lower() in (".md", ".txt")
         and not p.name.startswith(".")),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    blocks = []
    for f in files:
        blocks.append("## %s\n%s"
                      % (f.name, f.read_text(encoding="utf-8", errors="replace")))
    return "\n\n".join(blocks)


# -- the wizard ---------------------------------------------------------------


class Wizard:
    """The resumable setup state machine. One instance drives it top to bottom.

    Injection points (all optional) let tests run the whole machine without
    Ollama or network: backend (a runtime seam), daemon_factory (a way to get
    a pairing-capable server), manifest (download metadata dict).
    """

    def __init__(self, cfg: Config | None = None, *, env: dict | None = None,
                 state_file: Path | None = None, ui: UI | None = None,
                 backend=None, daemon_factory: Callable | None = None,
                 manifest: dict | None = None, host_ip: str | None = None,
                 port: int | None = None, source_dir: str | None = None,
                 model: str | None = None) -> None:
        self.cfg = cfg or Config.load(env)
        self.root = _root_for(self.cfg)
        self.state_file = state_file or (self.root / STATE_FILE)
        self.ui = ui or UI()
        self._backend = backend
        self._daemon_factory = daemon_factory
        self._manifest = manifest
        self.host_ip = host_ip
        self._port = port or _as_port(self.cfg.port) or DEFAULT_PAIR_PORT
        self.source_dir = source_dir
        self._model_override = model or (self.cfg.model or "") or None

        self.profile = None
        self.verdict_res = None
        self.bundle = None
        self.results: dict = {}
        self.transcript: list[str] = []
        self._owns_daemon = False
        self._owns_backend = False
        self._daemon_server = None
        self._daemon_thread: threading.Thread | None = None
        self._downloader = dl
        self._cancel_scope: dl.CancelScope | None = None
        self.last_message = ""
        self.served_model = self._model_override or ""
        self.selftest_results: dict = {}

    # -- cursor ---------------------------------------------------------------

    def cursor(self) -> str:
        return _read_cursor(self.state_file) or "preflight"

    def reset(self) -> None:
        try:
            self.state_file.unlink()
        except OSError:
            pass

    # -- plan -----------------------------------------------------------------

    def compute_plan(self) -> dict:
        """Detect + verdict + bundle, the whole plan behind the TUI and web."""
        from steward.compat import verdict
        from steward.detect import detect
        from steward.selector import select_bundle

        if self.profile is None:
            self.profile = detect(self.root)
        if self.verdict_res is None:
            self.verdict_res = verdict(self.profile, self.profile.disk_free_gb)
        if self.bundle is None:
            self.bundle = select_bundle(self.profile, self.verdict_res)
        b = self.bundle
        targets = self._target_models()
        needed = self._required_download_bytes(targets)
        return {
            "class": b.class_name,
            "verdict": self.verdict_res.state,
            "serving_mem_gb": b.serving_mem_gb,
            "capability": b.capability.id,
            "capability_q4_gb": b.capability.q4_gb,
            "assistant": b.assistant.id,
            "vision": b.vision,
            "served_model": self.served_model or b.assistant.id,
            "models_needed": [m.id for m in targets],
            "download_required_bytes": needed,
            "disk_free_gb": self.profile.disk_free_gb,
        }

    def _target_models(self) -> list:
        """Model catalog entries the wizard must make sure are resident.

        A pinned served model (STEWARD_MODEL / config.model / --model) is the
        only requirement; otherwise the bundle assistant and capability both
        must exist.
        """
        from steward.models import MODELS

        if self.bundle is None:
            self.compute_plan()
        if not self.bundle:
            return []
        if self.served_model:
            ids = [self.served_model]
        else:
            b = self.bundle
            ids = [b.capability.id]
            if b.assistant.id != b.capability.id:
                ids.append(b.assistant.id)
        out = []
        for mid in dict.fromkeys(ids):
            if mid in MODELS:
                out.append(MODELS[mid])
        return out

    def compute_plan_unless(self) -> bool:
        """Ensure plan state is computed once; True when the plan resolved."""
        self.compute_plan()
        return self.bundle is not None

    def _required_download_bytes(self, targets: list) -> int:
        """Bytes a fresh box must fetch for the targets not already cached."""
        if not targets:
            return 0
        resident = self._resident_set()
        needed = 0
        for model in targets:
            if model.id in resident:
                continue
            entry = self._manifest_entry(model.id)
            if entry:
                needed += int(entry.get("bytes") or 0)
            else:
                needed += int(model.q4_gb * (1024 ** 3))
        return needed

    # -- backend / manifest helpers -------------------------------------------

    def _resolve_backend(self):
        if self._backend is not None:
            return self._backend
        from steward.runtime import make_runtime

        backend = make_runtime(self.cfg)
        self._backend = backend
        if backend.kind == "sidecar-ollama":
            self._owns_backend = True
        return backend

    def _resident_set(self) -> set:
        """Model ids the backend already has resident, best effort."""
        try:
            health = self._resolve_backend().health()
            return set(health.get("models") or [])
        except Exception:  # noqa: BLE001 - best effort
            return set()

    def _manifest_entry(self, model_id: str) -> dict | None:
        if self._manifest is None:
            manifest = load_manifest(self.root / MODEL_DIR)
        elif isinstance(self._manifest, dict) and "models" in self._manifest:
            # accept either the on-disk shape {"models": {...}} or a direct
            # {id: {...}} mapping (tests inject the direct form)
            manifest = self._manifest.get("models")
        else:
            manifest = self._manifest
        entry = (manifest or {}).get(model_id) or {}
        return entry if entry.get("url") else None

    def _models_dir(self) -> Path:
        return paths.ensure_dir(self.root / MODEL_DIR)

    # -- step dispatcher ------------------------------------------------------

    def _handler(self, state: str) -> Callable[[], StepResult]:
        return getattr(self, "_on_" + state)

    def step(self, state: str) -> StepResult:
        """Run one state. Advances the saved cursor only on success."""
        result = self._handler(state)()
        if result.ok and result.next:
            _write_cursor(self.state_file, result.next)
        self.results[state] = result
        self.last_message = result.message
        self.transcript.append("== %s ==" % state)
        if result.message:
            self.transcript.extend(result.message.splitlines())
        return result

    def run(self, *, from_state: str | None = None, ui: UI | None = None) -> StepResult:
        """Drive the machine from the saved cursor to done. Returns the final
        StepResult; on failure the cursor stays on the failing state."""
        ui = ui or self.ui
        state = from_state or self.cursor()
        guard = 0
        while state != DONE:
            guard += 1
            if guard > len(STATES) + 4:
                raise WizardError("setup loop did not terminate at %s" % state)
            result = self.step(state)
            ui.emit("== %s ==" % result.state)
            if result.message:
                for line in result.message.splitlines():
                    ui.emit("  %s" % line)
            if not result.ok:
                return result
            state = result.next or DONE
        _write_cursor(self.state_file, DONE)
        return StepResult(DONE, True, "Steward is set up.", None, {"state": DONE})

    # -- state: preflight -----------------------------------------------------

    def _on_preflight(self) -> StepResult:
        from steward.compat import verdict
        from steward.detect import detect

        profile = detect(self.root)
        self.profile = profile
        v = verdict(profile, profile.disk_free_gb)
        self.verdict_res = v

        hard_failures: list[str] = []
        warnings: list[str] = []

        # 1) A machine that cannot serve a model at all.
        if v.state == "no-fit":
            hard_failures.append(
                "Steward cannot run on this machine. %s\n"
                "  Next step: install Steward on a machine with at least "
                "16 GB of usable memory." % v.min_hardware_sentence
            )

        # 2) Model too big for disk: refuse before any byte is written.
        plan = self.compute_plan()
        targets = self._target_models()
        required = self._required_download_bytes(targets)
        if required > 0:
            try:
                dl.check_disk(required, self.root)
            except dl.DiskSpaceError as exc:
                hard_failures.append(
                    "%s\n  Next step: free up space on the data drive "
                    "(%s) or move STEWARD_HOME to a larger drive."
                    % (exc, self.root)
                )

        # Backend status.
        backend = self._resolve_backend()
        health_ok = False
        try:
            health = backend.health()
            health_ok = bool(health.get("ok"))
        except Exception:  # noqa: BLE001 - preflight stays out of the weeds
            health = {}

        has_binary = bool(getattr(backend, "binary_path", None)) or _ollama_binary_present()

        if health_ok:
            warnings.append(
                "A model backend is already running and Steward will use it "
                "(%s)." % getattr(backend, "url", None) or "the local backend"
            )

        # 3) No network / no backend when fetching a model is actually required.
        needs_fetch = False
        resident = self._resident_set()
        for model in self._target_models():
            if model.id not in resident:
                needs_fetch = True
                break

        if not health_ok and not has_binary and needs_fetch:
            hard_failures.append(
                "No model backend is running, no Ollama binary was found on "
                "this machine, and Steward needs to download model weights "
                "over the network.\n  Next step: install Ollama "
                "(ollama.com/download), then run setup again."
            )

        # 4) GPU visible but Ollama not using it (warning, actionable).
        gpu = ollama_gpu_status(profile)
        if gpu.get("gpu_visible") and gpu.get("uses_gpu") is False:
            warnings.append(
                "A GPU is installed but the model process is not using it "
                "(nvidia-smi shows zero compute apps).\n"
                "  Next step: restart Ollama after setting "
                "OLLAMA_GPU_LAYERS=999 in its environment, then run setup again."
            )
        elif gpu.get("gpu_visible") and gpu.get("uses_gpu") == "unknown" and gpu.get("note"):
            warnings.append(gpu["note"])

        # 5) Port in use (early heads-up; the pair state enforces it).
        if self._port_in_use(self._port):
            warnings.append(
                "Port %d is already in use. If it is a Steward daemon, pairing "
                "will reuse it; otherwise set STEWARD_PORT to a free port."
                % self._port
            )

        if hard_failures:
            body = "\n".join("  - %s" % f for f in hard_failures)
            return StepResult(
                "preflight", False,
                "Preflight did not pass:\n%s"
                "\nFix the item above, then run setup again (it resumes where "
                "it stopped)." % body,
                None,
                {"verdict": v.state, "warnings": warnings, "plan": plan},
            )

        lines = [
            "Preflight passed.",
            "  machine:      %s %s, %s" % (profile.os_name, profile.arch,
                                          profile.cpu_name or "unknown"),
            "  serving mem:  %.1f GB (%s), verdict %s"
            % (v.serving_mem_gb, profile.gpu_kind, v.state),
            "  disk free:    %.1f GB at %s" % (plan["disk_free_gb"], self.root),
        ]
        for warning in warnings:
            lines.append("  note: %s" % warning.replace("\n", "\n        "))
        return StepResult("preflight", True, "\n".join(lines),
                          "verdict",
                          {"verdict": v.state, "warnings": warnings, "plan": plan})

    # -- state: verdict -------------------------------------------------------

    def _on_verdict(self) -> StepResult:
        from steward.compat import verdict
        from steward.detect import detect

        if self.profile is None:
            self.profile = detect(self.root)
        v = self.verdict_res or verdict(self.profile, self.profile.disk_free_gb)
        self.verdict_res = v
        label = {"no-fit": "cannot run", "lite": "light mode", "full": "full mode"}[v.state]
        lines = [
            "Compatibility verdict: %s." % label,
            "  %s" % v.min_hardware_sentence,
        ]
        for reason in v.reasons:
            lines.append("  - %s" % reason)
        return StepResult("verdict", True, "\n".join(lines), "explain",
                          {"verdict": v.state})

    # -- state: explain -------------------------------------------------------

    def _on_explain(self) -> StepResult:
        plan = self.compute_plan()
        b = self.bundle
        mode = {"no-fit": "cannot run", "lite": "light mode", "full": "full mode"}[plan["verdict"]]
        targets = self._target_models()
        needed = plan["download_required_bytes"]
        lines = [
            "What Steward will do, in plain English:",
            "  This machine runs Steward in %s (%.1f GB of usable memory for "
            "model weights)." % (mode, plan["serving_mem_gb"]),
            "  The workhorse model is %s (about %.1f GB at Q4) and the "
            "always-on assistant is %s." % (plan["capability"],
                                            plan["capability_q4_gb"],
                                            plan["assistant"]),
            "  Image understanding: %s." % ("on" if plan["vision"] else "off"),
        ]
        if self.served_model and self.served_model not in (plan["assistant"],
                                                           plan["capability"]):
            lines.append("  A specific model was requested and will be served: %s."
                         % self.served_model)
        resident = self._resident_set()
        have = [m.id for m in targets if m.id in resident]
        need = [m.id for m in targets if m.id not in resident]
        if not targets:
            lines.append("  No models need attention.")
        else:
            lines.append("  Models to make ready: %s." % ", ".join(
                "%s (already cached)" % mid if mid in have else mid for mid in
                [m.id for m in targets]))
        if needed:
            lines.append("  About %s will be downloaded (with headroom that "
                         "needs %s free)." % (dl.human_bytes(needed),
                                              dl.human_bytes(int(needed * dl.DISK_FACTOR))))
        else:
            lines.append("  No download is needed; the models are already "
                         "on this machine.")
        return StepResult("explain", True, "\n".join(lines), "download",
                          {"plan": plan})

    # -- state: download ------------------------------------------------------

    def _on_download(self) -> StepResult:
        plan = self.compute_plan()
        resident = self._resident_set()
        lines: list[str] = []
        pending: list[str] = []

        for model in self._target_models():
            if model.id in resident:
                lines.append("  %s is already cached in the backend. No download."
                             % model.id)
                continue
            dest = self._models_dir() / model.id
            if dl.verify_file(dest, self._manifest_entry(model.id).get("sha256", "")
                              if self._manifest_entry(model.id) else ""):
                lines.append("  %s blob is already on disk and verified." % model.id)
                continue
            entry = self._manifest_entry(model.id)
            if not entry:
                pending.append(model.id)
                continue
            url, sha256 = entry["url"], entry.get("sha256", "")
            expected = int(entry.get("bytes") or 0)
            scope = self._active_scope()
            scope.reset()  # clear a previous cancel so a resume can start
            _set_download_progress(model.id, expected, 0)
            try:
                dl.fetch(dl.DownloadSpec(url=str(url), dest=dest,
                                         expected_bytes=expected,
                                         sha256=str(sha256)),
                         progress=self._progress_cb(model.id),
                         cancel=scope)
                _clear_download_progress()
                lines.append("  %s downloaded and verified (%s)."
                             % (model.id, sha256[:12]))
            except dl.Cancelled as exc:
                return StepResult("download", False,
                                  "Download of %s stopped (%s). It will resume "
                                  "from where it left off the next time setup "
                                  "runs." % (model.id, exc),
                                  None, {"model": model.id, "cancelled": True})
            except dl.ChecksumMismatch as exc:
                return StepResult("download", False,
                                  str(exc) + "\n  Next step: run setup again; "
                                  "it will re-download from scratch.",
                                  None, {"model": model.id})
            except dl.DiskSpaceError as exc:
                return StepResult("download", False,
                                  str(exc) + "\n  Next step: free up space and "
                                  "run setup again; it resumes.",
                                  None, {"model": model.id})
            except dl.DownloadFailed as exc:
                return StepResult("download", False,
                                  "Download of %s failed after retries: %s\n"
                                  "  Nothing claims success (no final file was "
                                  "written). Next step: check the network and "
                                  "run setup again; it resumes from the .part "
                                  "file." % (model.id, exc),
                                  None, {"model": model.id})

        if pending:
            return StepResult(
                "download", False,
                "These models are not cached and have no Steward download "
                "manifest, so Steward cannot fetch them itself: %s.\n"
                "  Next step: run `ollama pull %s`, then run setup again."
                % (", ".join(pending), ", ".join(pending)),
                None, {"pending": pending})

        if lines:
            return StepResult("download", True, "\n".join(lines), "runtime",
                              {"models": [m.id for m in self._target_models()]})
        return StepResult("download", True, "  All models are ready.", "runtime",
                          {"models": [m.id for m in self._target_models()]})

    def _active_scope(self) -> dl.CancelScope:
        """A cancel scope shared across Wizard instances for the same data
        root, so a web /v1/setup/cancel can reach an in-flight download."""
        key = str(self.root)
        scope = _ACTIVE_SCOPES.get(key)
        if scope is None:
            scope = dl.CancelScope()
            _ACTIVE_SCOPES[key] = scope
        self._cancel_scope = scope
        return scope

    def cancel_download(self) -> bool:
        """Ask an in-flight download to stop. The .part file and cursor stay."""
        if self._cancel_scope is None:
            return False
        self._cancel_scope.cancel()
        return True

    def _progress_cb(self, model_id: str):
        ui = self.ui

        def progress(done: int, expected: int) -> None:
            ui.progress(done, expected)
            _set_download_progress(model_id, expected, done)

        return progress

    # -- state: runtime -------------------------------------------------------

    def _on_runtime(self) -> StepResult:
        backend = self._resolve_backend()
        try:
            health = backend.health()
        except Exception as exc:  # noqa: BLE001 - turned into a message
            health = {"ok": False, "error": str(exc)}
        if health.get("ok"):
            return StepResult(
                "runtime", True,
                "Model backend is serving: %s%s. Nothing to start."
                % (getattr(backend, "kind", "backend"),
                   " at %s" % getattr(backend, "url", None) or ""),
                "selftest", {"backend": getattr(backend, "kind", "unknown")})

        # Backend is down: try to start the bundled sidecar.
        if hasattr(backend, "start"):
            try:
                url = backend.start()
                return StepResult(
                    "runtime", True,
                    "No running backend was found, so Steward started the "
                    "bundled Ollama at %s." % url,
                    "selftest", {"backend": backend.kind, "url": url})
            except Exception as exc:  # noqa: BLE001 - specific failure messages
                text = str(exc)
                if "listens" in text.lower() or "port" in text.lower():
                    return StepResult(
                        "runtime", False,
                        "Could not start the bundled backend: %s\n"
                        "  Next step: stop whatever uses that port, or set "
                        "STEWARD_OLLAMA_BIN / OLLAMA_PORT to a free port."
                        % text, None, {})
                return StepResult("runtime", False,
                                  "Could not start the bundled backend: %s\n"
                                  "  Next step: install Ollama from "
                                  "ollama.com/download and run setup again."
                                  % text, None, {})
        return StepResult(
            "runtime", False,
            "No model backend is running and no bundled Ollama was found on "
            "this machine.\n  Next step: install Ollama from "
            "ollama.com/download, then run setup again.",
            None, {})

    # -- state: selftest ------------------------------------------------------

    def _on_selftest(self) -> StepResult:
        backend = self._resolve_backend()
        model = self.served_model or (self.bundle.assistant.id if self.bundle else "")
        model = model or "qwen3:8b"
        lines: list[str] = []

        # A cold-backed model can take more than the seam's default 8 s on the
        # long summary prompt; the first-run self-test gets a generous window.
        saved_timeout = getattr(backend, "timeout", None)
        try:
            if saved_timeout is not None:
                backend.timeout = 120.0
            return self._run_selftest(backend, model, lines)
        finally:
            if saved_timeout is not None:
                backend.timeout = saved_timeout

    def _run_selftest(self, backend, model: str, lines: list[str]) -> StepResult:
        # One chat through the real runtime seam.
        try:
            chat_reply = "".join(d["delta"] for d in backend.chat(
                [{"role": "user", "content": SELFTEST_PROMPT_CHAT}],
                stream=False, model=model))
        except Exception as exc:  # noqa: BLE001 - selftest failure is actionable
            return StepResult("selftest", False,
                              "The self-test chat failed: %s\n"
                              "  Next step: check the backend is responding "
                              "(steward doctor), then run setup again."
                              % exc, None, {})
        lines.append("  self-test chat (%s): %s"
                     % (model, _shorten(chat_reply, 140)))
        self.selftest_results["chat"] = chat_reply

        # One note summary through the same runtime.
        source = find_notes_source(self.cfg, self.root, self.source_dir or "")
        if source is None:
            lines.append("  note summary: no local notes folder was found, so "
                         "the summary step is skipped (chat passed).")
            self.selftest_results["summary"] = None
        else:
            notes = summarize_notes(source)
            try:
                summary = "".join(d["delta"] for d in backend.chat(
                    [{"role": "user", "content": SELFTEST_PROMPT_SUMMARY + "\n\n"
                                                + notes[:4000]}],
                    stream=False, model=model))
            except Exception as exc:  # noqa: BLE001
                return StepResult("selftest", False,
                                  "The self-test note summary failed: %s\n"
                                  "  Next step: check the backend, then run "
                                  "setup again." % exc, None, {})
            lines.append("  note summary: %s (from %s)"
                         % (_shorten(summary, 140), source))
            self.selftest_results["summary"] = summary

        return StepResult("selftest", True, "\n".join(lines), "pair",
                          {"model": model})

    # -- state: pair ----------------------------------------------------------

    def _on_pair(self) -> StepResult:
        model = self.served_model or (
            self.bundle.assistant.id if self.bundle else "")
        try:
            server = self._ensure_daemon(model)
        except OSError as exc:
            return StepResult(
                "pair", False,
                "Port %d is already in use by another program: %s\n"
                "  Next step: stop that program, or set a free port with "
                "STEWARD_PORT (or `steward setup --port N`), then run setup "
                "again." % (self._port, exc), None, {})
        code = server.ctx.pair_code if hasattr(server, "ctx") else self._external_code()
        host = self.host_ip or _lan_ip()
        port = self._port
        payload = "steward://pair?host=%s&port=%d" % (host, port)
        if not code:
            code = "provided by the running Steward daemon"
        lines = [
            "Pair your phone now:",
            "  QR payload: %s" % payload,
            "  pairing code: %s" % code,
            "  In the Steward phone app choose Pair, scan the QR (or type the "
            "code) and the phone connects over your local network.",
            "  To serve after this setup screen: run `steward serve` (keeps a "
            "live pairing code for the app).",
        ]
        if self._owns_daemon:
            lines.append(
                "  The code above is live for the daemon this setup screen "
                "started; after this screen it stops and `steward serve` "
                "prints a fresh one."
            )
        self.results["pair"] = {"payload": payload, "code": str(code),
                                "host": host, "port": port}
        return StepResult("pair", True, "\n".join(lines), DONE,
                          {"payload": payload, "code": str(code)})

    def _external_code(self) -> str:
        """Read the pairing code from a daemon already serving on the port."""
        try:
            import urllib.request
            with urllib.request.urlopen("http://127.0.0.1:%d/pair/code"
                                        % self._port, timeout=2) as resp:
                import json as _json
                data = _json.loads(resp.read().decode("utf-8"))
            return str(data.get("code", ""))
        except Exception:  # noqa: BLE001 - no external daemon reachable
            return ""

    def _port_in_use(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            return sock.connect_ex(("127.0.0.1", port)) == 0

    def _same_instance_on_port(self, port: int) -> bool:
        """True when a Steward daemon already answers on the port."""
        try:
            import urllib.error
            import urllib.request
            req = urllib.request.Request("http://127.0.0.1:%d/health" % port,
                                         method="GET")
            with urllib.request.urlopen(req, timeout=2) as resp:
                server = resp.headers.get("Server", "")
            return "StewardDaemon" in server
        except Exception:  # noqa: BLE001 - anything not answering is not ours
            return False

    def _ensure_daemon(self, model: str):
        """Return a daemon-like server exposing ctx.pair_code and host_ip.

        Order: a wizard-owned server if already started, else a Steward daemon
        already serving on the port, else start a fresh one (which the wizard
        owns and shuts down on finish unless keep_daemon is set).
        """
        if self._daemon_server is not None:
            return self._daemon_server
        if self._same_instance_on_port(self._port):
            return _ExternalDaemon(code=self._external_code())
        factory = self._daemon_factory or _default_daemon_factory
        backend = self._resolve_backend()
        server, thread = factory(
            node_host="0.0.0.0", port=self._port, runtime=backend,
            model=model or "qwen3:8b", host_ip=self.host_ip or _lan_ip())
        self._daemon_server = server
        self._daemon_thread = thread
        self._owns_daemon = True
        return server

    def shutdown(self) -> None:
        """Stop everything this wizard started. System backends are untouched."""
        if self._owns_daemon and self._daemon_server is not None:
            srv = self._daemon_server
            try:
                if hasattr(srv, "ctx") and hasattr(srv, "shutdown_server"):
                    port = getattr(srv.ctx, "port", "?")
                    srv.shutdown_server()
                    self.ui.emit(
                        "  setup daemon stopped (port %s was released)." % port)
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
            if self._daemon_thread is not None:
                self._daemon_thread.join(timeout=5)
            self._daemon_server = None
        if self._owns_backend and self._backend is not None:
            try:
                stop = getattr(self._backend, "shutdown", None)
                if stop is not None:
                    stop()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass


class _ExternalDaemon:
    """Minimal handle for a Steward daemon this wizard did not start."""

    def __init__(self, code: str) -> None:
        self.ctx = type("Ctx", (), {"pair_code": code, "port": None})()
        self.host_ip = _lan_ip()


def _default_daemon_factory(*, node_host: str, port: int, runtime, model: str,
                            host_ip: str | None) -> tuple:
    """Start a real StewardServer (no mDNS) and a serving thread. Returns
    (server, thread)."""
    from steward.server.daemon import create_server

    server = create_server((node_host, port), runtime=runtime, model=model,
                           advertise=False, host_ip=host_ip)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _lan_ip() -> str:
    try:
        from steward.server.mdns import lan_ip as _
        return _()
    except Exception:  # noqa: BLE001 - loopback fallback
        return "127.0.0.1"


def _ollama_binary_present() -> bool:
    """True when an Ollama binary exists somewhere the backend could start."""
    import shutil

    if shutil.which("ollama"):
        return True
    try:
        from steward.runtime import SIDECAR_SEARCH

        return any(
            os.path.exists(p) and os.access(p, os.X_OK) for p in SIDECAR_SEARCH
        )
    except Exception:  # noqa: BLE001 - absence is a fact, not an error
        return False


def _as_port(value) -> int | None:
    try:
        port = int(value)
        return port if 0 < port < 65536 else None
    except (TypeError, ValueError):
        return None


def _shorten(text: str, limit: int) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 3] + "..."


# -- download progress registry (reads by the web /v1/setup/status route) ------


def _set_download_progress(model: str, expected: int, done: int) -> None:
    percent = (100.0 * done / expected) if expected else 0.0
    _ACTIVE_PROGRESS["current"] = {
        "model": model,
        "expected_bytes": int(expected),
        "done_bytes": int(done),
        "percent": round(percent, 1),
        "state": "running" if done < expected else "done",
    }


def _clear_download_progress() -> None:
    _ACTIVE_PROGRESS.pop("current", None)


def current_download_info() -> dict | None:
    return _ACTIVE_PROGRESS.get("current")


# -- web wizard adapters (called by POST /v1/setup/* in the daemon) -------------
#
# Contract (published in the S10 completion):
#   POST /v1/setup/start  body {"resume": bool}
#                         continue from the saved cursor (default true);
#                         resume=false wipes the cursor and starts fresh
#   POST /v1/setup/step   body {}    run exactly the next pending state
#   POST /v1/setup/status body {}    current snapshot, no state change
#   POST /v1/setup/cancel body {}    cancel an in-flight download
#   POST /v1/setup/plan   body {}    idempotent plan read, no state change
#
# Every route returns the same envelope:
#   {"ok": bool, "state": str, "done": bool, "message": str,
#    "transcript": [str], "plan": {...} | null,
#    "download": null | {model, expected_bytes, done_bytes, percent, state},
#    "error": null | str}
# The routes are loopback-only in the daemon because they mutate machine state.


def make_wizard(env: dict | None = None, ui: UI | None = None, **kw) -> Wizard:
    """A Wizard bound to the standard env/config for the caller, with a
    transcript-buffering UI so the envelope can return the transcript."""
    cfg = Config.load(env)
    return Wizard(cfg, env=env, ui=ui or BufferUI(), **kw)


def _envelope(w: Wizard, error: str | None = None) -> dict:
    state = w.cursor()
    return {
        "ok": error is None,
        "state": state,
        "done": state == DONE,
        "message": w.last_message,
        "transcript": list(w.transcript),
        "plan": _plan_safe(w),
        "download": current_download_info(),
        "error": error,
    }


def _plan_safe(w: Wizard) -> dict:
    try:
        return w.compute_plan()
    except Exception as exc:  # noqa: BLE001 - plan is a report, never a crash
        return {"error": str(exc)}


def setup_plan(env: dict | None = None) -> dict:
    w = make_wizard(env)
    return {"ok": True, "state": w.cursor(), "plan": _plan_safe(w)}


def setup_start(env: dict | None = None, *, resume: bool = True) -> dict:
    w = make_wizard(env)
    if not resume:
        w.reset()
    result = w.step(w.cursor())
    del result
    return _envelope(w)


def setup_step(env: dict | None = None) -> dict:
    w = make_wizard(env)
    state = w.cursor()
    if state == DONE:
        return _envelope(w, "setup is already complete")
    result = w.step(state)
    if not result.ok:
        return _envelope(w)  # cursor stays on the failing state; message carries the fix
    return _envelope(w)


def setup_status(env: dict | None = None) -> dict:
    return _envelope(make_wizard(env))


def setup_cancel(env: dict | None = None) -> dict:
    w = make_wizard(env)
    scope = _ACTIVE_SCOPES.get(str(w.root))
    cancelled = False
    if scope is not None:
        scope.cancel()
        cancelled = True
    return {"ok": True, "state": w.cursor(), "cancelled": cancelled,
            "message": "cancel requested" if cancelled else "no download to cancel"}


# -- TUI: `steward setup` -------------------------------------------------------


def run_tui(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="steward setup",
        description="First-run wizard: preflight, verdict, explains the model "
                    "choice, downloads or reuses the model, starts the "
                    "backend, self-tests chat and notes, and prints the "
                    "phone pairing payload. Fully resumable.",
    )
    parser.add_argument("--model", default="", help="serve this specific model (uses it if cached)")
    parser.add_argument("--source", default="", help="folder of notes for the self-test summary")
    parser.add_argument("--port", type=int, default=0, help="daemon pairing port (default 3877)")
    parser.add_argument("--from", dest="from_state", default="",
                        help="start from a specific state (preflight, download, ...)")
    parser.add_argument("--keep-daemon", action="store_true",
                        help="leave the pairing daemon running after setup")
    parser.add_argument("--home", default="", help="STEWARD_HOME for this run")
    args = parser.parse_args(argv)
    return run_tui_options(model=args.model, source=args.source, port=args.port,
                           from_state=args.from_state,
                           keep_daemon=args.keep_daemon, home=args.home)


def run_tui_options(*, model: str = "", source: str = "", port: int = 0,
                    from_state: str = "", keep_daemon: bool = False,
                    home: str = "") -> int:
    """The `steward setup` TUI body. cli.py and `python -m steward.wizard`
    both land here; the flag names stay in sync with run_tui and cli.py."""
    env = dict(os.environ)
    if home:
        env["STEWARD_HOME"] = home
    cfg = Config.load(env)
    if port:
        cfg.port = port

    ui = PrintUI()
    w = Wizard(cfg, env=env, ui=ui, port=port or None,
               source_dir=source or None, model=model or None)

    ui.emit("Steward setup")
    ui.emit("=============")
    ui.emit("Sets up Steward on this machine in one pass: what the box can "
            "run, the model it will serve, any download, a self-test, and "
            "phone pairing.")
    cursor = w.cursor()
    if cursor == DONE:
        ui.emit("Setup already completed for %s. To start over, run with "
                "--from preflight." % w.root)
        return 0
    if cursor != "preflight":
        ui.emit("Resuming from state: %s" % cursor)
    else:
        ui.emit("Data root: %s" % w.root)
    ui.emit("")

    try:
        result = w.run(ui=ui, from_state=from_state or None)
    except KeyboardInterrupt:
        w.cancel_download()
        ui.emit("")
        ui.emit("Setup interrupted (Ctrl-C). It stopped cleanly at state %s; "
                "the .part file (if any) is kept, and nothing claims success."
                % w.cursor())
        ui.emit("Run `steward setup` again to resume from here.")
        w.shutdown()
        return 130
    except Exception as exc:  # noqa: BLE001 - never a reachable traceback
        ui.emit("")
        ui.emit("Setup hit an unexpected error (%s). The cursor is saved at "
                "%s; running `steward setup` again resumes from there." % (exc, w.cursor()))
        w.shutdown()
        return 1

    ui.emit("")
    if not result.ok:
        ui.emit("Setup did not finish. It stopped at state '%s'." % w.cursor())
        ui.emit("Fix the point above and run `steward setup` again; it "
                "resumes where it stopped.")
        w.shutdown()
        return 1

    ui.emit("Setup complete. Steward is ready on this machine.")
    pair = w.results.get("pair")
    if pair and pair.data.get("payload"):
        ui.emit("  QR payload : %s" % pair.data["payload"])
        ui.emit("  code shown : %s" % pair.data["code"])
    if not keep_daemon:
        ui.emit("The pairing daemon started for this screen was stopped to "
                "leave nothing running.")
        ui.emit("When you want to pair for real: `steward serve` shows a "
                "fresh code; the app pairs over your local network.")
    else:
        ui.emit("The pairing daemon was left running in the background so you "
                "can pair right away.")
    return 0


if __name__ == "__main__":
    sys.exit(run_tui())
