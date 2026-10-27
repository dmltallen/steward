# Packaging

How Steward is built and shipped to consumers.

| Artifact | Source | Validated |
| --- | --- | --- |
| Linux one-file binary | `pyinstaller.spec` | built and run on U1 (linux/arm64) |
| Debian package (.deb) | `nfpm.yaml` + `systemd/steward.service` | installed and removed in `debian:stable-slim` |
| Windows manifest | `winget/Steward.singleton.yaml` | JSON schema v1.9.0, dry run only |
| macOS cask | `homebrew/steward.rb` | structural template only, NOT MEASURED |
| Curl installer | `scripts/install.sh` | checksum-verified install in a container |

## Build

From the repo root:

```
make build-linux     # one-file binary at dist/steward
make build-deb       # .deb at dist/, uses nfpm when present else dpkg-deb
make build-win       # documented only, no cross compile, never on u1-runner
make clean
```

The native build target on U1 is linux/arm64. The same Makefile targets build
linux/amd64 or linux/arm64 depending on the host (arch comes from `uname -m`).
PyInstaller does not cross-compile, so each binary must be built on matching
hardware.

## Install paths

- `.deb` installs `/usr/bin/steward` plus a systemd user unit at
  `/usr/lib/systemd/user/steward.service`.
- `scripts/install.sh` installs to `$HOME/.local/bin/steward`.
- The cURL one-liner form is: `sh -c "$(curl -fsSL <script-url>)"`.

## Release flow

Tag pushes to `don/steward` (gitea) trigger `.gitea/workflows/build.yaml` on
u1-runner: it builds the native Linux one-file binary and .deb, writes
`<asset>.sha256` checksum files, and attaches everything as assets to the
gitea release for the tag. `scripts/install.sh` then fetches the matching
asset and verifies its checksum before installing.

## Measured versus not measured

Anything without a real build behind it is NOT MEASURED here: Windows and
macOS builds do not exist, so their sha256 values are placeholders, and
publishing to winget, Homebrew, or any store is out of scope and NOT MEASURED.
Only the Linux artifacts are real, and only they are reported with real
numbers.
