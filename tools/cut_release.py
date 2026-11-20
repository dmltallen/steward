#!/usr/bin/env python3
"""One-command Steward release: preflight, suite, build, artifacts, manifest,
publish. Stop on the first failure.

Usage (from the root of the checkout you intend to release):

    python3 tools/cut_release.py --version X.Y.Z [--publish-to github|gitea|both]
                                 [--dry-run]

The repo is resolved by asking git for the top level of the current working
directory, so you run it from the checkout you mean to release. In real mode
the preflight requires a clean tree on master whose HEAD equals the publish
remote's master HEAD; publish then creates the release, attaches every asset,
re-downloads each asset and compares its sha256, commits releases/update.json
with a release commit, tags the release commit, and pushes master and the tag.

--publish-to names the host(s): github (default, the canonical home only a
stranger can download from) and/or gitea (the mirror). The github path drives
`gh release create`, refuses to overwrite an existing asset on a release (a
replaced asset under the same URL is served from cache), requires the signed
manifest to reach master before the release is usable, and after publishing
fetches the raw master manifest and asserts its app version, retrying for up
to five minutes and reporting the measured CDN lag instead of claiming
success. The gitea path is unchanged from earlier releases.
No version is invented, nothing is force-pushed, and a dirty tree refuses.

--dry-run runs preflight, the full suite, the native build, the artifact
hashing and the signed manifest exactly as a real release would, then prints
exactly what would be published and stops. The two publish-environment checks
(on master, and HEAD in sync with gitea) are hard refusals in real mode and
advisory warnings in dry-run, so a rehearsal can run from any clean checkout.
All other preflight checks (clean tree, pyproject version match, CHANGELOG
section, existing tag) are hard refusals in both modes.

Environment overrides:
    STEWARD_RELEASE_REMOTE   remote name for the gitea mirror, default gitea
    STEWARD_RELEASE_GITHUB_REMOTE   remote name for github, default github
    STEWARD_RELEASE_MANIFEST_TIMEOUT   raw-manifest lag ceiling in seconds,
        default 300 (five minutes); the tool retries until then and reports
        the measured lag
    STEWARD_RELEASE_MANIFEST_RETRY    retry pause in seconds, default 5
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
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from typing import NoReturn

TAG_PREFIX = "v"
RAW_MANIFEST_TIMEOUT_DEFAULT = 300.0
RAW_MANIFEST_RETRY_DEFAULT = 5.0
GH_DOWNLOAD_TIMEOUT = 60


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


def _github_owner_repo(root: Path, remote: str) -> tuple:
    """(owner, repo) parsed from a github remote URL (https or ssh form).

    Only github.com is accepted; any other host (e.g. the gitea mirror) is
    refused loudly so a release is never aimed at the wrong host.
    """
    url = _remote_url(root, remote)
    u = url
    if u.endswith(".git"):
        u = u[:-4]
    if u.endswith("/"):
        u = u[:-1]
    # https://github.com/owner/repo
    m = re.match(r"^https?://([^/]+)/([^/]+)/([^/]+)$", u)
    if m and m.group(1) in ("github.com", "www.github.com"):
        return m.group(2), m.group(3)
    # git@github.com:owner/repo
    m = re.match(r"^([^/@]+)@([^:]+):([^/]+)/([^/]+)$", u)
    if m and m.group(2) == "github.com":
        return m.group(3), m.group(4)
    die("github remote %r is not a github.com https or ssh URL: %r"
        % (remote, url))
    raise SystemExit(1)  # pragma: no cover - die() exits


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


def _http_get(url: str, timeout: int = GH_DOWNLOAD_TIMEOUT) -> tuple:
    """(status, body) for a plain GET, following redirects. No auth header,
    so a token never leaks to a redirect target (GitHub asset CDN)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:  # noqa: BLE001 - report any transport failure
        return 0, str(exc).encode("utf-8", "replace")


def _gh(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run the gh CLI capturing output; die on non-zero when check is set."""
    cmd = [os.environ.get("STEWARD_RELEASE_GH_BIN", "gh")] + list(args)
    proc = _sh_capture(cmd, root, check=False)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        die("gh %s ... failed (exit %s): %s" % (args[0], proc.returncode, detail))
    return proc


def _gh_json(rel, args: list) -> dict | None:
    """Return gh <...> --json parsed, or None when the call fails.

    Used for release views where 'not found' is an ordinary outcome; any
    other failure still returns None and the caller re-checks reachability.
    """
    out = _gh(rel.root, *args, check=False)
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


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
    def __init__(self, version: str, dry_run: bool, publish_to: tuple = ("github",)):
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            die("--version must be X.Y.Z with digits only, got %r" % version)
        self.version = version
        self.dry_run = dry_run
        # Normalized publish targets in pipeline order (github first).
        self.publish_to = tuple(t for t in ("github", "gitea")
                                if t in set(publish_to))
        if not self.publish_to:
            die("--publish-to must include at least one of github or gitea")
        self.has_github = "github" in self.publish_to
        self.has_gitea = "gitea" in self.publish_to
        self.tag = TAG_PREFIX + version
        self.root = _repo_root()
        self.pyproject = self.root / "pyproject.toml"
        self.changelog = self.root / "docs" / "CHANGELOG.md"
        self.dist = self.root / "dist"
        self.remote = os.environ.get("STEWARD_RELEASE_REMOTE", "gitea").strip()
        self.github_remote = os.environ.get(
            "STEWARD_RELEASE_GITHUB_REMOTE", "github").strip()
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
        for host in self.publish_to:
            remote = self.github_remote if host == "github" else self.remote
            remote_out = _git(self.root, "ls-remote", remote,
                              "refs/heads/master", check=False)
            if remote_out.returncode != 0:
                die("could not reach remote %s (git ls-remote failed)" % remote)
            remote_head = (remote_out.stdout.split("\t")[0].strip()
                           if remote_out.stdout.strip() else "")
            if not remote_head:
                die("could not read %s master HEAD from the remote" % remote)
            if local_head != remote_head:
                msg = ("local HEAD %s does not equal %s/master %s; "
                       % (local_head[:12], remote, remote_head[:12]))
                if self.dry_run:
                    _say("warn %smaking this check advisory in dry-run" % msg)
                else:
                    die(msg + "the publish gates are hard in a real release")
            else:
                _say("ok   local HEAD == %s/master (%s)"
                     % (remote, remote_head[:12]))

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

        for host in self.publish_to:
            remote = self.github_remote if host == "github" else self.remote
            remote_tag = _git(self.root, "ls-remote", remote,
                              "refs/tags/" + self.tag, check=False).stdout.strip()
            if remote_tag:
                _say("warn %s already has tag %s at %s; a republish will need "
                     "release teardown first (see docs/RELEASE.md)"
                     % (remote, self.tag, remote_tag.split("\t")[0][:12]))
            else:
                _say("ok   no %s tag on %s" % (self.tag, remote))

        if self.dry_run:
            return

        if self.has_github:
            if not self._gh_authed():
                die("gh is not authenticated for github.com; run gh auth login "
                    "before cutting a release to github")
            owner, repo = _github_owner_repo(self.root, self.github_remote)
            existing = _gh_json(self, ["release", "view", self.tag,
                                       "--repo", "%s/%s" % (owner, repo),
                                       "--json", "name,assets"])
            if existing is not None:
                names = sorted(a["name"] for a in (existing.get("assets") or []))
                _say("warn github already hosts a release tagged %s%s; publish "
                     "will refuse until that release is deleted (cut a new "
                     "version, see docs/RELEASE.md)"
                     % (self.tag, (" with assets %s" % ", ".join(names)) if names else ""))
            else:
                _say("ok   no github release tagged %s" % self.tag)
        if self.has_gitea:
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

    def _gh_authed(self) -> bool:
        """True when `gh auth status` is happy for github.com."""
        proc = _gh(self.root, "auth", "status", "--hostname", "github.com",
                   check=False)
        if proc.returncode != 0:
            _say("gh auth status: %s" % ((proc.stderr or proc.stdout or "").strip()))
        return proc.returncode == 0

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
        # The manifest is read by the released binary from the raw master URL,
        # so its payload URLs must point where a stranger can download the
        # binary; that is the github release for github/both, or the gitea
        # release for a gitea-only cut.
        if self.has_github:
            owner, repo = _github_owner_repo(self.root, self.github_remote)
            base = "https://github.com/%s/%s" % (owner, repo)
        else:
            base = _gitea_base(self.root, self.remote)
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
        """The seven gitea assets, sorted by name: three artifacts, their
        .sha256 sidecars, and the SHA256SUMS file. This matches the layout of
        every prior gitea release and what scripts/install.sh expects."""
        primary = [artifacts["binary"], artifacts["deb"], artifacts["install"]]
        files = list(primary)
        files.extend(self.dist / (p.name + ".sha256") for p in primary)
        files.append(self.dist / "SHA256SUMS")
        return sorted(files, key=lambda p: p.name)

    def _github_assets(self, artifacts: dict, manifest: Path) -> list:
        """The five github assets, sorted by name: the three artifacts,
        SHA256SUMS, and the signed update manifest. The public 1.3.3 release
        chose this exact set, and github path refuses to overwrite any of
        them, so keeping the set stable matters (see docs/RELEASE.md)."""
        primary = [artifacts["binary"], artifacts["deb"], artifacts["install"]]
        files = list(primary)
        files.append(self.dist / "SHA256SUMS")
        files.append(manifest)
        return sorted(files, key=lambda p: p.name)

    @property
    def github_repo_arg(self) -> str:
        owner, repo = _github_owner_repo(self.root, self.github_remote)
        return "%s/%s" % (owner, repo)

    # -- publish plan for dry-run ---------------------------------------------
    def publish_plan(self, artifacts: dict, manifest: Path) -> None:
        _say("")
        _say("== [6/8] publish (dry run: nothing was published) ==")
        body_tail = self._release_body()
        if body_tail:
            _say("would set release body to the v%s CHANGELOG section (%d chars)"
                 % (self.version, len(body_tail)))
        _say("targets: %s" % ", ".join(self.publish_to))
        if self.has_github:
            owner, repo = _github_owner_repo(self.root, self.github_remote)
            base = "https://github.com/%s" % self.github_repo_arg
            asset_files = self._github_assets(artifacts, manifest)
            _say("would publish to github -> %s" % base)
            _say("  would create release %s on the tag with:" % self.tag)
            _say("    gh release create %s --repo %s --title 'Steward %s' "
                 "--notes-file <body>" % (self.tag, self.github_repo_arg,
                                          self.version))
            _say("  would attach and re-download %d assets:"
                 % len(asset_files))
            _say("    %-40s %-24s %12s" % ("asset", "sha256", "bytes"))
            for path in asset_files:
                _say("    %-40s %-24s %12d"
                     % (path.name, artifacts["sha256"].get(path,
                        _sha256(path))[:24], path.stat().st_size))
            _say("  payload url: %s/releases/download/%s/%s"
                 % (base, self.tag, artifacts["binary"].name))
            _say("  notes url:   %s/releases/tag/%s" % (base, self.tag))
            _say("  would first commit releases/update.json on master, push "
                 "master and %s to %s" % (self.tag, self.github_remote))
            _say("  would then verify the raw master manifest at "
                 "https://raw.githubusercontent.com/%s/%s/master/releases/update.json "
                 "equals version %s, retrying for up to %ss and reporting the "
                 "measured lag" % (owner, repo, self.version,
                                   RAW_MANIFEST_TIMEOUT_DEFAULT))
        if self.has_gitea:
            base = _gitea_base(self.root, self.remote)
            asset_files = self._assets(artifacts)
            _say("would publish to gitea -> %s" % base)
            _say("  would create release %s (target master) with %d assets:"
                 % (self.tag, len(asset_files)))
            _say("    %-40s %-24s %12s" % ("asset", "sha256", "bytes"))
            for path in asset_files:
                _say("    %-40s %-24s %12d"
                     % (path.name, artifacts["sha256"].get(path,
                        _sha256(path))[:24], path.stat().st_size))
            _say("  payload url: %s/releases/download/%s/%s"
                 % (base, self.tag, artifacts["binary"].name))
            _say("  notes url:   %s/releases/tag/%s" % (base, self.tag))
        _say("would commit on master:  release: %s publish the signed update "
             "manifest (%s)" % (self.version, manifest.name))
        _say("would tag:               %s" % self.tag)
        for host in self.publish_to:
            remote = self.github_remote if host == "github" else self.remote
            _say("would push:              %s master and refs/tags/%s"
                 % (remote, self.tag))

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
    if rel.has_github:
        # GitHub: the tag and master must already be on github before the
        # release is created, because the shipped binary reads the manifest
        # from the raw master URL and the release must sit on the tag that
        # carries it.
        commit_and_tag(rel)
        push_refs(rel, rel.github_remote)
        publish_github_release(rel, artifacts, manifest)
        verify_raw_manifest(rel)
    if rel.has_gitea:
        # Gitea behaviour is unchanged from earlier releases: create the
        # release first, then commit, tag and push.
        publish_gitea_release(rel, artifacts)
        commit_and_tag(rel)
        push_refs(rel, rel.remote)


def publish_gitea_release(rel: Release, artifacts: dict) -> None:
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
    _say("ok   created gitea release %s (%s)"
         % (rel.tag, release.get("html_url", release_id)))

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
        die("one or more gitea assets failed the re-download hash compare")


def _overlapping(existing: list, ours: list) -> list:
    """Asset names present both on the host and locally, sorted.

    The github path refuses to overwrite an overlapping asset because a
    replaced asset under the same URL is served from cache.
    """
    return sorted(set(existing) & set(ours))


def publish_github_release(rel: Release, artifacts: dict, manifest: Path) -> None:
    _say("")
    _say("publish to github -> https://github.com/%s" % rel.github_repo_arg)
    if not rel._gh_authed():
        die("gh is not authenticated for github.com; run gh auth login first")
    existing = _gh_json(rel, ["release", "view", rel.tag,
                              "--repo", rel.github_repo_arg,
                              "--json", "name,assets"])
    if existing is not None:
        names = sorted(a["name"] for a in (existing.get("assets") or []))
        ours = sorted(p.name for p in rel._github_assets(artifacts, manifest))
        overlap = _overlapping(names, ours)
        if overlap:
            die("github release %s already has asset(s): %s. GitHub serves a "
                "replaced asset from cache under the same URL, so cut_release "
                "refuses to overwrite; cut a new version (or delete the "
                "release and its assets first, see docs/RELEASE.md)"
                % (rel.tag, ", ".join(overlap)))
        die("github already has a release tagged %s; cut a new version (the "
            "tool never overwrites an existing release)" % rel.tag)

    asset_files = rel._github_assets(artifacts, manifest)
    with tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8") as notes_fh:
        notes_fh.write(rel._release_body())
        notes_path = notes_fh.name
    try:
        cmd = ["release", "create", rel.tag,
               "--repo", rel.github_repo_arg,
               "--title", "Steward %s" % rel.version,
               "--notes-file", notes_path,
               "--target", "master"]
        cmd += [str(p) for p in asset_files]
        _say("running: gh release create %s --repo %s --title 'Steward %s' "
             "--notes-file <body> --target master + %d asset(s)"
             % (rel.tag, rel.github_repo_arg, rel.version, len(asset_files)))
        proc = _gh(rel.root, *cmd, check=False)
        if proc.returncode != 0:
            die("gh release create failed (exit %s): %s"
                % (proc.returncode, (proc.stderr or proc.stdout or "").strip()))
    finally:
        try:
            os.unlink(notes_path)
        except OSError:
            pass
    _say("ok   created github release %s" % rel.tag)

    # Re-download every uploaded asset and compare sha256, exactly as the
    # gitea path does.
    base = "https://github.com/%s/releases/download/%s" % (
        rel.github_repo_arg, rel.tag)
    table = []
    for path in asset_files:
        dl_status, dl_body = _http_get("%s/%s" % (
            base, urllib.parse.quote(path.name)))
        if dl_status != 200:
            die("re-download failed for %s (status %s)" % (path.name, dl_status))
        got = hashlib.sha256(dl_body).hexdigest()
        want = _sha256(path)
        table.append((path.name, want, got, want == got))
    _say("%-42s %-24s %-24s %s" % ("asset", "uploaded sha256", "re-read sha256", "match"))
    for name, want, got, ok in table:
        _say("%-42s %s %s %s" % (name, want[:24], got[:24], "ok" if ok else "FAIL"))
    if not all(row[3] for row in table):
        die("one or more github assets failed the re-download hash compare")
    _say("ok   github release %s with %d assets verified"
         % (rel.tag, len(asset_files)))
    _say("release url: https://github.com/%s/releases/tag/%s"
         % (rel.github_repo_arg, rel.tag))


def read_manifest_app_version(body: bytes) -> str:
    """The app version from a signed update.json, or '' when unreadable.

    The committed manifest is the signed envelope ({alg, payload, sig}), so
    unwrap `payload` first, then accept both the schema-v2 `app.version`
    and the `app_version` fallback.
    """
    try:
        doc = json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return ""
    if not isinstance(doc, dict):
        return ""
    payload = doc.get("payload")
    inner = payload if isinstance(payload, dict) else doc
    app = inner.get("app")
    if isinstance(app, dict) and app.get("version"):
        return str(app.get("version")).strip()
    return str(inner.get("app_version") or "").strip()


def verify_raw_manifest(rel: Release, timeout_s: float | None = None,
                        fetch=None) -> None:
    """Assert the raw master manifest already serves the released version.

    The shipped binary reads the manifest from
    https://raw.githubusercontent.com/<owner>/<repo>/master/releases/update.json,
    so the release is not complete until that URL carries the version just
    cut. A raw CDN can serve a stale copy for a while, so retry up to
    timeout_s (default five minutes, or $STEWARD_RELEASE_MANIFEST_TIMEOUT)
    and always report the measured lag rather than claiming success.
    """
    owner, repo = _github_owner_repo(rel.root, rel.github_remote)
    url = ("https://raw.githubusercontent.com/%s/%s/master/releases/update.json"
           % (owner, repo))
    if timeout_s is None:
        timeout_s = float(os.environ.get("STEWARD_RELEASE_MANIFEST_TIMEOUT",
                                         RAW_MANIFEST_TIMEOUT_DEFAULT))
    retry_s = float(os.environ.get("STEWARD_RELEASE_MANIFEST_RETRY",
                                   RAW_MANIFEST_RETRY_DEFAULT))
    if fetch is None:
        fetch = _http_get
    _say("verifying raw master manifest: %s" % url)
    started = time.monotonic()
    deadline = started + timeout_s
    attempts = 0
    last_seen = ""
    while True:
        attempts += 1
        status, body = fetch(url)
        if status == 200 and body:
            version = read_manifest_app_version(body)
            if version:
                last_seen = version
                if version == rel.version:
                    lag = time.monotonic() - started
                    _say("ok   raw master manifest serves app version %s after "
                         "%d fetch(es); measured CDN lag %.1fs"
                         % (version, attempts, lag))
                    return
        if time.monotonic() >= deadline:
            break
        if retry_s > 0:
            time.sleep(min(retry_s, max(0.0, deadline - time.monotonic())))
    lag = time.monotonic() - started
    die("raw master manifest still serves app version %r after %.1fs and %d "
        "fetch(es); expected %s. CDN lag measured and reported, not hidden."
        % (last_seen, lag, attempts, rel.version))


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

    # Idempotent: when a release is published to both hosts this runs once
    # per host, and the commit/tag must exist only once.
    existing_tag = _git(rel.root, "rev-parse", "-q", "--verify",
                        "refs/tags/" + rel.tag, check=False)
    if existing_tag.returncode == 0:
        head = _git_stage("rev-parse", "--short", rel.tag).stdout.strip()
        _say("ok   tag %s already exists at %s (state already cut)"
             % (rel.tag, head))
        return

    staged = _git_stage("status", "--porcelain", "--", "releases/update.json")
    if staged.stdout.strip():
        _git_stage("add", "releases/update.json")
        _git_stage("commit", "-m", message)
        head = _git_stage("rev-parse", "--short", "HEAD").stdout.strip()
        _say("ok   release commit %s on master: %s" % (head, message))
    else:
        head = _git_stage("rev-parse", "--short", "HEAD").stdout.strip()
        _say("ok   releases/update.json already committed at %s (no new "
             "commit needed)" % head)
    _git_stage("tag", rel.tag)
    _say("ok   tagged %s at %s" % (rel.tag, head))


def push_refs(rel: Release, remote: str) -> None:
    _say("")
    _say("== [8/8] push (%s) ==" % remote)
    for ref in ("master", "refs/tags/%s" % rel.tag):
        proc = _git(rel.root, "push", remote, ref, check=False)
        if proc.returncode != 0:
            die("push of %s to %s failed: %s"
                % (ref, remote, (proc.stderr or proc.stdout or "").strip()))
        _say("ok   pushed %s %s" % (remote, ref))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="cut_release", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", required=True,
                        help="the release version, X.Y.Z (must match pyproject.toml)")
    parser.add_argument("--publish-to", default="github",
                        choices=("github", "gitea", "both"),
                        help="which host to publish to (default github; gitea "
                             "is the mirror)")
    parser.add_argument("--dry-run", action="store_true",
                        help="run everything except publish, commit and tag")
    args = parser.parse_args(argv)

    publish_targets = ("github", "gitea") if args.publish_to == "both" \
        else (args.publish_to,)
    rel = Release(args.version, args.dry_run, publish_to=publish_targets)
    _say("steward cut_release %s (dry run: %s) on arch %s; publish to %s"
         % (args.version, "yes" if args.dry_run else "no", rel.arch,
            ", ".join(rel.publish_to)))
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
