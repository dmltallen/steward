# Winget packaging (dry run)

`Steward.singleton.yaml` is a singleton Windows Package Manager manifest for
the Steward desktop app and CLI.

## Status

NOT MEASURED. This project has no Windows build yet: PyInstaller does not
cross-compile, and the only CI runner (u1-runner) is linux/arm64, so no
windows-amd64 job is queued. The manifest is a validated template: the
`InstallerUrl` points at a reserved `example.invalid` placeholder and
`InstallerSha256` is all zeros until a real installer exists.

## Validation

The manifest validates against the official winget singleton JSON schema
(`schemas/JSON/manifests/v1.9.0/manifest.singleton.1.9.0.json` from
microsoft/winget-cli). It is intended for dry-run validation only.

## Publishing (out of scope here)

Publishing a package to the Windows Package Manager community repository
requires a Microsoft account, a GitHub PR into microsoft/winget-pkgs with the
manifest set in the canonical directory layout, and passing the winget
validation bot. Publishing is a manual step done by a maintainer outside this
repository and is NOT MEASURED.
