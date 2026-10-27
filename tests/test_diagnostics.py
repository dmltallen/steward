"""Tests for steward.diagnostics: the redacted support bundle and log rotation.

The bundle path is exercised end to end against a throwaway STEWARD_HOME full
of planted secrets (a session token, a Bearer value, an sk-* key, a license
key, a fake user name). The assertions that matter for support safety are:
the zip member list is exactly what a stranger expects, and none of the
planted secrets or the fake user name survive into any member, including a
token embedded mid-sentence and home paths hashed to short digests.

Rotation is tested at a small cap for the exact-shape checks and at the real
5 MB cap for the size behavior.
"""

from __future__ import annotations

import json
import re
import socket
import zipfile

from steward.diagnostics import (
    BUNDLE_MEMBERS,
    Redactor,
    build_bundle,
    rotate_log,
    steward_log_path,
)

TOKEN = "sv1.1893456000.XWRzZWNyZXQ.Ym9ndXM.X2ZhZmFrZQ"
LIC = "STEWARD-LIC-AAAA-BBBB-CCCC-DDDD"
BEARER_VALUE = "xYzAbCdEf1234567890AbCdEfGhIjKlMnOpQrStUvWxYz"
SK = "sk-ant-faketokenvalues0321098"
USER = "victimuser"
REAL_HOME_PATTERN = re.compile(r"/home/[0-9a-f]{12}")


class FakeRuntime:
    """Deterministic stand-in for the backend seam; never touches a socket."""

    kind = "system-ollama"

    @staticmethod
    def health() -> dict:
        return {"ok": True, "version": "9.9.9-fake", "models": ["qwen3:8b"]}


def _env(root) -> dict:
    return {"STEWARD_HOME": str(root)}


def _make_root(tmp_path):
    root = tmp_path / "data"
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps({
        "port": 3877, "model": "", "data_dir": f"/home/{USER}/.steward",
        "ollama_url": "http://localhost:11434", "ollama_bin": "",
        "telemetry": False, "allow_shell": False,
        "retrieval_dirs": [f"/home/{USER}/notes"],
    }))
    log = root / "logs" / "steward.log"
    log.write_text(
        "info: daemon started, backend ok\n"
        f"warn: the client presented a session token {TOKEN} mid-sentence "
        "but it was rejected\n"
        f"info: Authorization: Bearer {BEARER_VALUE} received\n"
        f"error: license validation failed for key={LIC}\n"
        f"error: api_key={SK} at /home/{USER}/secretdir/plan.txt\n"
    )
    return root


def _member_text(archive: zipfile.ZipFile, name: str) -> str:
    return archive.read(name).decode("utf-8", errors="replace")


def _build(tmp_path):
    root = _make_root(tmp_path)
    out = tmp_path / "out" / "support.zip"
    build_bundle(out, root=root, runtime=FakeRuntime(), env=_env(root))
    return root, out


# -- bundle structure ----------------------------------------------------------


def test_bundle_members_and_zip_valid(tmp_path):
    _, out = _build(tmp_path)
    assert out.exists()
    with zipfile.ZipFile(out) as archive:
        assert archive.testzip() is None          # zip itself is intact
        assert archive.namelist() == list(BUNDLE_MEMBERS)


def test_bundle_members_have_expected_shapes(tmp_path):
    _, out = _build(tmp_path)
    with zipfile.ZipFile(out) as archive:
        system = _member_text(archive, "system.txt")
        assert "Serving memory:" in system and "verdict" in system
        bundle = json.loads(_member_text(archive, "bundle.json"))
        assert {"class", "capability", "assistant", "verdict"} <= set(bundle)
        versions = _member_text(archive, "versions.txt")
        assert versions.startswith("steward ")
        assert "python " in versions and "ollama " in versions
        index = json.loads(_member_text(archive, "index-stats.json"))
        assert "documents" in index
        license_state = _member_text(archive, "license-state.txt")
        assert "No license keys or blobs are included" in license_state


# -- redaction -----------------------------------------------------------------


def test_bundle_never_contains_planted_secrets(tmp_path):
    _, out = _build(tmp_path)
    secrets = (TOKEN, LIC, BEARER_VALUE, SK, USER)
    with zipfile.ZipFile(out) as archive:
        for name in archive.namelist():
            text = _member_text(archive, name)
            for secret in secrets:
                assert secret not in text, f"{name} leaked {secret!r}"


def test_token_embedded_mid_sentence_is_redacted(tmp_path):
    _, out = _build(tmp_path)
    with zipfile.ZipFile(out) as archive:
        log = _member_text(archive, "recent-errors.log")
    assert TOKEN not in log
    assert "[REDACTED]" in log
    assert "session token" in log          # prose context survives


def test_bearer_and_sk_values_redacted(tmp_path):
    _, out = _build(tmp_path)
    with zipfile.ZipFile(out) as archive:
        log = _member_text(archive, "recent-errors.log")
    assert BEARER_VALUE not in log
    assert SK not in log
    assert "[REDACTED]" in log
    assert "received" in log           # prose context survives


def test_redact_json_blanks_secret_keys_and_hashes_home():
    redactor = Redactor()
    out: dict = redactor.redact_json_value({
        "api_key": SK,
        "license": LIC,
        "data_dir": f"/home/{USER}/.steward",
        "ollama_url": "http://localhost:11434",
        "port": 3877,
    })
    assert out["api_key"] == "[REDACTED]"
    assert out["license"] == "[REDACTED]"
    assert out["ollama_url"] == "http://localhost:11434"   # not a secret
    assert out["port"] == 3877
    assert USER not in out["data_dir"]
    assert REAL_HOME_PATTERN.search(out["data_dir"])


def test_home_paths_hashed_in_bundle(tmp_path):
    _, out = _build(tmp_path)
    with zipfile.ZipFile(out) as archive:
        config = json.loads(_member_text(archive, "config.redacted.json"))
        log = _member_text(archive, "recent-errors.log")
    assert USER not in config["data_dir"]
    assert REAL_HOME_PATTERN.search(config["data_dir"])
    assert USER not in log
    assert REAL_HOME_PATTERN.search(log)


def test_config_redacted_json_is_valid_and_complete(tmp_path):
    _, out = _build(tmp_path)
    with zipfile.ZipFile(out) as archive:
        config = json.loads(_member_text(archive, "config.redacted.json"))
    assert config["port"] == 3877
    assert config["telemetry"] is False
    assert "ollama_url" in config
    assert "data_dir" in config


# -- log rotation --------------------------------------------------------------


def test_rotation_keeps_exactly_three_files(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log = log_dir / "steward.log"
    cap = 100
    log.write_text("new " * 60)                       # over the cap
    (log_dir / "steward.log.1").write_text("one " * 60)
    (log_dir / "steward.log.2").write_text("two " * 60)
    assert rotate_log(log, max_bytes=cap) == 1
    names = sorted(p.name for p in log_dir.iterdir())
    assert names == ["steward.log", "steward.log.1", "steward.log.2"]
    assert log.read_text() == ""                      # fresh current file
    assert "new " in (log_dir / "steward.log.1").read_text()   # old current
    assert "one " in (log_dir / "steward.log.2").read_text()    # old .1
    assert "two " not in (log_dir / "steward.log.2").read_text()  # oldest dropped


def test_rotation_stays_at_three_across_repeated_rotations(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log = log_dir / "steward.log"
    cap = 100
    # Prime the chain so the full three-file shape exists before asserting.
    log.write_text("x" * (cap + 10))
    rotate_log(log, max_bytes=cap)                       # -> log, .1
    log.write_text("x" * (cap + 10))
    rotate_log(log, max_bytes=cap)                       # -> log, .1, .2
    for _ in range(6):
        log.write_text("x" * (cap + 10))
        rotate_log(log, max_bytes=cap)
        names = sorted(p.name for p in log_dir.iterdir())
        assert names == ["steward.log", "steward.log.1", "steward.log.2"]


def test_rotation_noop_below_cap(tmp_path):
    log = tmp_path / "steward.log"
    log.write_text("small")
    assert rotate_log(log, max_bytes=1024) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["steward.log"]
    assert log.read_text() == "small"


def test_rotation_at_real_five_mb_cap(tmp_path):
    log = tmp_path / "steward.log"
    log.write_text("x" * (5 * 1024 * 1024 + 1))
    assert rotate_log(log) == 1
    assert log.read_text() == ""
    assert (tmp_path / "steward.log.1").exists()
    assert (tmp_path / "steward.log.2").exists() is False


def test_rotation_addresses_log_file_under_data_root(tmp_path):
    root = tmp_path / "root"
    (root / "logs").mkdir(parents=True)
    log = steward_log_path(root)
    log.write_text("a" * 1024)
    assert rotate_log(log, max_bytes=500) == 1
    assert sorted(p.name for p in (root / "logs").iterdir()) == [
        "steward.log", "steward.log.1"]


# -- CLI -----------------------------------------------------------------------


def _dead_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_cli_doctor_bundle_writes_and_prints(tmp_path, capsys, monkeypatch):
    root = tmp_path / "home"
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps({"data_dir": str(root)}))
    monkeypatch.setenv("STEWARD_HOME", str(root))
    monkeypatch.setenv("STEWARD_OLLAMA_URL", f"http://127.0.0.1:{_dead_port()}")
    monkeypatch.setenv("STEWARD_OLLAMA_BIN", "/nonexistent/steward-ollama")
    monkeypatch.setattr("steward.runtime.SIDECAR_SEARCH", [])
    out_zip = tmp_path / "support.zip"
    from steward.cli import main
    assert main(["doctor", "--bundle", str(out_zip)]) == 0
    printed = capsys.readouterr().out
    assert str(out_zip) in printed
    assert "Send this file" in printed
    assert "support" in printed
    with zipfile.ZipFile(out_zip) as archive:
        assert archive.namelist() == list(BUNDLE_MEMBERS)
