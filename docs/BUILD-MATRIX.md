# Steward build matrix

What Steward builds, on which host, under which status. Read the Status
column literally: a row that says template only or NOT MEASURED is not a
working install, no matter what the README promises.

The only real, measured artifacts today are the Linux arm64 ones built on U1.
Windows, macOS and Linux amd64 have no build that has ever run, so every
number and file for them is either a placeholder or does not exist.

## The matrix

| Platform | Build host | Artifact | Status | What unblocks a real build |
| --- | --- | --- | --- | --- |
| Linux arm64 | U1, u1-runner on the gitea board (container python:3.12-slim) | `steward-linux-arm64` | BUILT and released. sha256 `1cfcc14e2e8f...961f2e3`, measured from the real U1 arm64 build on 2026-11-19 (build output lives in `dist/`, which is gitignored) | none, this is the shipped leg |
| Debian arm64 (.deb) | U1, u1-runner | `steward_1.2.0_arm64.deb` | BUILT and released. sha256 `d9b86053a1dc...255b591d`, same measurement | none |
| Curl installer | U1 | `steward-install.sh` | BUILT, checksum-verified install exercised in a container (see packaging README) | none |
| Linux amd64 | GitHub hosted `ubuntu-latest` | `steward-linux-amd64` | Workflow leg defined in `.github/workflows/build.yml`, no build has ever run, NOT MEASURED | the repository moving to GitHub so the hosted runner exists |
| Linux arm64 (GitHub) | GitHub hosted `ubuntu-24.04-arm` (preview label, UNVERIFIED) | `steward-linux-arm64` | Workflow leg defined, never run, UNVERIFIED label | same as above plus verifying the `ubuntu-24.04-arm` label and setup-python ARM support when the repo goes public |
| Windows amd64 | GitHub hosted `windows-latest` | `steward.exe`, staged as `steward-windows-x64.exe` | Workflow leg defined, never run, NOT MEASURED | same as above; then a real exe exists, its sha256 feeds the winget manifest |
| macOS arm64 | GitHub hosted `macos-14` | `steward-darwin-arm64` | Workflow leg defined, never run, no code-signing identity or notarization configured, NOT MEASURED | same as above; then an Apple Developer account to sign and notarize, which is a separate manual step, also NOT MEASURED |
| winget manifest | none | `packaging/winget/Steward.singleton.yaml` | Template only, validated against the official winget schema | a real Windows exe sha256, a Microsoft account, a confirmed publisher id, a public installer URL, a passing winget-pkgs PR |
| Homebrew cask | none | `packaging/homebrew/steward.rb` | Template only, no tap exists | a real notarized macOS binary, a public HTTPS url, a maintained tap |

## Reading this table

- BUILT means the artifact exists and has a real checksum in this repository.
  Only the Linux arm64 rows are BUILT.
- Workflow leg defined means the CI definition exists but has never run: no
  GitHub repository exists for this project, so nothing on this board can
  execute it. Columns derived from such a build (sha256 values, exe names
  on Windows, signing state on macOS) are NOT MEASURED.
- Template only means the file is a validated placeholder that no installer
  can use yet.
- UNVERIFIED marks the `ubuntu-24.04-arm` runner label, which is GitHub's
  public ARM hosted runner preview. It is stated plainly instead of assumed:
  this workflow has not run, so the label has not been exercised here.

## What each row needs, one line

- Windows amd64: an actual Windows build, then a real sha256, then a
  Microsoft account to publish the winget manifest. The promise
  `winget install Steward` is not real until all three exist.
- macOS arm64: an actual macOS build, then an Apple Developer account for
  signing and notarization (macOS refuses unsigned installs), then a real
  Homebrew tap. The promise `brew install steward` is not real until all
  three exist.
- Linux amd64: an amd64 build host. None exists on this board; the amd64
  leg is written for a GitHub hosted runner.

## Why the gitea board builds Linux arm64 only

The only runner on the gitea board is u1-runner (linux/arm64). PyInstaller
cannot cross-compile, so a Windows or amd64 build needs a matching runner.
No such runner exists, so `.gitea/workflows/build.yaml` defines no windows,
amd64, or macOS job; adding one would leave it queued forever. The
multi-platform legs live in `.github/workflows/build.yml`, which runs only
after the repository moves to GitHub, a separate human decision.
