# Winget packaging (template only, NOT published)

`Steward.singleton.yaml` is a validated Windows Package Manager manifest
template for Steward as a portable exe, so a working package would install
with `winget install Steward`.

## Status

NOT MEASURED. There is no Windows build on this project yet: PyInstaller
cannot cross-compile, u1-runner is linux/arm64 only, and no windows-amd64 job
ever runs on the gitea board. The manifest is a validated template, not a
published or even installable package.

## What is still needed before this is a real package

- a Microsoft account, used to publish through the winget-pkgs pipeline,
- a confirmed publisher id: `Steward.Steward` must be unique on the community
  repository and match the publishing Microsoft account,
- a real SHA-256 from the actual built exe, replacing `InstallerSha256`
  (compute it with `Get-FileHash -Algorithm SHA256`),
- a public HTTPS `InstallerUrl` (the current value is the reserved
  `example.invalid` placeholder) and public `PackageUrl`/`PublisherUrl`/
  `LicenseUrl` pages, because the LAN gitea URLs are private,
- a PR into `microsoft/winget-pkgs` that passes the winget validation bot.

Publishing is a manual maintainer step outside this repository and is
NOT MEASURED.

## Validation

The manifest is validated against the official winget singleton JSON schema
(`schemas/JSON/manifests/v1.9.0/manifest.singleton.1.9.0.json` from
microsoft/winget-cli). The exact command and result are quoted in the task
record for this work, not re-claimed here. Validation confirms the shape is
correct; it does not make the package installable. The real winget pipeline
re-validates on submission.
