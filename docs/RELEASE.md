# Steward release runbook and update ownership

This is the one way a Steward release is cut. The job is automated in
`tools/cut_release.py` (driven by `make release`); a release is a git tag plus
a gitea release with the built artifacts and a signed update manifest that the
daemon already points at. The second half of this file is the contract for who
owns the installed binary, because getting that wrong strands a user on an old
version or makes two updaters fight over one file.

## Who may run it

A release changes what every installed daemon may upgrade to, moves the signed
update manifest on master, and publishes artifacts + a git tag. It is gated on
the gitea token in `~/.git-credentials` having release-create rights on
`don/steward`, the Ed25519 signing key at `~/.steward-keys/steward-signing.key`,
and write access to `master`. In practice only Don, or the release card he
authorizes, runs the real flow. Everything below is fine for anyone to rehearse
with `--dry-run`, which publishes nothing.

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
# The version argument comes from pyproject.toml via the Makefile.
make release
# or directly:
python3 tools/cut_release.py --version 1.3.0
```

The tool runs eight stages and stops on the first failure with a leading
`cut refused:` line:

1. **preflight**: clean tree (hard), on `master` and `gitea/master` in sync
   (hard in real mode, advisory in `--dry-run` so a rehearsal can run from a
   worktree), `pyproject.toml` version equals `--version` (hard), CHANGELOG has
   the `## vX.Y.Z` section (hard), the tag does not exist (hard on the local
   tag; a remote tag or an existing gitea release is reported and a republish
   must be torn down first).
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
   `releases/update.json`; the tool then re-verifies the signature through
   `steward.ota.verify_manifest` and confirms the payload sha256 matches the
   built binary.
6. **publish**: creates the gitea release `vX.Y.Z` targeting `master`, attaches
   the seven assets (three artifacts, three `.sha256` sidecars, `SHA256SUMS`),
   then re-downloads every asset and compares hashes, printing a table.
7. **commit + tag**: commits `releases/update.json` on `master` with the
   message `release: X.Y.Z publish the signed update manifest` and tags that
   commit `vX.Y.Z`.
8. **push**: pushes `master` and `refs/tags/vX.Y.Z`. Never force-push, the tool
   refuses dirty trees, and it never invents a version.

`--dry-run` does stages 1 through 5 in full (real suite, real native build,
real signed manifest) and prints the exact publish plan for stage 6, then
stops. There is no code path that pokes the gitea API, commits, or tags in
dry-run mode.

## The update manifest and the default URL

The shipped `DEFAULT_OTA_URL` in `src/steward/config.py` points at
`releases/update.json` on `master` of the private gitea repo. The manifest now
exists at that exact path because a release **commits it there** as stage 7
(and the file is already tracked, see `git ls-files releases/`).

Two honest caveats, by design:

- The URL is the **private** gitea raw path, so it answers only to a client
  with repo access. An anonymous `steward update check` on a stock install
  against that URL gets a 404 and records `state=error`; the daemon keeps
  running on the last good state (an unreachable source is never a failed
  unit). Clients that need updates pass `STEWARD_OTA_URL` or pin
  `ota_url` in `<data>/config.json`.
- A **public GitHub mirror** changes this URL. The new default is one
  constant: edit `DEFAULT_OTA_URL` in `src/steward/config.py`. Nothing else
  needs to change; the tool already derives asset URLs from the remote.

`scripts/install.sh` is independent of the manifest: it downloads
`steward-linux-<arch>` plus its `.sha256` sidecar from the gitea release and
verifies before installing, so a broken upload is caught at install time, not
after.

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
   per-platform payloads, and publish it at the configured `DEFAULT_OTA_URL`.
   Releases are cut by `tools/cut_release.py` (see the runbook above); it
   refuses to run unless the version matches `pyproject.toml` and the
   CHANGELOG section exists.
4. Verify on a real machine for each channel you changed:
   * a .deb build reports channel `apt` and `steward update apply` refuses
     with the apt command;
   * a manual install reports channel `manual` and the in-app updater works;
   * the web update card shows the managed-by line, never a dead button.
5. Never set `auto_updates` in the Homebrew cask.

## Rollback story

Bad asset or wrong hash:

- The daemon verifies the app payload sha256 before staging and again before
  the pointer commit, so a wrong-hash manifest is refused (`state=refused`)
  and installed clients stay on the last verified version. Nothing half-applied
  can take over: the swap is one atomic rename with a health probe and restore
  on failure (see the OTA module docstring).
- If the gitea release carries a wrong artifact, delete the release and its
  assets (gitea UI or API), fix the artifact, and re-run the release for the
  **next** version (a re-cut of the same tag is refused by preflight or by the
  API "release already exists" error). The fix rides out in a patch release.

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

- Never force-push. To pull a release: delete the gitea release (UI or API),
  then `git tag -d vX.Y.Z` locally on every checkout that has it. Clients keep
  the last version they applied; `steward ota status` shows what is active.
