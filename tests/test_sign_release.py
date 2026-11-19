"""Tests for tools/sign_release.py (release checksum signing).

Covers: a sign/verify round trip against the embedded vendor public key,
refusal of a tampered checksum file, refusal when the signing key is
missing, refusal of a wrong-length or non-matching signature, and refusal
when the checksum or signature file is missing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from steward import license as lic  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import sign_release as sr  # noqa: E402


def write_sums(dirpath: Path, lines: list[str] | None = None) -> Path:
    body = lines if lines is not None else [
        "1cfcc14e2e8ff5ffbfdea78b1f69e7e1e5c2c622e936b93188a8ce07c961f2e3  steward-linux-arm64\n",
        "d9b86053a1dc2b45b510843c7d54ef286e31b785ddf8ef8b5ddae2a6255b591d  steward_1.2.0_arm64.deb\n",
    ]
    sums = dirpath / "SHA256SUMS"
    sums.write_text("".join(body), encoding="utf-8")
    return sums


@pytest.fixture
def keypair(tmp_path, monkeypatch) -> dict:
    pem, pub = lic.generate_keypair()
    keyfile = tmp_path / "keys" / "steward-signing.key"
    keyfile.parent.mkdir()
    keyfile.write_bytes(pem)
    keyfile.chmod(0o600)
    monkeypatch.setattr(lic, "VENDOR_PUBLIC_KEY_HEX", pub)
    return {"pem": pem, "pub": pub, "key": keyfile}


def test_roundtrip_sign_and_verify(tmp_path, keypair) -> None:
    sums = write_sums(tmp_path)
    args = sr.build_parser().parse_args(
        ["sign", "--key", str(keypair["key"]), "--sums", str(sums)])
    assert sr.cmd_sign(args) == 0

    sig = tmp_path / "SHA256SUMS.sig"
    assert sig.is_file()
    assert len(sig.read_bytes()) == sr.ED25519_SIG_BYTES

    args = sr.build_parser().parse_args(["verify", "--sums", str(sums)])
    assert sr.cmd_verify(args) == 0


def test_tampered_sums_refused(tmp_path, keypair, capsys) -> None:
    sums = write_sums(tmp_path)
    args = sr.build_parser().parse_args(
        ["sign", "--key", str(keypair["key"]), "--sums", str(sums)])
    assert sr.cmd_sign(args) == 0

    sums.write_text(sums.read_text(encoding="utf-8").replace("arm64", "amd64"),
                    encoding="utf-8")

    args = sr.build_parser().parse_args(["verify", "--sums", str(sums)])
    assert sr.cmd_verify(args) != 0
    err = capsys.readouterr().err
    assert "INVALID" in err


def test_verify_does_not_fix_the_embedded_key(tmp_path, keypair) -> None:
    """A signature made with one key must fail against a different embedded key."""
    sums = write_sums(tmp_path)
    args = sr.build_parser().parse_args(
        ["sign", "--key", str(keypair["key"]), "--sums", str(sums)])
    assert sr.cmd_sign(args) == 0

    _pem, other_pub = lic.generate_keypair()
    lic.VENDOR_PUBLIC_KEY_HEX = other_pub  # a different vendor key is embedded
    try:
        args = sr.build_parser().parse_args(["verify", "--sums", str(sums)])
        assert sr.cmd_verify(args) != 0
    finally:
        lic.VENDOR_PUBLIC_KEY_HEX = keypair["pub"]


def test_missing_key_refuses_to_sign(tmp_path) -> None:
    sums = write_sums(tmp_path)
    missing = tmp_path / "missing-key.pem"
    args = sr.build_parser().parse_args(
        ["sign", "--key", str(missing), "--sums", str(sums)])
    assert sr.cmd_sign(args) != 0
    assert not (tmp_path / "SHA256SUMS.sig").exists()


def test_sign_refuses_when_sums_missing(tmp_path, keypair) -> None:
    missing = tmp_path / "SHA256SUMS"
    args = sr.build_parser().parse_args(
        ["sign", "--key", str(keypair["key"]), "--sums", str(missing)])
    assert sr.cmd_sign(args) != 0


def test_verify_refuses_missing_files(tmp_path) -> None:
    args = sr.build_parser().parse_args(
        ["verify", "--sums", str(tmp_path / "nope")])
    assert sr.cmd_verify(args) != 0

    sums = write_sums(tmp_path)
    args = sr.build_parser().parse_args(["verify", "--sums", str(sums)])
    assert sr.cmd_verify(args) != 0  # no .sig present


def test_verify_refuses_wrong_length_signature(tmp_path, keypair) -> None:
    sums = write_sums(tmp_path)
    (tmp_path / "SHA256SUMS.sig").write_bytes(b"x" * 32)
    args = sr.build_parser().parse_args(["verify", "--sums", str(sums)])
    assert sr.cmd_verify(args) != 0


def test_env_key_override(tmp_path, keypair, monkeypatch) -> None:
    """$STEWARD_SIGNING_KEY selects the key when --key is not given."""
    sums = write_sums(tmp_path)
    monkeypatch.setenv("STEWARD_SIGNING_KEY", str(keypair["key"]))
    args = sr.build_parser().parse_args(["sign", "--sums", str(sums)])
    assert sr.cmd_sign(args) == 0
    args = sr.build_parser().parse_args(["verify", "--sums", str(sums)])
    assert sr.cmd_verify(args) == 0
