"""Tests for steward.config, steward.paths, and the CLI surface.

Config behavior is exercised against a throwaway STEWARD_HOME so the real
~/.steward is never touched.
"""

from __future__ import annotations

import json
import stat

from steward import paths
from steward.config import Config, atomic_write_json


def _write_config(home, values: dict) -> None:
    import os
    os.makedirs(str(home), exist_ok=True)
    with open(home / "config.json", "w", encoding="utf-8") as fh:
        json.dump(values, fh)


def _env(home, **extra) -> dict:
    env = {"STEWARD_HOME": str(home)}
    env.update(extra)
    return env


# -- paths ---------------------------------------------------------------------


def test_paths_data_root_override(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_HOME", str(tmp_path / "elsewhere"))
    assert paths.data_root() == tmp_path / "elsewhere"


def test_paths_default_root(monkeypatch):
    monkeypatch.delenv("STEWARD_HOME", raising=False)
    assert paths.data_root() == paths.Path.home() / ".steward"


def test_ensure_dirs_owner_only(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("STEWARD_HOME", str(home))
    created = paths.ensure_dirs()
    assert set(created) == {"db", "models", "index", "logs", "licenses"}
    for directory in created.values():
        assert directory.is_dir()
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_config_path_under_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("STEWARD_HOME", str(home))
    assert paths.config_path() == home / "config.json"


# -- config --------------------------------------------------------------------


def test_load_defaults(tmp_path):
    cfg = Config.load(_env(tmp_path / "home"))
    assert cfg.port == 3877
    assert cfg.model == ""
    assert cfg.data_dir == str(tmp_path / "home")
    assert cfg.ollama_url == "http://localhost:11434"
    assert cfg.telemetry is False
    assert cfg.allow_shell is False
    assert cfg.retrieval_dirs == []


def test_env_overrides_file(tmp_path):
    home = tmp_path / "home"
    _write_config(home, {
        "port": 1000, "model": "gemma4:26b", "ollama_url": "http://file:1",
        "telemetry": True, "allow_shell": True, "retrieval_dirs": ["/vault/Notes"],
    })
    cfg = Config.load(_env(home, STEWARD_PORT="4133", STEWARD_MODEL="qwen3:8b",
                             STEWARD_OLLAMA_URL="http://env:2"))
    assert cfg.port == 4133
    assert cfg.model == "qwen3:8b"
    assert cfg.ollama_url == "http://env:2"
    assert cfg.allow_shell is True            # file value survives
    assert cfg.retrieval_dirs == ["/vault/Notes"]
    assert cfg.telemetry is False             # always false, no matter what


def test_invalid_port_env_ignored(tmp_path):
    home = tmp_path / "home"
    _write_config(home, {"port": 2000})
    cfg = Config.load(_env(home, STEWARD_PORT="not-a-number"))
    assert cfg.port == 2000


def test_atomic_write_round_trip_and_modes(tmp_path):
    home = tmp_path / "home"
    cfg = Config(port=4242, model="llama3.1:8b",
                 retrieval_dirs=["/mnt/vault/Notes", "/mnt/vault/Journal"])
    cfg.save(_env(home))
    config_file = home / "config.json"
    assert config_file.exists()
    assert stat.S_IMODE(config_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(home.stat().st_mode) == 0o700   # created owner-only
    leftovers = [p for p in home.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []                             # atomic: no tmp debris
    again = Config.load(_env(home))
    assert again.port == 4242
    assert again.model == "llama3.1:8b"
    assert again.retrieval_dirs == ["/mnt/vault/Notes", "/mnt/vault/Journal"]
    assert again.data_dir == str(home)


def test_save_without_env_uses_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("STEWARD_HOME", str(home))
    Config(port=5100, model="qwen3:8b").save()
    assert (home / "config.json").exists()
    assert Config.load().port == 5100


def test_corrupt_config_falls_back_to_defaults(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text("{not json", encoding="utf-8")
    cfg = Config.load(_env(home))
    assert cfg.port == 3877


def test_atomic_write_json_owner_only(tmp_path):
    target = tmp_path / "deep" / "config.json"
    atomic_write_json(target, {"port": 1}, mode=0o600)
    assert target.exists()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


# -- CLI surface ---------------------------------------------------------------


def test_version_command(capsys):
    from steward.cli import main
    assert main(["version"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("steward ")


def test_doctor_reports_degraded_not_traceback(capsys, monkeypatch, tmp_path):
    import socket as _socket
    with _socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]
    monkeypatch.setenv("STEWARD_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("STEWARD_OLLAMA_URL", f"http://127.0.0.1:{dead_port}")
    monkeypatch.setenv("STEWARD_OLLAMA_BIN", "/nonexistent/steward-ollama")
    monkeypatch.setattr("steward.runtime.SIDECAR_SEARCH", [])
    from steward.cli import main
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "DEGRADED" in out
    assert "Traceback" not in out
    assert "data dir:" in out
    assert "backend:" in out
