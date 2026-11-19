# Release notes and update ownership

This file is the contract for whoever ships a Steward release. The most
important rule is about who owns the installed binary, because getting it
wrong strands a user on an old version or makes two updaters fight over one
file.

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

1. Bump the version in `src/steward/__init__.py` (single source of truth).
2. Build the release artifacts per platform (Linux .deb, macOS binary, Windows
   binary) with the frozen build, and sign the app payloads.
3. Write the signed update manifest (schema v2) with the `app` block and the
   per-platform payloads, and publish it at the configured `DEFAULT_OTA_URL`.
4. Verify on a real machine for each channel you changed:
   * a .deb build reports channel `apt` and `steward update apply` refuses
     with the apt command;
   * a manual install reports channel `manual` and the in-app updater works;
   * the web update card shows the managed-by line, never a dead button.
5. Never set `auto_updates` in the Homebrew cask.
