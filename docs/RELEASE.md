# Steward release runbook and update ownership

This is the one way a Steward release is cut. The job is automated in
`tools/cut_release.py` (driven by `make release`); a release is a git tag plus
a GitHub release (unless `--publish-to` says otherwise) with the built
artifacts and a signed update manifest that every installed daemon reads from
master. GitHub is canonical: it is the only host a stranger can download
from, and the shipped binary gets its update source from the raw master file
at `https://raw.githubusercontent.com/dmltallen/steward/master/releases/update.json`.
The NAS Gitea is a mirror only, published when an explicit
`--publish-to gitea` or `--publish-to both` asks for it. The second half of
this file is the contract for who owns the installed binary, because getting
that wrong strands a user on an old version or makes two updaters fight over
one file.

## Who may run it

A release changes what every installed daemon may upgrade to, moves the signed
update manifest on master, and publishes artifacts + a git tag. It is gated on
`gh` being authenticated for github.com with `repo` scope on
`dmltallen/steward`, on the Ed25519 signing key at
`~/.steward-keys/steward-signing.key`, and on write access to `master`. A
gitea-only publish additionally needs the token in `~/.git-credentials` to
have release-create rights on `don/steward`. In practice only Don, or the
release card he authorizes, runs the real flow. Everything below is fine for
anyone to rehearse with `--dry-run`, which publishes nothing.

## Version-numbering rule

One source of truth: `version = "X.Y.Z"` in `pyproject.toml`. The git tag of a
release is exactly `vX.Y.Z`.

- Patch (1.2.0 -> 1.2.1): bug fixes that do not change behavior contracts.
- Minor (1.2.0 -> 1.3.0): new features that stay backward compatible.
- Major (1.2.0 -> 2.0.0): breaking changes to config, storage, or the update
  contract.

Before a release the version is bumped in `pyproject.toml`, a matching
`## vX.Y.Z` section is added to `docs/CHANGELOG.md`, and the change is merged
to `master`. The tool refuses to run unless the version matches `pyproject.toml`,
the CHANGELOG has the section, and the tag does not already exist.
`src/steward/__init__.py` keeps a fallback copy equal to `pyproject.toml`;
`test_version_single_source` enforces they never drift.

## Remotes and hosts

The tool names two remotes, both configured in this checkout: `github`
(`https://github.com/dmltallen/steward.git`) and `gitea`
(`http://192.168.0.101:3200/don/steward.git`). GitHub is the release target;
Gitea is a mirror that nothing on the public internet can reach, kept in sync
because earlier releases and some LAN docs still point at it. The tool
derives the github owner/repo from the `github` remote URL (`github.com`
only; any other host is refused), and the gitea API root from the `gitea`
remote. Env overrides: `STEWARD_RELEASE_REMOTE` (gitea remote name) and
`STEWARD_RELEASE_GITHUB_REMOTE` (github remote name).

Two GitHub behaviours the tool encodes, both learned from the 1.3.3 cut:

- **Never overwrite an asset.** A replaced asset under the same URL is served
  from cache, so if the release already carries one of the five asset names
  the cut refuses and tells the operator to cut a new version.
- **Master must carry the manifest before the release is usable.** The
  shipped binary reads the update source from the raw master URL, so the
  publish step commits and pushes `releases/update.json` on master, and only
  then creates the GitHub release on the tag that carries it.

## The exact commands

From the root of a **clean checkout on `master`** (do not cut from a feature
branch; the real flow refuses it):

```sh
# Rehearse end to end: preflight, suite, native build, artifact checksums,
# signed manifest. Prints exactly what would be published. Publishes nothing,
# commits nothing, tags nothing.
make release RELEASE_ARGS="--dry-run"
# or the equivalent direct call:
python3 tools/cut_release.py --version 1.3.0 --dry-run

# Cut for real: everything the dry-run does, then publish + commit + tag.
# The version argument comes from pyproject.toml via the Makefile. The
# default publish host is GitHub; pass --publish-to to choose otherwise.
make release
# or directly:
python3 tools/cut_release.py --version 1.3.0

# Publish host control: github (default), gitea (mirror only), both.
python3 tools/cut_release.py --version 1.4.0 --publish-to gitea
python3 tools/cut_release.py --version 1.4.0 --publish-to both
```

The tool runs eight stages and stops on the first failure with a leading
`cut refused:` line:

1. **preflight**: clean tree (hard), on `master` and in sync with each
   publish host's `master` (hard in real mode, advisory in `--dry-run` so a
   rehearsal can run from a worktree), `pyproject.toml` version equals
   `--version` (hard), CHANGELOG has the `## vX.Y.Z` section (hard), the
   local tag does not exist (hard); a remote tag or an existing GitHub or
   Gitea release is reported and a republish must be torn down first. For a
   GitHub publish, `gh auth status` is checked in real mode.
2. **suite**: the full `pytest` run, quoted counts. Command override:
   `STEWARD_RELEASE_SUITE_CMD` (shlex-split). Default prefers `uv run
   --no-project --with pytest --with numpy --with zeroconf --with cryptography
   python -m pytest tests -q`, then the repo venv, then `python3 -m pytest`.
3. **build**: `make build-deb` for the native arch, then the frozen binary
   version string is captured from `./dist/steward version` and must equal
   `steward X.Y.Z`.
4. **artifacts**: the binary, the `.deb`, and `scripts/install.sh` are staged
   as `steward-linux-<arch>`, `steward_<version>_<arch>.deb` and
   `steward-install.sh`; sha256 is computed for each; `SHA256SUMS` and one
   `.sha256` sidecar per artifact are written; every hash is verified by
   re-reading the file.
5. **manifest**: `tools/mint_manifest.py` signs the app block (payload sha256
   and size computed from the built artifacts, never typed) and writes
   `releases/update.json`, pointing the payload URL and notes URL at the
   GitHub release when publishing to GitHub (publicly downloadable), or at
   the Gitea release for a gitea-only cut. The tool then re-verifies the
   signature through `steward.ota.verify_manifest` and confirms the payload
   sha256 matches the built binary.
6. **publish**: for GitHub (the default), commits `releases/update.json` and
   tags first, pushes master and the tag to the github remote, then creates
   the release with `gh release create` (title "Steward X.Y.Z", notes from
   the CHANGELOG section) and attaches five assets: the binary, the `.deb`,
   `steward-install.sh`, `SHA256SUMS`, and `update.json`. It refuses to
   overwrite an existing asset, because GitHub serves a replaced asset from
   cache under the same URL; the operator must cut a new version. Every
   uploaded asset is then re-downloaded and its sha256 compared, exactly as
   the Gitea path does. For Gitea (mirror), the release is created with the
   seven historic assets (three artifacts, three `.sha256` sidecars,
   `SHA256SUMS`) and the same re-download verification.
7. **commit + tag**: commits `releases/update.json` on `master` with the
   message `release: X.Y.Z publish the signed update manifest` and tags that
   commit `vX.Y.Z`. For GitHub this happens before the release exists,
   because the shipped binary reads the manifest from master and the release
   must sit on the tag that carries it.
8. **push**: pushes `master` and `refs/tags/vX.Y.Z` to each publish host.
   After a GitHub publish the tool fetches the raw master manifest and
   asserts its app version equals the released version, retrying for up to
   five minutes and reporting the measured CDN lag instead of claiming
   success. Never force-push, the tool refuses dirty trees, and it never
   invents a version.

`--dry-run` does stages 1 through 5 in full (real suite, real native build,
real signed manifest) and prints the exact publish plan for stage 6 with the
host each release would land on, then stops. There is no code path that
pokes the GitHub or Gitea API, commits, or tags in dry-run mode.

## The update manifest and the default URL

The shipped `DEFAULT_OTA_URL` in `src/steward/config.py` points at
`https://raw.githubusercontent.com/dmltallen/steward/master/releases/update.json`,
the raw master file of the public GitHub repo. The manifest reaches that
exact path because a release **commits it there** as stage 7 and pushes
master (and the file is already tracked, see `git ls-files releases/`). A
stranger with no repo access can read this URL, which is the whole point of
GitHub being canonical.

Two honest caveats, by design:

- The raw master URL is CDN-backed, so right after a publish it can serve a
  stale copy. That is why the publish step retries the version assertion for
  up to five minutes and reports the measured CDN lag (see stage 8) instead
  of claiming success. Measured on this host on 2026-11-20: a plain read of
  this raw URL round-tripped in 0.05s, which is fetch latency, not a publish
  lag; the first real cut records its own reported lag here. A gitea-only
  publish (`--publish-to gitea`) must never be the default, because it leaves
  the public raw URL serving the previous manifest.
- `STEWARD_OTA_URL` (env) or `ota_url` in `<data>/config.json` still overrides
  the default for clients that need a different source. Reaching the default
  is never required for a normal start: an unreachable source records a
  status on the daemon instead of failing the unit.

`scripts/install.sh` is independent of the manifest: it downloads
`steward-linux-<arch>` plus its `.sha256` sidecar from the gitea release
(mirror path) and verifies before installing, so a broken upload is caught
at install time, not after. The public GitHub release carries the three
artifacts, `SHA256SUMS` and `update.json`; the gitea release additionally
keeps the three `.sha256` sidecars from earlier releases.

## One owner per install

Steward ships an in-app updater (one-click update that swaps the running
binary), and it is distributed through package managers. Those two things
must never own the same file. The daemon detects the install channel at
runtime (see `src/steward/ota.py`, `detect_channel`) and records it:

* `apt`: dpkg owns the binary (a .deb install). Updates come from apt:
  `sudo apt install --only-upgrade steward`.
* `brew`: the binary lives under a Homebrew prefix (a cask install). Updates
  come from Homebrew: `brew upgrade --cask steward`.
* `winget`: the binary lives under the winget package root (Windows). Updates
  come from winget: `winget upgrade --id Steward.Steward`.
* `manual`: a plain binary in a user path with no package manager involved.
  Steward's own updater owns updates here.
* `source`: this process runs from a source checkout. The updater owns
  updates (a developer rebuilds from git).
* `unknown`: no probe could prove an owner. Steward updates itself but says
  so, so an operator knows nothing else owns the file.

Detection is honest: it never guesses. A channel is only reported when the
platform's own tooling proves it (dpkg-query, a Homebrew prefix, a winget
path, a user path). Anything not provable is `unknown`.

## What a package-managed install may and may not do

* `/v1/update/check` may still report that a newer version exists.
* `/v1/update/apply` refuses on a managed install. It returns a structured
  result with `state: "managed"`, names the package manager, and carries the
  exact command a person runs. It never stages or swaps the binary, and it
  performs no network work for the refused self-update.
* The `/health` endpoint and every update check response carry the channel.
* On a managed channel the check reports `app.available: false` even when a
  newer version exists, so no client renders an Update button it cannot act
  on (no dead button). The response carries `channel`, `managed_by`, and
  `managed_update_command`, which is the contract a client's update card
  renders as a managed-by line instead of an Update button.

The same rule applies to the CLI: `steward update apply` refuses on a managed
install before it downloads anything.

## Homebrew cask rule

The cask at `packaging/homebrew/steward.rb` MUST NOT declare `auto_updates
true`. Steward's in-app updater refuses to run on a Homebrew-managed install,
so `auto_updates true` would tell Homebrew the app self-updates (while the
app is refusing to), hide the cask from `brew outdated`, and strand the user
on the version that shipped. Homebrew owns updates for a cask install, full
stop. This rule is restated in the cask file itself and in
`packaging/homebrew/README.md`.

## Release checklist

A release must satisfy both halves of this file:

1. Bump the version in `pyproject.toml` (single source of truth; the fallback
   in `src/steward/__init__.py` must equal it, enforced by
   `test_version_single_source`) and add the matching `## vX.Y.Z` section to
   `docs/CHANGELOG.md`.
2. Build the release artifacts per platform (Linux .deb, macOS binary, Windows
   binary) with the frozen build, and sign the app payloads.
3. Write the signed update manifest (schema v2) with the `app` block and the
   per-platform payloads, and publish it at the configured `DEFAULT_OTA_URL`
   (the raw master URL, public Github). Releases are cut by
   `tools/cut_release.py` (see the runbook above); it refuses to run unless
   the version matches `pyproject.toml` and the CHANGELOG section exists.
   The default publish host is GitHub; use `--publish-to` only to also mount
   the Gitea mirror.
4. Verify on a real machine for each channel you changed:
   * a .deb build reports channel `apt` and `steward update apply` refuses
     with the apt command;
   * a manual install reports channel `manual` and the in-app updater works;
   * the web update card shows the managed-by line, never a dead button.
5. Confirm the raw master URL serves the released version (the publish step
   does this automatically and reports the measured CDN lag).
6. Never set `auto_updates` in the Homebrew cask.

## Rollback story

Bad asset or wrong hash:

- The daemon verifies the app payload sha256 before staging and again before
  the pointer commit, so a wrong-hash manifest is refused (`state=refused`)
  and installed clients stay on the last verified version. Nothing half-applied
  can take over: the swap is one atomic rename with a health probe and restore
  on failure (see the OTA module docstring).
- If a GitHub release carries a wrong artifact, delete the release and its
  assets (`gh release delete vX.Y.Z --yes`), fix the artifact, and re-run the
  release for the **next** version. A re-cut of the same tag is refused by
  preflight and by `gh` when a release already exists for it. The fix rides
  out in a patch release. The same teardown applies to a wrong gitea release
  (gitea UI or API for `don/steward`).
- Never overwrite an asset in place: GitHub serves a replaced asset from
  cache under the same URL, so the tool refuses and tells you to cut a new
  version.

Bad manifest (signature fails, or hashes do not match the assets):

- Daemons refuse it cleanly and keep the last good state. The correction is a
  new release commit that writes a fixed `releases/update.json` (restore the
  previous release's file if the bad one was published, or simply land patch
  content) and republishes.
- `git log -- releases/update.json` lists every manifest; the previous signed
  manifest is always recoverable from history, but the fix must be a new signed
  manifest, never a hand-edited unsigned file.

An install breaks after apply:

- `steward update apply` runs a health probe on the new binary (it must answer
  `steward <version>`); on failure it restores the previous pointer and removes
  the new binary, so the old binary still answers. Manual escape hatch:
  reinstall the previous deb with
  `curl -fsSL -o steward.deb <release-download-url> && sudo dpkg -i steward.deb`,
  and if a manifest is refusing, point `STEWARD_OTA_URL` at a known-good source
  until the fix lands.

Downgrade / pull a release:

- Never force-push. To pull a release: delete the GitHub release
  (`gh release delete vX.Y.Z --yes`), delete the local and remote tag
  (`git tag -d vX.Y.Z` and `git push origin :refs/tags/vX.Y.Z`), and repeat
  on the gitea side for a gitea-published cut. Clients keep the last version
  they applied; `steward ota status` shows what is active.
