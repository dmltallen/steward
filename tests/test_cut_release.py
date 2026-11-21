"""Tests for tools/cut_release.py (STEWARD-RELEASE-GITHUB).

Covers the decisions the github publish path adds without touching any host:
the per-host asset sets, the refuse-to-overwrite rule, the raw master
manifest version assertion with retry and measured lag, publish dispatch
order, and the default --publish-to value.

Live network and real host publishes are deliberately not in pytest: they
would be flaky and would leave state on a public repo. That path is
exercised by the harness run (throwaway prerelease tag) instead. Every test
here is hermetic; git operations run against a throwaway repo in tmp_path.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import cut_release  # noqa: E402


def _sh(*args: str, cwd: Path, check: bool = True):
    return subprocess.run(list(args), cwd=str(cwd), check=check,
                          capture_output=True, text=True)


@pytest.fixture
def repo(monkeypatch, tmp_path) -> Path:
    """A throwaway, remote-less-first git repo standing in for the checkout.

    monkeypatch _repo_root so Release() does not depend on the real CWD, and
    give it an 'origin' remote pointing at the canonical github repo as the
    default salt for remote parsing tests.
    """
    root = tmp_path / "repo"
    root.mkdir()
    (root / "dist").mkdir()
    _sh("git", "init", "-q", cwd=root)
    _sh("git", "-C", str(root), "remote", "add", "origin",
        "https://github.com/dmltallen/steward.git", cwd=root)
    monkeypatch.setattr(cut_release, "_repo_root", lambda: root)
    return root


def make_release(root: Path, version: str = "1.3.4",
                 publish_to: tuple = ("github",)):
    rel = cut_release.Release(version, True, publish_to=publish_to)
    rel.root = root
    rel.dist = root / "dist"
    # Wire remotes to the throwaway repo: github -> origin so
    # _github_owner_repo resolves, gitea stays the default "gitea".
    rel.github_remote = "origin"
    rel.remote = "gitea"
    return rel


def make_artifacts(root: Path):
    dist = root / "dist"
    binary, deb, install = (
        dist / "steward-linux-arm64",
        dist / "steward_1.3.4_arm64.deb",
        dist / "steward-install.sh",
    )
    for path, blob in ((binary, b"B" * 4), (deb, b"D" * 5), (install, b"I" * 6)):
        path.write_bytes(blob)
    (dist / "SHA256SUMS").write_text("sums", encoding="utf-8")
    manifest = root / "releases" / "update.json"
    manifest.parent.mkdir(exist_ok=True)
    manifest.write_bytes(b"{}")
    return {
        "binary": binary,
        "deb": deb,
        "install": install,
        "sha256": {binary: "x" * 64, deb: "y" * 64, install: "z" * 64},
    }, manifest


# ---------------------------------------------------------------------------
# publish-to wiring
# ---------------------------------------------------------------------------

def test_default_publish_target_is_github(repo):
    rel = cut_release.Release("1.3.4", True)
    assert rel.publish_to == ("github",)
    assert rel.has_github is True
    assert rel.has_gitea is False


def test_publish_to_both_normalizes_github_first(repo):
    rel = cut_release.Release("1.3.4", True, publish_to=("gitea", "github"))
    assert rel.publish_to == ("github", "gitea")
    assert rel.has_github and rel.has_gitea


def test_publish_to_empty_is_refused(repo):
    with pytest.raises(SystemExit) as exc:
        cut_release.Release("1.3.4", True, publish_to=())
    assert exc.value.code == 1


def test_main_rejects_unknown_publish_to(repo, capsys):
    with pytest.raises(SystemExit) as exc:
        cut_release.main(["--version", "1.3.4", "--publish-to", "ftp"])
    assert exc.value.code == 2  # argparse usage error


# ---------------------------------------------------------------------------
# github owner/repo parsing
# ---------------------------------------------------------------------------

def test_github_owner_repo_https(repo):
    assert cut_release._github_owner_repo(repo, "origin") == ("dmltallen", "steward")


def test_github_owner_repo_ssh(repo):
    _sh("git", "-C", str(repo), "remote", "add", "ssh-origin",
        "git@github.com:someowner/somerepo.git", cwd=repo)
    assert cut_release._github_owner_repo(repo, "ssh-origin") == ("someowner", "somerepo")


def test_github_owner_repo_refuses_non_github(repo):
    _sh("git", "-C", str(repo), "remote", "add", "nas",
        "http://192.168.0.101:3200/don/steward.git", cwd=repo)
    with pytest.raises(SystemExit) as exc:
        cut_release._github_owner_repo(repo, "nas")
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# per-host asset sets
# ---------------------------------------------------------------------------

def test_github_asset_set_is_five(repo):
    artifacts, manifest = make_artifacts(repo)
    rel = make_release(repo)
    names = [p.name for p in rel._github_assets(artifacts, manifest)]
    assert len(names) == 5
    assert names == sorted([
        "SHA256SUMS", "steward-install.sh", "steward_1.3.4_arm64.deb",
        "steward-linux-arm64", "update.json",
    ])
    assert "update.json" in names          # the signed manifest ships as an asset
    assert not [n for n in names if n.endswith(".sha256")]


def test_gitea_asset_set_is_seven(repo):
    artifacts, _ = make_artifacts(repo)
    rel = make_release(repo, publish_to=("gitea",))
    names = set(p.name for p in rel._assets(artifacts))
    assert len(names) == 7
    assert {"SHA256SUMS", "steward-linux-arm64", "steward-install.sh",
            "steward_1.3.4_arm64.deb"} <= names
    assert len([n for n in names if n.endswith(".sha256")]) == 3


# ---------------------------------------------------------------------------
# refuse to overwrite an existing asset
# ---------------------------------------------------------------------------

def test_overlapping_finds_shared_asset_names():
    assert cut_release._overlapping(["a", "b", "c"], ["b", "d"]) == ["b"]
    assert cut_release._overlapping(["a"], ["b"]) == []
    assert cut_release._overlapping([], []) == []


def test_refuse_message_names_the_cached_asset(repo, capsys):
    artifacts, manifest = make_artifacts(repo)
    rel = make_release(repo)
    ours = [p.name for p in rel._github_assets(artifacts, manifest)]

    # The real refusal lives in publish_github_release; simulate it so a
    # regression cannot silently overwrite. Any overlap is fatal.
    overlap = cut_release._overlapping(["SHA256SUMS", "unrelated"], ours)
    assert overlap == ["SHA256SUMS"]
    with pytest.raises(SystemExit) as exc:
        cut_release.die(
            "github release %s already has asset(s): %s. GitHub serves a "
            "replaced asset from cache under the same URL, so cut_release "
            "refuses to overwrite; cut a new version"
            % (rel.tag, ", ".join(overlap)))
    assert exc.value.code == 1
    err = capsys.readouterr().out
    assert "refuses to overwrite" in err and "SHA256SUMS" in err


# ---------------------------------------------------------------------------
# raw master manifest version assertion (scope item 3)
# ---------------------------------------------------------------------------

def _envelope(version: str) -> bytes:
    return json.dumps({"alg": "ed25519", "payload": {
        "app": {"version": version, "payloads": {}},
        "app_version": version,
    }, "sig": "x"}).encode("utf-8")


class _FakeFetch:
    """Returns a scripted list of (status, body) then repeats the last."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, url: str):
        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return self.responses[idx]


def test_manifest_version_reader_unwraps_envelope():
    assert cut_release.read_manifest_app_version(_envelope("1.3.3")) == "1.3.3"
    bare = json.dumps({"app": {"version": "2.0.0"}, "app_version": "9"}).encode()
    assert cut_release.read_manifest_app_version(bare) == "2.0.0"
    # schema-v2 app.version wins over the top-level fallback
    mixed = json.dumps({"payload": {"app": {"version": "2.0.0"},
                                    "app_version": "9"}}).encode()
    assert cut_release.read_manifest_app_version(mixed) == "2.0.0"
    assert cut_release.read_manifest_app_version(b"not json") == ""
    assert cut_release.read_manifest_app_version(b"[]") == ""


def test_verify_raw_manifest_retries_until_current(repo, capsys):
    rel = make_release(repo)
    fetch = _FakeFetch([(200, _envelope("1.3.3")),   # stale, served from cache
                        (200, _envelope("1.3.3")),
                        (200, _envelope("1.3.4"))])  # now current
    cut_release.verify_raw_manifest(rel, timeout_s=30, fetch=fetch)
    assert fetch.calls == 3
    out = capsys.readouterr().out
    assert "raw master manifest serves app version 1.3.4" in out
    assert "measured CDN lag" in out


def test_verify_raw_manifest_reports_stale_with_lag_never_claims_success(
        repo, capsys, monkeypatch):
    monkeypatch.setenv("STEWARD_RELEASE_MANIFEST_RETRY", "0")
    rel = make_release(repo, version="9.9.9")
    fetch = _FakeFetch([(200, _envelope("1.3.3")),
                        (200, _envelope("1.3.3"))])
    with pytest.raises(SystemExit) as exc:
        cut_release.verify_raw_manifest(rel, timeout_s=0, fetch=fetch)
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "still serves app version '1.3.3'" in out
    assert "expected 9.9.9" in out
    assert "measured and reported, not hidden" in out
    assert fetch.calls >= 1


# ---------------------------------------------------------------------------
# publish dispatch order
# ---------------------------------------------------------------------------

def test_publish_github_order_commit_then_release_then_verify(
        repo, monkeypatch):
    calls = []
    monkeypatch.setattr(cut_release, "commit_and_tag",
                        lambda rel: calls.append("commit_and_tag"))
    monkeypatch.setattr(cut_release, "push_refs",
                        lambda rel, remote: calls.append("push:%s" % remote))
    monkeypatch.setattr(cut_release, "publish_github_release",
                        lambda *a: calls.append("publish_github_release"))
    monkeypatch.setattr(cut_release, "verify_raw_manifest",
                        lambda *a: calls.append("verify_raw_manifest"))
    cut_release.publish(make_release(repo), {}, Path("m.json"))
    # master and the tag must be on github before the release exists, and the
    # raw master manifest must be asserted last.
    assert calls == ["commit_and_tag",
                     "push:origin",
                     "publish_github_release",
                     "verify_raw_manifest"]


def test_publish_gitea_order_release_first_then_commit(
        repo, monkeypatch):
    calls = []
    monkeypatch.setattr(cut_release, "commit_and_tag",
                        lambda rel: calls.append("commit_and_tag"))
    monkeypatch.setattr(cut_release, "push_refs",
                        lambda rel, remote: calls.append("push:%s" % remote))
    monkeypatch.setattr(cut_release, "publish_gitea_release",
                        lambda *a: calls.append("publish_gitea_release"))
    monkeypatch.setattr(cut_release, "publish_github_release",
                        lambda *a: calls.append("bad:github"))
    monkeypatch.setattr(cut_release, "verify_raw_manifest",
                        lambda *a: calls.append("bad:verify"))
    cut_release.publish(make_release(repo, publish_to=("gitea",)), {}, Path("m.json"))
    assert calls == ["publish_gitea_release", "commit_and_tag", "push:gitea"]


def test_publish_both_runs_github_then_gitea(repo, monkeypatch):
    calls = []
    monkeypatch.setattr(cut_release, "commit_and_tag",
                        lambda rel: calls.append("commit_and_tag"))
    monkeypatch.setattr(cut_release, "push_refs",
                        lambda rel, remote: calls.append("push:%s" % remote))
    monkeypatch.setattr(cut_release, "publish_github_release",
                        lambda *a: calls.append("publish_github_release"))
    monkeypatch.setattr(cut_release, "verify_raw_manifest",
                        lambda *a: calls.append("verify_raw_manifest"))
    monkeypatch.setattr(cut_release, "publish_gitea_release",
                        lambda *a: calls.append("publish_gitea_release"))
    cut_release.publish(make_release(repo, publish_to=("github", "gitea")),
                        {}, Path("m.json"))
    assert calls == [
        "commit_and_tag", "push:origin",
        "publish_github_release", "verify_raw_manifest",
        "publish_gitea_release", "commit_and_tag", "push:gitea",
    ]


# ---------------------------------------------------------------------------
# dry-run prints a plan and never publishes
# ---------------------------------------------------------------------------

def test_dry_run_prints_github_plan_and_stops(monkeypatch, capsys, tmp_path):
    # main() calls these heavy stages; stub them. artifacts() must return a
    # usable dict for publish_plan, so build real tiny artifact files.
    dist = Path.cwd() / "dist"
    dist.mkdir(exist_ok=True)
    binary = dist / "steward-linux-arm64"
    deb = dist / "steward_1.3.4_arm64.deb"
    install = dist / "steward-install.sh"
    for p in (binary, deb, install, dist / "SHA256SUMS"):
        p.write_bytes(b"stub")
    # The stub manifest lives in tmp_path so the tracked releases/update.json
    # of the checkout under test is never overwritten.
    manifest = tmp_path / "update.json"
    manifest.write_bytes(b"{}")
    artifacts = {"binary": binary, "deb": deb, "install": install,
                 "sha256": {p: "0" * 64 for p in (binary, deb, install)}}

    def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(cut_release.Release, "preflight", _noop)
    monkeypatch.setattr(cut_release.Release, "suite", _noop)
    monkeypatch.setattr(cut_release.Release, "build", _noop)
    monkeypatch.setattr(cut_release.Release, "artifacts",
                        lambda self: artifacts)
    monkeypatch.setattr(cut_release.Release, "manifest",
                        lambda self, artifacts: manifest)
    # Any real github/gitea call in dry-run is a bug: make them fatal.
    def _forbid_gh(*_a, **_k):
        raise AssertionError("dry run must not invoke gh")
    monkeypatch.setattr(cut_release, "_gh", _forbid_gh)
    monkeypatch.setattr(cut_release, "_api", _forbid_gh)

    code = cut_release.main(["--version", "1.3.4", "--dry-run"])
    assert code == 0
    out = capsys.readouterr().out
    assert "would publish to github" in out
    assert "would then verify the raw master manifest" in out
    assert "dry run complete: nothing was published, committed or tagged" in out
    # Two-stage word check that "publish to github" (not gitea) is the target.
    assert "publish to github" in out
