"""Resumable, checksum-verified download for Steward (state 5 of the wizard).

fetch() pulls one file over HTTP into a ``<name>.part`` file next to the final
name, resumes an interrupted transfer from where the .part file ends via a
Range request, retries transient failures with capped exponential backoff,
verifies the assembled bytes against a known sha256, and only then renames the
.part to the final name. Cancellation (a callable or a threading.Event) stops
the transfer cleanly and keeps the .part file so a later run resumes; the
final name never appears until the checksum passes, so a cancelled or failed
run never leaves a file that claims success.

Disk preflight: fetch() refuses to start when free space on the destination
filesystem is below ``DISK_FACTOR`` (1.5) times the expected size, and repeats
the same check before the final verification read, because hashing the last
bytes itself needs a little room. This is the threshold Steward uses to say
"model too big for disk" with real numbers (wizard.py).

Server behaviors handled:
    206 Range resume          the normal resumed path
    200 to a Range request    the server ignored Range, a resume is impossible,
                              so the transfer restarts from zero
    416 Range unsatisfiable   the .part is newer/larger than the server file,
                              so the transfer restarts from zero
    transient failures        5xx, resets, timeouts: retry with backoff from
                              the current .part size

Import surface: stdlib only (hashlib, os, shutil, time, urllib, pathlib).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

CHUNK_SIZE = 1 << 20       # 1 MiB read chunks
DISK_FACTOR = 1.5          # required free disk = expected bytes * DISK_FACTOR
DEFAULT_MAX_RETRIES = 5
DEFAULT_BACKOFF_S = 1.0
DEFAULT_READ_TIMEOUT_S = 30.0
MAX_BACKOFF_S = 30.0


class DownloadError(Exception):
    """Base for every download failure."""


class Cancelled(DownloadError):
    """The transfer was cancelled by the caller. The .part file is kept."""


class ChecksumMismatch(DownloadError):
    """The downloaded bytes did not match the manifest sha256."""


class DiskSpaceError(DownloadError):
    """Not enough free space on the destination filesystem."""


class DownloadFailed(DownloadError):
    """The transfer gave up after exhausting its retries."""


class _RestartDownload(Exception):
    """Internal signal: the resume point is invalid, start the file over."""


@dataclass
class DownloadSpec:
    """One file to fetch: where from, how big, and what it must hash to."""

    url: str
    dest: Path           # final path; .part is written beside it
    expected_bytes: int  # 0 or negative means unknown to a plain GET server
    sha256: str          # lowercase hex digest the full file must match


class CancelScope:
    """A cancel token usable as both a callable and an Event-like object.

        scope = CancelScope()
        ... elsewhere ... scope.cancel()
        fetch(spec, cancel=scope)
    """

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def is_set(self) -> bool:
        return self._cancelled

    def __call__(self) -> bool:
        return self._cancelled

    def reset(self) -> None:
        self._cancelled = False


def human_bytes(n: int) -> str:
    """Render a byte count the way a person reads it (1.5 GB, 480 MB)."""
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            if unit == "B":
                return "%d B" % n
            return "%.1f %s" % (value, unit)
        value /= 1024.0
    return "%d B" % n


def free_disk(path) -> int:
    """Free bytes on the filesystem containing path, walking up to an existing
    ancestor (the destination directory may not exist yet)."""
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return int(shutil.disk_usage(probe).free)
    except OSError:
        return 0


def check_disk(expected_bytes: int, dest_dir, factor: float = DISK_FACTOR) -> None:
    """Raise DiskSpaceError when free disk is below expected * factor.

    expected_bytes <= 0 means the size is unknown to the caller, in which case
    no preflight is possible and the check passes silently.
    """
    if expected_bytes is None or expected_bytes <= 0:
        return
    needed = int(expected_bytes * factor)
    free = free_disk(dest_dir)
    if free < needed:
        raise DiskSpaceError(
            "Not enough free disk space. This file needs %s and Steward "
            "reserves 1.5x, so %s must be free; the drive has %s free. "
            "Next step: free up space on %s and run setup again."
            % (human_bytes(needed - free), human_bytes(needed),
               human_bytes(free), Path(dest_dir))
        )


def sha256_of(path) -> str:
    """Lowercase hex sha256 of a whole file."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_file(path, sha256: str) -> bool:
    """True when path exists and hashes to sha256 (case-insensitive)."""
    if not Path(path).is_file():
        return False
    try:
        return sha256_of(path).lower() == (sha256 or "").lower()
    except OSError:
        return False


def _cancel_requested(cancel) -> bool:
    if cancel is None:
        return False
    check = getattr(cancel, "is_set", None)
    if callable(check):
        return bool(check())
    if callable(cancel):
        return bool(cancel())
    return False


def _nearest_existing(path: Path) -> Path:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe


def _part_path(dest) -> Path:
    return Path(str(dest) + ".part")


def _head_info(url: str, timeout: float) -> tuple[int | None, bool]:
    """(Content-Length, Accept-Ranges) from a HEAD when the server allows it."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            length = resp.headers.get("Content-Length")
            length_int = int(length) if length and length.isdigit() else None
            ranges = (resp.headers.get("Accept-Ranges") or "").lower()
            return length_int, "bytes" in ranges
    except Exception:  # noqa: BLE001 - HEAD is advisory, GET below is authoritative
        return None, False


def _seed_digest(part: Path) -> hashlib._Hash:
    """Hash the bytes already in the .part file (a resume prefix or nothing)."""
    digest = hashlib.sha256()
    if part.exists() and part.stat().st_size > 0:
        with open(part, "rb") as fh:
            for chunk in iter(lambda: fh.read(CHUNK_SIZE), b""):
                digest.update(chunk)
    return digest


def _stream_one_pass(url: str, part: Path, offset: int, expected: int | None,
                     digest, progress: Callable | None, cancel,
                     timeout: float) -> None:
    """Fetch one HTTP response and append it to the .part file.

    Advances offset/digest as bytes land. Raises _RestartDownload when the
    server cannot continue from `offset` (no Range support, or 416).
    """
    if cancel is not None and _cancel_requested(cancel):
        raise Cancelled("download cancelled before this pass started")
    headers = {"Range": "bytes=%d-" % offset} if offset > 0 else {}
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 416:
            raise _RestartDownload("server reported the resume point is past the end "
                                   "of the file; restarting the download") from exc
        # 5xx and other HTTP errors flow to the caller, which retries 5xx with
        # backoff and wraps the rest as DownloadFailed.
        raise
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        raise  # transient, retried by the caller

    status = getattr(resp, "status", 200)
    if status == 416:
        resp.close()
        raise _RestartDownload("server reported the resume point is past the end "
                               "of the file; restarting the download")
    if status == 200 and offset > 0:
        # The server ignored Range: the body is the whole file again. Resuming
        # is impossible without duplicating bytes, so start the file over.
        resp.close()
        raise _RestartDownload("server did not honor the Range request; "
                               "restarting the download")

    # Learn the total from a 206 Content-Range header when we did not know it.
    cur_expected = expected
    if status == 206 and cur_expected in (None, 0):
        cr = resp.headers.get("Content-Range") or ""
        # Format: bytes START-END/TOTAL
        if "/" in cr and cr.rpartition("/")[2].isdigit():
            cur_expected = int(cr.rpartition("/")[2])

    mode = "ab" if (offset > 0 or (part.exists() and part.stat().st_size > 0)) else "wb"
    try:
        with open(part, mode) as fh:
            while True:
                if _cancel_requested(cancel):
                    raise Cancelled("download cancelled")
                chunk = resp.read(CHUNK_SIZE)
                if not chunk:
                    break
                fh.write(chunk)
                fh.flush()
                os.fsync(fh.fileno())
                digest.update(chunk)
                done = part.stat().st_size
                if progress is not None:
                    progress(done, cur_expected or done)
    finally:
        resp.close()


def fetch(spec: DownloadSpec, *, progress: Callable | None = None,
          cancel=None, max_retries: int = DEFAULT_MAX_RETRIES,
          backoff: float = DEFAULT_BACKOFF_S,
          timeout: float = DEFAULT_READ_TIMEOUT_S,
          factor: float = DISK_FACTOR) -> Path:
    """Download spec.url to spec.dest, verified against spec.sha256.

    Returns the final path on success. Never leaves a file named like the
    destination unless the checksum passed. Raises Cancelled, ChecksumMismatch,
    DiskSpaceError or DownloadFailed (the last only after retries run out).
    """
    dest = Path(spec.dest)
    part = _part_path(dest)
    _nearest_existing(dest.parent).mkdir(parents=True, exist_ok=True)

    # Already complete: an existing verified file short-circuits.
    if verify_file(dest, spec.sha256):
        return dest

    # A stale unverified final file must not claim success. Remove it so the
    # verified rename below is the only way a final name appears.
    if dest.exists():
        dest.unlink()

    expected = spec.expected_bytes if spec.expected_bytes and spec.expected_bytes > 0 else None
    if spec.expected_bytes and spec.expected_bytes > 0:
        check_disk(spec.expected_bytes, dest.parent, factor)

    while True:  # outer loop: full restarts after an invalid resume point
        try:
            return _fetch_attempt(spec, part, dest, expected, progress, cancel,
                                  max_retries, backoff, timeout, factor)
        except _RestartDownload:
            if part.exists():
                part.unlink()
            # loop again from zero


def _fetch_attempt(spec: DownloadSpec, part: Path, dest: Path,
                   expected: int | None, progress, cancel,
                   max_retries: int, backoff: float, timeout: float,
                   factor: float) -> Path:
    url = spec.url
    offset = part.stat().st_size if part.exists() else 0

    # A .part larger than the declared size is stale; restart the file.
    if expected is not None and offset > expected:
        part.unlink()
        offset = 0

    if expected is not None and offset == expected and part.exists():
        digest = _seed_digest(part)  # nothing left to fetch, verify what we have
    else:
        digest = _seed_digest(part)
        attempts = 0
        while True:
            try:
                _stream_one_pass(url, part, offset, expected, digest,
                                 progress, cancel, timeout)
            except _RestartDownload:
                raise
            except (Cancelled, ChecksumMismatch, DiskSpaceError):
                raise
            except DownloadFailed:
                raise
            except urllib.error.HTTPError as exc:
                # 5xx is transient and resumable; 4xx (401, 403, ...) is not.
                if 500 <= exc.code < 600 and attempts < max_retries:
                    attempts += 1
                    time.sleep(min(backoff * (2 ** (attempts - 1)), MAX_BACKOFF_S))
                    offset = part.stat().st_size if part.exists() else 0
                    continue
                raise DownloadFailed(
                    "server returned HTTP %d and retries did not help" % exc.code
                ) from exc
            except Exception as exc:
                # Every other interruption (connection reset, timeout, a body
                # that ends mid-stream: IncompleteRead and friends) is transient
                # and picks up again from the .part file's current size.
                if attempts >= max_retries:
                    raise DownloadFailed(
                        "gave up after %d attempts: %s" % (max_retries, exc)
                    ) from exc
                attempts += 1
                time.sleep(min(backoff * (2 ** (attempts - 1)), MAX_BACKOFF_S))
                offset = part.stat().st_size if part.exists() else 0
                continue
            break

    done = part.stat().st_size if part.exists() else 0
    if expected is not None and done != expected:
        raise DownloadFailed(
            "download ended at %d bytes but %d were expected; the source file "
            "changed size mid-transfer (%s)" % (done, expected, spec.url)
        )

    # Room for the final verification read, then hash it as one file.
    if expected is not None:
        check_disk(expected, dest.parent, factor)

    actual = digest.hexdigest()
    if actual.lower() != (spec.sha256 or "").lower():
        part.unlink()
        raise ChecksumMismatch(
            "checksum mismatch for %s: expected %s, downloaded %s. The .part "
            "file was discarded; setup resumes from a clean download."
            % (spec.url, spec.sha256, actual)
        )

    os.replace(part, dest)
    if progress is not None:
        progress(dest.stat().st_size, dest.stat().st_size)
    return dest
