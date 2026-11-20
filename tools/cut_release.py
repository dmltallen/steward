#!/usr/bin/env python3
"""One-command Steward release: preflight, suite, build, artifacts, manifest,
publish. Stop on the first failure.

Usage (from the root of the checkout you intend to release):

    python3 tools/cut_release.py --version X.Y.Z [--dry-run]

The repo is resolved by asking git for the top level of the current working
directory, so you run it from the checkout you mean to release. In real mode
the preflight requires a clean tree on master whose HEAD equals the gitea
remote HEAD; publish then creates the gitea release, attaches every asset,
re-downloads each asset and compares its sha256, commits releases/update.json
with a release commit, tags the release commit, and pushes master and the tag.
No version is invented, nothing is force-pushed, and a dirty tree refuses.

--dry-run runs preflight, the full suite, the native build, the artifact
hashing and the signed manifest exactly as a real release would, then prints
exactly what would be published and stops. The two publish-environment checks
(on master, and HEAD in sync with gitea) are hard refusals in real mode and
advisory warnings in dry-run, so a rehearsal can run from any clean checkout.
All other preflight checks (clean tree, pyproject version match, CHANGELOG
section, existing tag) are hard refusals in both modes.

Environment overrides:
    STEWARD_RELEASE_REMOTE   remote name, default gitea
    STEWARD_RELEASE_SUITE_CMD   exact test command (shlex-split), default:
        uv run --no-project --with pytest --with numpy --with zeroconf
        --with cryptography python -m pytest tests -q
        (falls back to "python3 -m pytest tests -q" when uv is missing)
    STEWARD_SIGNING_KEY       path to the Ed25519 signing key, default
        ~/.steward-keys/steward-signing.key (same default as mint_manifest)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from typing import NoReturn

TAG_PREFIX = "v"


def _say(msg: str) -> None:
    print(msg)


def die(msg: str) -> NoReturn:
    _say("cut refused: %s" % msg)
    sys.exit(1)


def _repo_root() -> Path:
    """The git top level of the current directory, or die."""
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        die("not inside a git checkout (%s); run from a steward checkout"
            % (out.stderr or out.stdout or "").strip())
    root = out.stdout.strip()
    if not root:
        die("could not determine the repo root")
    return Path(root)


def _git(root: Path, *args: str, check: bool = True):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, check=check,
    )


def _sh(cmd: list, cwd: Path, check: bool = True, label: str = ""):
    """Run cmd in cwd, streaming output; return CompletedProcess."""
    return subprocess.run(cmd, cwd=str(cwd), check=check)


def _sh_capture(cmd: list, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, check=check,
        encoding="utf-8", errors="replace",
    )


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _remote_url(root: Path, remote: str) -> str:
    out = _git(root, "remote", "get-url", remote)
    return out.stdout.strip()


def _gitea_base(root: Path, remote: str) -> str:
    url = _remote_url(root, remote)
    base = url
    if base.endswith(".git"):
        base = base[:-4]
    if base.endswith("/"):
        base = base[:-1]
    # A scp-like or file remote has no usable http api; refuse loudly.
    if not base.startswith("http://") and not base.startswith("https://"):
        die("gitea remote %r is not an http(s) URL: %r" % (remote, url))
    return base


def _api_base(root: Path, remote: str) -> str:
    """The scheme://host:port API root for the remote (no repo path).

    _gitea_base() returns the repo web base (http://host:port/<owner>/<repo>),
    which is what asset download URLs need. The REST API instead sits at
    http://host:port/api/v1/repos/<owner>/<repo>, so the repo path must be
    dropped here and re-appended as part of the /api/v1/repos prefix.
    """
    parsed = urllib.parse.urlparse(_gitea_base(root, remote))
    return "%s://%s" % (parsed.scheme, parsed.netloc)


def _api_token(host_port: str) -> str:
    """Read the bearer token for host:port from ~/.git-credentials.

    The file holds one "http://user:pass@host:port" line per credential.
    Prefer a non-oauth2 user (a personal access token) over the oauth2 line.
    """
    creds = Path.home() / ".git-credentials"
    if not creds.is_file():
        die("~/.git-credentials not found; a token is needed to publish")
    wanted = host_port.replace(":3200", "%3a3200").replace(":443", "%3a443")
    candidates = []
    for line in creds.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^https?://([^:]+):([^@]*)@(.+)$", line)
        if not m:
            continue
        username, password, host_in_line = m.group(1), m.group(2), m.group(3)
        if host_in_line != wanted and host_in_line != host_port:
            continue
        token = urllib.parse.unquote(password)
        candidates.append((username, token))
    if not candidates:
        die("no credential for %s in ~/.git-credentials" % host_port)
    candidates.sort(key=lambda pair: 1 if pair[0] == "oauth2" else 0)
    return candidates[0][1]


def _api(url: str, token: str, data=None, method: str | None = None,
         headers: dict | None = None, binary: bytes | None = None,
         content_type: str | None = None):
    req_headers = {"Authorization": "token %s" % token}
    if headers:
        req_headers.update(headers)
    body = None
    if binary is not None:
        body = binary
        if content_type:
            req_headers["Content-Type"] = content_type
    elif data is not None:
        body = json.dumps(data).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=req_headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _parse_version(text: str) -> str:
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        die("pyproject.toml has no version = \"...\" line")
    return m.group(1)


def _changelog_section(text: str, version: str) -> str | None:
    """The CHANGELOG section body for v<version>, or None when absent."""
    lines = text.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if re.match(r"^## v%s\b" % re.escape(version), line):
            start = idx
            break
    if start is None:
        return None
    body = []
    for line in lines[start + 1:]:
        if re.match(r"^## ", line):
            break
        body.append(line)
    return "\n".join(body).strip()


def _native_arch() -> tuple:
    """(deb_arch, platform_key) for this host, e.g. ('arm64', 'linux-aarch64')."""
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64", "armv8l"):
        return "arm64", "linux-aarch64"
    if machine in ("x86_64", "amd64"):
        return "amd64", "linux-x86_64"
    die("unsupported build host arch: %s" % machine)
    raise SystemExit(1)  # pragma: no cover - die() exits


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

class Release:
    def __init__(self, version: str, dry_run: bool):
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            die("--version must be X.Y.Z with digits only, got %r" % version)
        self.version = version
        self.dry_run = dry_run
        self.tag = TAG_PREFIX + version
        self.root = _repo_root()
        self.pyproject = self.root / "pyproject.toml"
        self.changelog = self.root / "docs" / "CHANGELOG.md"
        self.dist = self.root / "dist"
        self.remote = os.environ.get("STEWARD_RELEASE_REMOTE", "gitea").strip()
        self.arch, self.platform_key = _native_arch()
        self.binary_name = "steward-linux-%s" % self.arch
        self.install_name = "steward-install.sh"
        self.deb_name = "steward_%s_%s.deb" % (version, self.arch)
        self.catalog_version = date.today().isoformat() + ".1"
        self.released = date.today().isoformat()

    # -- 1. preflight -------------------------------------------------------
    def preflight(self) -> None:
        _say("")
        _say("== [1/8] preflight ==")
        status = _git(self.root, "status", "--porcelain")
        if status.stdout.strip():
            die("working tree is not clean:\n%s" % status.stdout.strip())
        _say("ok   working tree is clean")

        branch = _git(self.root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if branch != "master":
            msg = "must run a real release from master (on %r); " % branch
            if self.dry_run:
                _say("warn %smaking this check advisory in dry-run" % msg)
            else:
                die(msg + "the publish gates are hard in a real release")
        else:
            _say("ok   on master")

        local_head = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        remote_out = _git(self.root, "ls-remote", self.remote,
                          "refs/heads/master", check=False)
        if remote_out.returncode != 0:
            die("could not reach remote %s (git ls-remote failed)"
                % self.remote)
        remote_head = remote_out.stdout.split("\t")[0].strip() if remote_out.stdout.strip() else ""
        if not remote_head:
            die("could not read %s master HEAD from the remote" % self.remote)
        if local_head != remote_head:
            msg = ("local HEAD %s does not equal %s/master %s; "
                   % (local_head[:12], self.remote, remote_head[:12]))
            if self.dry_run:
                _say("warn %smaking this check advisory in dry-run" % msg)
            else:
                die(msg + "the publish gates are hard in a real release")
        else:
            _say("ok   local HEAD == %s/master (%s)" % (self.remote, remote_head[:12]))

        declared = _parse_version(self.pyproject.read_text(encoding="utf-8"))
        if declared != self.version:
            die("pyproject.toml version %s does not match --version %s; bump it first"
                % (declared, self.version))
        _say("ok   pyproject.toml version %s == %s" % (declared, self.version))

        changelog_text = self.changelog.read_text(encoding="utf-8")
        if not _changelog_section(changelog_text, self.version):
            die("docs/CHANGELOG.md has no section for v%s; add one first" % self.version)
        _say("ok   CHANGELOG has a v%s section" % self.version)

        # The hard collision gate is the local tag: it is the state this repo
        # controls. A remote tag or an existing gitea release is reported below
        # and publish refuses loudly at the API if a release already exists.
        local_tag = _git(self.root, "rev-parse", "-q", "--verify",
                         "refs/tags/" + self.tag, check=False)
        if local_tag.returncode == 0:
            die("tag %s already exists locally at %s; a release must be a new version"
                % (self.tag, local_tag.stdout.strip()[:12]))
        _say("ok   tag %s does not exist locally" % self.tag)

        remote_tag = _git(self.root, "ls-remote", self.remote,
                          "refs/tags/" + self.tag, check=False).stdout.strip()
        if remote_tag:
            _say("warn remote %s already has tag %s at %s; a republish will need "
                 "release teardown first (see docs/RELEASE.md)"
                 % (self.remote, self.tag, remote_tag.split("\t")[0][:12]))
        else:
            _say("ok   no %s tag on %s" % (self.tag, self.remote))

        if not self.dry_run:
            token = self._api_token_or_die()
            parsed = urllib.parse.urlparse(_gitea_base(self.root, self.remote))
            status, body = _api(
                "%s/api/v1/repos%s/releases/tags/%s"
                % (_api_base(self.root, self.remote), parsed.path, self.tag), token)
            if status == 200:
                _say("warn gitea already hosts a release tagged %s; publish will "
                     "refuse at the API until that release is removed" % self.tag)
            else:
                _say("ok   no gitea release tagged %s" % self.tag)

    def _api_token_or_die(self) -> str:
        base = _gitea_base(self.root, self.remote)
        parsed = urllib.parse.urlparse(base)
        host_port = parsed.netloc
        token = _api_token(host_port)
        if not token:
            die("no usable token for %s in ~/.git-credentials" % host_port)
        return token

    # -- 2. suite ------------------------------------------------------------
    def _suite_command(self) -> tuple:
        """(argv, description) for the full test run, most-capable first."""
        override = os.environ.get("STEWARD_RELEASE_SUITE_CMD")
        if override:
            return shlex.split(override), "STEWARD_RELEASE_SUITE_CMD override"
        uv = shutil.which("uv")
        hermes_uv = Path.home() / ".hermes" / "bin" / "uv"
        if uv:
            return [uv, "run", "--no-project", "--with", "pytest",
                    "--with", "numpy", "--with", "zeroconf",
                    "--with", "cryptography",
                    "python", "-m", "pytest", "tests", "-q"], "uv"
        if hermes_uv.is_file():
            return [str(hermes_uv), "run", "--no-project",
                    "--with", "pytest", "--with", "numpy",
                    "--with", "zeroconf", "--with", "cryptography",
                    "python", "-m", "pytest", "tests", "-q"], "uv"
        venv_py = self.root / ".venv" / "bin" / "python"
        if venv_py.is_file():
            return [str(venv_py), "-m", "pytest", "tests", "-q"], "repo venv"
        return ["python3", "-m", "pytest", "tests", "-q"], "python3"

    def suite(self) -> None:
        _say("")
        _say("== [2/8] full test suite ==")
        cmd, how = self._suite_command()
        _say("running (via %s): %s" % (how, " ".join(cmd)))
        proc = _sh_capture(cmd, self.root, check=False)
        tail = proc.stdout[-4000:] if proc.stdout else ""
        if tail:
            _say(tail.strip())
        if proc.returncode != 0 or " passed" not in proc.stdout:
            die("test suite failed (exit %s); fix before cutting a release"
                % proc.returncode)
        m = re.search(r"(\d+) passed", proc.stdout)
        count = m.group(1) if m else "?"
        _say("suite ok: %s passed" % count)

    # -- 3. build ------------------------------------------------------------
    def build(self) -> None:
        _say("")
        _say("== [3/8] native build (make build-deb) ==")
        proc = _sh(["make", "build-deb"], self.root, check=False)
        if proc.returncode != 0:
            die("make build-deb failed (exit %s)" % proc.returncode)
        binary = self.dist / "steward"
        if not binary.is_file():
            die("build produced no dist/steward binary")
        out = _sh_capture(["./dist/steward", "version"], self.root,
                          check=False)
        version_string = (out.stdout or "").strip()
        expected = "steward %s" % self.version
        if version_string != expected:
            die("frozen binary reports %r but --version is %s"
                % (version_string, self.version))
        _say("ok   frozen binary version string: %s" % version_string)
        deb = self.dist / self.deb_name
        if not deb.is_file():
            die("build produced no %s" % self.deb_name)
        _say("ok   artifacts: %s (%d bytes), %s (%d bytes)"
             % (binary.name, binary.stat().st_size,
                deb.name, deb.stat().st_size))

    # -- 4. artifacts ---------------------------------------------------------
    def artifacts(self) -> dict:
        _say("")
        _say("== [4/8] artifacts and checksums ==")
        binary = self.dist / "steward"
        dist_binary = self.dist / self.binary_name
        dist_install = self.dist / self.install_name
        deb = self.dist / self.deb_name
        shutil.copy2(binary, dist_binary)
        shutil.copy2(self.root / "scripts" / "install.sh", dist_install)
        for path, label in ((dist_binary, "binary"), (deb, "deb"),
                            (dist_install, "install script")):
            if not path.is_file():
                die("missing %s artifact %s" % (label, path.name))
        files = [dist_binary, deb, dist_install]
        sums = {path: _sha256(path) for path in files}
        # SHA256SUMS and per-file .sha256 sidecars, matching the layout the
        # install script and prior releases use.
        (self.dist / "SHA256SUMS").write_text(
            "".join("%s  %s\n" % (sums[p], p.name) for p in sorted(files, key=lambda p: p.name)),
            encoding="utf-8")
        for path in files:
            (self.dist / (path.name + ".sha256")).write_text(
                "%s  %s\n" % (sums[path], path.name), encoding="utf-8")
        # Verify every hash by re-reading the file.
        for path in files:
            again = _sha256(path)
            if again != sums[path]:
                die("internal checksum mismatch for %s" % path.name)
        _say("%-42s %12s  %s" % ("file", "bytes", "sha256"))
        for path in files:
            _say("%-42s %12d  %s" % (path.name, path.stat().st_size,
                                     sums[path][:24] + "..."))
        _say("wrote SHA256SUMS and one .sha256 sidecar per artifact; "
             "every hash re-read and verified")
        return {
            "binary": dist_binary,
            "deb": deb,
            "install": dist_install,
            "sha256": sums,
        }

    # -- 5. manifest ----------------------------------------------------------
    def manifest(self, artifacts: dict) -> Path:
        _say("")
        _say("== [5/8] signed update manifest ==")
        base = _gitea_base(self.root, self.remote)
        parsed = urllib.parse.urlparse(base)
        notes_url = "%s/releases/tag/%s" % (base, self.tag)
        payload_base = "%s/releases/download/%s" % (base, self.tag)
        mint = Path(__file__).resolve().parent / "mint_manifest.py"
        out = self.root / "releases" / "update.json"
        cmd = [sys.executable, str(mint),
               "--catalog-version", self.catalog_version,
               "--released", self.released,
               "--notes-url", notes_url,
               "--payload", self.platform_key, str(artifacts["binary"]),
               "--payload-base", payload_base,
               "--out", str(out)]
        proc = _sh(cmd, self.root, check=False)
        if proc.returncode != 0:
            die("mint_manifest failed (exit %s); the manifest was not written" % proc.returncode)
        if not out.is_file():
            die("mint_manifest reported success but wrote no %s" % out)
        # Verify the signature with the shipped ota code path, then confirm the
        # app payload sha256 in the manifest matches the built artifact exactly.
        sys.path.insert(0, str(self.root / "src"))
        from steward import ota as ota_mod
        payload = None
        try:
            doc = ota_mod.fetch_document(str(out))
            payload = ota_mod.verify_manifest(doc)
        except Exception as exc:  # noqa: BLE001 - the verify path raises typed errors
            die("self-verify of the minted manifest failed: %s" % exc)
        assert payload is not None
        app = payload.get("app") or {}
        entry = (app.get("payloads") or {}).get(self.platform_key) or {}
        if entry.get("sha256") != artifacts["sha256"][artifacts["binary"]]:
            die("manifest app payload sha256 does not match the built artifact")
        _say("ok   signature verified by steward.ota.verify_manifest "
             "(catalog %s, app %s, source %s)"
             % (payload["version"], app.get("version"), self.platform_key))
        _say("ok   manifest app payload sha256 matches %s"
             % artifacts["binary"].name)
        _say("ok   wrote %s" % out)
        return out

    def _assets(self, artifacts: dict) -> list:
        """The seven published assets, sorted by name: three artifacts, their
        .sha256 sidecars, and the SHA256SUMS file. This matches the layout of
        every prior release and what scripts/install.sh expects."""
        primary = [artifacts["binary"], artifacts["deb"], artifacts["install"]]
        files = list(primary)
        files.extend(self.dist / (p.name + ".sha256") for p in primary)
        files.append(self.dist / "SHA256SUMS")
        return sorted(files, key=lambda p: p.name)

    # -- publish plan for dry-run ---------------------------------------------
    def publish_plan(self, artifacts: dict, manifest: Path) -> None:
        _say("")
        _say("== [6/8] publish (dry run: nothing was published) ==")
        body_tail = self._release_body()
        _say("would create gitea release: %s (target master)" % self.tag)
        if body_tail:
            _say("would set release body to the v%s CHANGELOG section (%d chars)"
                 % (self.version, len(body_tail)))
        base = _gitea_base(self.root, self.remote)
        asset_files = self._assets(artifacts)
        _say("would attach and re-download %d assets:" % len(asset_files))
        _say("  %-40s %-24s %12s" % ("asset", "sha256", "bytes"))
        for path in asset_files:
            _say("  %-40s %-24s %12d"
                 % (path.name, artifacts["sha256"].get(path, _sha256(path))[:24],
                    path.stat().st_size))
        _say("  payload url: %s/releases/download/%s/%s"
             % (base, self.tag, artifacts["binary"].name))
        _say("  notes url:   %s/releases/tag/%s" % (base, self.tag))
        _say("would commit on master:  release: %s publish the signed update "
             "manifest (%s)" % (self.version, manifest.name))
        _say("would tag:               %s" % self.tag)
        _say("would push:              %s master and refs/tags/%s"
             % (self.remote, self.tag))

    def _release_body(self) -> str:
        text = self.changelog.read_text(encoding="utf-8")
        return _changelog_section(text, self.version) or ""


# ---------------------------------------------------------------------------
# Publish (real mode only)
# ---------------------------------------------------------------------------

def _multipart(name: str, filename: str, blob: bytes) -> tuple:
    boundary = "----cutrelease%s" % hashlib.sha256(blob).hexdigest()[:16]
    lines = [
        "--%s" % boundary,
        'Content-Disposition: form-data; name="%s"; filename="%s"' % (name, filename),
        "Content-Type: application/octet-stream",
        "",
    ]
    body = ("\r\n".join(lines)).encode("utf-8") + b"\r\n" + blob + \
        ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    content_type = "multipart/form-data; boundary=%s" % boundary
    return content_type, body


def publish(rel: Release, artifacts: dict, manifest: Path) -> None:
    _say("")
    _say("== [6/8] publish ==")
    base = _gitea_base(rel.root, rel.remote)
    parsed = urllib.parse.urlparse(base)
    repo_path = parsed.path
    token = rel._api_token_or_die()
    api = "%s/api/v1/repos%s" % (_api_base(rel.root, rel.remote), repo_path)

    status, resp = _api("%s/releases" % api, token, data={
        "tag_name": rel.tag,
        "target_commitish": "master",
        "name": rel.tag,
        "body": rel._release_body(),
    })
    if status not in (200, 201):
        die("gitea create release failed (%s): %s" % (status, resp[:300]))
    release = json.loads(resp)
    release_id = release["id"]
    _say("ok   created release %s (%s)" % (rel.tag, release.get("html_url", release_id)))

    asset_files = rel._assets(artifacts)

    table = []
    for path in asset_files:
        blob = path.read_bytes()
        content_type, body = _multipart("attachment", path.name, blob)
        status, resp = _api(
            "%s/releases/%s/assets?name=%s" % (api, release_id,
                                               urllib.parse.quote(path.name)),
            token, binary=body, content_type=content_type,
        )
        if status not in (200, 201):
            die("asset upload failed for %s (%s): %s"
                % (path.name, status, resp[:300]))
        # Re-download the uploaded asset and compare hashes.
        dl_url = "%s/releases/download/%s/%s" % (base, rel.tag, path.name)
        dl_status, dl_body = _api(dl_url, token)
        if dl_status != 200:
            die("re-download failed for %s (%s)" % (path.name, dl_status))
        got = hashlib.sha256(dl_body).hexdigest()
        want = _sha256(path)
        table.append((path.name, want, got, want == got))
    _say("%-42s %-24s %-24s %s" % ("asset", "uploaded sha256", "re-read sha256", "match"))
    for name, want, got, ok in table:
        _say("%-42s %s %s %s" % (name, want[:24], got[:24], "ok" if ok else "FAIL"))
    if not all(row[3] for row in table):
        die("one or more uploaded assets failed the re-download hash compare")

    commit_and_tag(rel)


def commit_and_tag(rel: Release) -> None:
    _say("")
    _say("== [7/8] commit and tag ==")
    message = "release: %s publish the signed update manifest" % rel.version

    def _git_stage(*args: str) -> subprocess.CompletedProcess:
        proc = _git(rel.root, *args, check=False)
        if proc.returncode != 0:
            die("git %s failed: %s" % (args[0],
                (proc.stderr or proc.stdout or "").strip()))
        return proc

    _git_stage("add", "releases/update.json")
    _git_stage("commit", "-m", message)
    head = _git_stage("rev-parse", "--short", "HEAD").stdout.strip()
    _say("ok   release commit %s on master: %s" % (head, message))
    _git_stage("tag", rel.tag)
    _say("ok   tagged %s at %s" % (rel.tag, head))
    _say("")
    _say("== [8/8] push ==")
    for ref in ("master", "refs/tags/%s" % rel.tag):
        proc = _git(rel.root, "push", rel.remote, ref, check=False)
        if proc.returncode != 0:
            die("push of %s failed: %s"
                % (ref, (proc.stderr or proc.stdout or "").strip()))
        _say("ok   pushed %s %s" % (rel.remote, ref))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="cut_release", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", required=True,
                        help="the release version, X.Y.Z (must match pyproject.toml)")
    parser.add_argument("--dry-run", action="store_true",
                        help="run everything except publish, commit and tag")
    args = parser.parse_args(argv)

    rel = Release(args.version, args.dry_run)
    _say("steward cut_release %s (dry run: %s) on arch %s"
         % (args.version, "yes" if args.dry_run else "no", rel.arch))
    rel.preflight()
    rel.suite()
    rel.build()
    artifacts = rel.artifacts()
    manifest = rel.manifest(artifacts)
    if rel.dry_run:
        rel.publish_plan(artifacts, manifest)
        _say("")
        _say("dry run complete: nothing was published, committed or tagged")
        return 0
    publish(rel, artifacts, manifest)
    _say("")
    _say("release %s complete" % args.version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
