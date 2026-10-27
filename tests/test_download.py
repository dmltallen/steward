"""Tests for the resumable, verified downloader (src/steward/download.py).

A real HTTP server with Range support runs on loopback and injects faults
(abort mid-body, 5xx, ignored-Range, 416) so resume, retry and restart paths
are exercised against actual sockets, not mocks. No steward module except
download is imported, and no server or port survives a run.
"""

from __future__ import annotations

import hashlib
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import steward.download as dl

BODY = bytes(range(256)) * 4096          # 1 MiB deterministic body
BODY_SHA = hashlib.sha256(BODY).hexdigest()


class _Fairies:
    """Fault switches a test toggles on the live server before each call."""

    def __init__(self) -> None:
        self.abort_after: int | None = None     # bytes served, then sever
        self.budget = 0                          # bytes served so far in this run
        self.fail_5xx: int = 0                   # number of 503s to emit first
        self.no_range: bool = False              # ignore Range, answer 200 full
        self.force_416: bool = False             # answer 416 to any Range
        self.requests = 0                        # total GET/HEAD requests served
        self.range_requests = 0                  # requests that carried Range
        self.head_requests = 0


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # noqa: A003 - quiet test server
        pass

    def do_HEAD(self) -> None:  # noqa: N802
        self.server.fair.head_requests += 1
        self._respond(head_only=True)

    def do_GET(self) -> None:  # noqa: N802
        self._respond(head_only=False)

    def _respond(self, head_only: bool) -> None:
        fair = self.server.fair
        path = self.path.split("?")[0]
        if path != "/blob.bin":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        fair.requests += 1
        body = self.server.body
        total = len(body)

        if fair.fail_5xx > 0:
            fair.fail_5xx -= 1
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        rng = self.headers.get("Range")
        start, end = 0, total - 1
        status = 200
        if rng:
            fair.range_requests += 1
            m = re.match(r"bytes=(\d+)-(\d*)", rng)
            if m:
                start = int(m.group(1))
                upper = m.group(2)
                end = int(upper) - 1 if upper else total - 1
            if fair.force_416 or start > total - 1:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % total)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if not fair.no_range:
                status = 206

        chunk = body[start:end + 1]
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, total))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        if head_only:
            return
        if fair.abort_after is not None:
            slice_len = min(len(chunk), max(fair.abort_after - fair.budget, 0))
            if slice_len:
                self.wfile.write(chunk[:slice_len])
                self.wfile.flush()
                fair.budget += slice_len
            self.close_connection = True
            return
        self.wfile.write(chunk)
        self.wfile.flush()


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.fair = _Fairies()
        super().__init__(("127.0.0.1", 0), _Handler)


@pytest.fixture
def fakeserver():
    server = _Server(BODY)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def spec(tmp_path: Path, server, *, sha: str = BODY_SHA,
         expected: int = len(BODY), name: str = "blob.bin") -> dl.DownloadSpec:
    return dl.DownloadSpec(
        url="http://127.0.0.1:%d/%s" % (server.server_address[1], name),
        dest=tmp_path / name,
        expected_bytes=expected,
        sha256=sha,
    )


class Recorder:
    """Progress callback that records every (done, expected) pair."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def __call__(self, done, expected) -> None:
        self.calls.append((int(done), int(expected)))


# -- happy path ---------------------------------------------------------------


def test_successful_download_writes_verified_file(fakeserver, tmp_path):
    s = spec(tmp_path, fakeserver)
    rec = Recorder()
    returned = dl.fetch(s, progress=rec, max_retries=2, backoff=0.01)
    assert returned == s.dest
    assert Path(s.dest).read_bytes() == BODY
    assert dl.verify_file(s.dest, BODY_SHA)
    assert not Path(str(s.dest) + ".part").exists()
    assert rec.calls and rec.calls[-1][0] == len(BODY)
    # a monotonic progress path
    sizes = [c[0] for c in rec.calls]
    assert sizes == sorted(sizes) and sizes[0] > 0


def test_already_verified_file_short_circuits(fakeserver, tmp_path):
    s = spec(tmp_path, fakeserver)
    Path(s.dest).write_bytes(BODY)
    before = fakeserver.fair.requests
    assert dl.fetch(s, max_retries=1) == s.dest
    assert fakeserver.fair.requests == before  # no request issued


def test_stale_unverified_dest_is_replaced(fakeserver, tmp_path):
    s = spec(tmp_path, fakeserver)
    Path(s.dest).write_bytes(b"junk that does not match")
    dl.fetch(s, max_retries=2, backoff=0.01)
    assert Path(s.dest).read_bytes() == BODY


# -- resume -------------------------------------------------------------------


def test_truncated_download_resumes_from_part(fakeserver, tmp_path):
    s = spec(tmp_path, fakeserver)
    part = Path(str(s.dest) + ".part")
    rec = Recorder()

    # First run: the server severs the socket after 300 KB.
    fakeserver.fair.abort_after = 300 * 1024
    with pytest.raises(dl.DownloadFailed):
        dl.fetch(s, progress=rec, max_retries=1, backoff=0.01)
    assert part.exists() and part.stat().st_size == 300 * 1024
    assert not s.dest.exists()

    # Healed server: the same fetch resumes from the .part and verifies.
    fakeserver.fair.abort_after = None
    fakeserver.fair.budget = 0
    dl.fetch(s, progress=rec, max_retries=2, backoff=0.01)
    assert Path(s.dest).read_bytes() == BODY
    assert not part.exists()
    # at least one request carried a Range header (the resume)
    assert fakeserver.fair.range_requests >= 1


def test_retry_backs_off_through_5xx_then_succeeds(fakeserver, tmp_path):
    s = spec(tmp_path, fakeserver)
    fakeserver.fair.fail_5xx = 2
    start = 0.0
    import time
    t0 = time.monotonic()
    dl.fetch(s, max_retries=5, backoff=0.05)
    elapsed = time.monotonic() - t0
    del start, elapsed
    assert fakeserver.fair.requests >= 3  # two 503s then the real transfer
    assert Path(s.dest).read_bytes() == BODY


def test_4xx_is_terminal_not_retried(fakeserver, tmp_path):
    s = spec(tmp_path, fakeserver)
    # Point at a URL the server answers 404 for.
    s = dl.DownloadSpec(url=s.url + "-missing", dest=s.dest,
                        expected_bytes=len(BODY), sha256=BODY_SHA)
    with pytest.raises(dl.DownloadFailed):
        dl.fetch(s, max_retries=3, backoff=0.01)
    assert not s.dest.exists()


# -- checksum and disk --------------------------------------------------------


def test_checksum_mismatch_rejected_no_final_file(fakeserver, tmp_path):
    s = spec(tmp_path, fakeserver, sha="0" * 64)
    part = Path(str(s.dest) + ".part")
    with pytest.raises(dl.ChecksumMismatch):
        dl.fetch(s, max_retries=2, backoff=0.01)
    assert not s.dest.exists()          # nothing claims success
    assert not part.exists()            # the bad .part is discarded


def test_disk_preflight_refuses_before_any_request(fakeserver, tmp_path, monkeypatch):
    s = spec(tmp_path, fakeserver)
    monkeypatch.setattr(
        "steward.download.free_disk", lambda path: int(len(BODY) * dl.DISK_FACTOR) - 1)
    before = fakeserver.fair.requests
    with pytest.raises(dl.DiskSpaceError):
        dl.fetch(s, max_retries=2, backoff=0.01)
    assert fakeserver.fair.requests == before  # refused before touching the net


def test_disk_preflight_passes_with_room(fakeserver, tmp_path, monkeypatch):
    s = spec(tmp_path, fakeserver)
    monkeypatch.setattr(
        "steward.download.free_disk", lambda path: int(len(BODY) * dl.DISK_FACTOR) + 10)
    dl.fetch(s, max_retries=2, backoff=0.01)
    assert Path(s.dest).read_bytes() == BODY


# -- cancel -------------------------------------------------------------------


def test_cancel_keeps_part_and_never_claims_success(fakeserver, tmp_path):
    s = spec(tmp_path, fakeserver)
    part = Path(str(s.dest) + ".part")
    scope = dl.CancelScope()
    fired = []

    def progress(done, expected):
        if expected and done >= 100 * 1024 and not fired:
            fired.append(True)
            scope.cancel()

    with pytest.raises(dl.Cancelled):
        dl.fetch(s, progress=progress, cancel=scope, max_retries=2, backoff=0.01)
    assert not s.dest.exists()          # the final name never appeared
    assert part.exists()                # the partial is kept for resume
    assert part.stat().st_size >= 100 * 1024

    # The same fetch call, cancel cleared, resumes to a verified file.
    scope.reset()
    dl.fetch(s, cancel=scope, max_retries=2, backoff=0.01)
    assert Path(s.dest).read_bytes() == BODY
    assert not part.exists()


# -- restart on non-resumable servers -----------------------------------------


def test_server_without_range_restarts_from_zero(fakeserver, tmp_path):
    s = spec(tmp_path, fakeserver)
    part = Path(str(s.dest) + ".part")
    # Seed a partial file bigger than zero so a Range request would be sent.
    part.write_bytes(BODY[: 200 * 1024])
    fakeserver.fair.no_range = True   # server answers 200 for Range too
    dl.fetch(s, max_retries=2, backoff=0.01)
    assert Path(s.dest).read_bytes() == BODY  # restarted, not duplicated
    assert not part.exists()


def test_416_restarts_file(fakeserver, tmp_path):
    s = spec(tmp_path, fakeserver)
    part = Path(str(s.dest) + ".part")
    # A stale .part that is newer than the server file triggers a 416.
    part.write_bytes(b"\x00" * (len(BODY) + 1))
    dl.fetch(s, max_retries=2, backoff=0.01)
    assert Path(s.dest).read_bytes() == BODY
    assert not part.exists()


# -- helpers ------------------------------------------------------------------


def test_human_bytes_and_disk_helpers(tmp_path, monkeypatch):
    assert dl.human_bytes(0) == "0 B"
    assert dl.human_bytes(5_000_000).endswith("MB")
    assert dl.human_bytes(5_000_000_000).endswith("GB")
    assert dl.sha256_of(Path(dl.__file__))  # hashes anything readable
    assert dl.verify_file(Path(dl.__file__), dl.sha256_of(Path(dl.__file__)))
    assert not dl.verify_file(Path(dl.__file__), "0" * 64)
    assert dl.verify_file(Path("definitely-missing"), "anything") is False
    # check_disk passes when the size is unknown and fails when overcrowded.
    dl.check_disk(0, tmp_path)
    dl.check_disk(1, tmp_path)
    monkeypatch.setattr("steward.download.free_disk",
                        lambda path: 9 * (1024 ** 3))
    with pytest.raises(dl.DiskSpaceError):
        dl.check_disk(10 * (1024 ** 3), tmp_path)  # needs 15 GB free, only 9 GB


def test_incomplete_read_is_retried_then_fails(fakeserver, tmp_path):
    """A body cut mid-stream must be treated as transient, never a crash."""
    s = spec(tmp_path, fakeserver)
    fakeserver.fair.abort_after = 50 * 1024
    with pytest.raises(dl.DownloadFailed):
        dl.fetch(s, max_retries=1, backoff=0.01)
    part = Path(str(s.dest) + ".part")
    assert part.exists() and part.stat().st_size == 50 * 1024
